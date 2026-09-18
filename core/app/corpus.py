"""Corpus loading and hybrid retrieval.

Index layout (produced by pipeline/ingest.py), one shard per video so clips can be
added or re-transcribed without a full re-index:
  corpus/manifest.json                 {corpus_version, videos:[{id,title,source,youtube_id|url,duration,lang,week}]}
  corpus/index/<video_id>.chunks.jsonl {chunk_id, video_id, t_start, t_end, text}
  corpus/index/<video_id>.vecs.npy     float32[n, dim]  (e5 'passage:' embeddings, normalized)
  corpus/transcripts/<video_id>.vtt    caption track served to the player
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import settings


@dataclass
class Chunk:
    chunk_id: str
    video_id: str
    t_start: float
    t_end: float
    text: str


@dataclass
class Video:
    id: str
    title: str
    source: str            # youtube | file
    youtube_id: str | None = None
    url: str | None = None
    duration: float | None = None
    lang: str | None = None
    week: int | None = None


@dataclass
class Hit:
    chunk: Chunk
    score: float           # fused score in [0,1]
    dense: float
    sparse: float


_TOKEN_RE = re.compile(r"[\w']+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    # Light normalisation that helps Arabic BM25: strip tatweel/diacritics, unify alef/yaa/taa-marbuta.
    t = re.sub(r"[ً-ْـ]", "", text.lower())
    t = re.sub(r"[إأآا]", "ا", t)
    t = t.replace("ى", "ي").replace("ة", "ه")
    return _TOKEN_RE.findall(t)


class Corpus:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.Lock()
        self.version = "empty"
        self.videos: dict[str, Video] = {}
        self.chunks: list[Chunk] = []
        self.vecs: np.ndarray = np.zeros((0, 768), dtype=np.float32)
        self._bm25 = None
        self._by_id: dict[str, int] = {}
        self.shard_mtimes: dict[str, float] = {}

    # ---- loading -------------------------------------------------------------
    def load(self) -> None:
        manifest_path = self.root / "manifest.json"
        if not manifest_path.exists():
            return
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        videos = {v["id"]: Video(**{k: v.get(k) for k in Video.__dataclass_fields__}) for v in manifest["videos"]}
        chunks: list[Chunk] = []
        mats: list[np.ndarray] = []
        mtimes: dict[str, float] = {}
        for vid in videos:
            cpath = self.root / "index" / f"{vid}.chunks.jsonl"
            vpath = self.root / "index" / f"{vid}.vecs.npy"
            if not (cpath.exists() and vpath.exists()):
                continue
            mtimes[vid] = max(cpath.stat().st_mtime, vpath.stat().st_mtime)
            rows = [json.loads(l) for l in cpath.read_text(encoding="utf-8").splitlines() if l.strip()]
            m = np.load(vpath).astype(np.float32)
            if len(rows) != m.shape[0]:
                raise RuntimeError(f"shard mismatch for {vid}: {len(rows)} chunks vs {m.shape[0]} vectors")
            chunks.extend(Chunk(**r) for r in rows)
            mats.append(m)
        vecs = np.vstack(mats) if mats else np.zeros((0, 768), dtype=np.float32)
        bm25 = None
        if chunks:
            import bm25s

            bm25 = bm25s.BM25()
            bm25.index([tokenize(c.text) for c in chunks], show_progress=False)
        with self.lock:
            self.version = manifest.get("corpus_version", "0")
            self.videos = videos
            self.chunks = chunks
            self.vecs = vecs
            self._bm25 = bm25
            self._by_id = {c.chunk_id: i for i, c in enumerate(chunks)}
            self.shard_mtimes = mtimes

    @property
    def size(self) -> int:
        return len(self.chunks)

    # ---- retrieval -----------------------------------------------------------
    def search(self, qvecs: np.ndarray, queries: list[str], k: int = 6, video_ids: set[str] | None = None) -> list[Hit]:
        """Hybrid: dense cosine over all query vectors (max-pooled) + BM25 over all
        query strings, fused with reciprocal rank fusion. Dense score is kept as the
        gate signal because it is comparable across languages.

        `video_ids` restricts results to those videos (see content.published_video_ids):
        chunks outside it are zeroed out before ranking, so they can't win a slot, can't
        raise the gate score, and can't cite. None = the whole corpus."""
        if not self.chunks:
            return []
        with self.lock:
            dense = (self.vecs @ qvecs.T).max(axis=1)  # [n]
            sparse = np.zeros(len(self.chunks), dtype=np.float32)
            if self._bm25 is not None:
                for q in queries:
                    toks = tokenize(q)
                    if not toks:
                        continue
                    s = self._bm25.get_scores(toks)
                    sparse = np.maximum(sparse, s.astype(np.float32))
            allowed = None
            if video_ids is not None:
                allowed = np.array([c.video_id in video_ids for c in self.chunks])
                dense = np.where(allowed, dense, -1.0)
                sparse = np.where(allowed, sparse, 0.0)
        if sparse.max() > 0:
            sparse = sparse / sparse.max()
        d_rank = np.argsort(-dense)
        s_rank = np.argsort(-sparse)
        rrf = np.zeros(len(self.chunks), dtype=np.float32)
        K = 60.0
        for r, i in enumerate(d_rank[: k * 5]):
            rrf[i] += 1.0 / (K + r)
        for r, i in enumerate(s_rank[: k * 5]):
            rrf[i] += 0.7 / (K + r)  # dense weighted higher: it is the cross-lingual signal
        if allowed is not None:
            # A small scope (fewer than k*5 chunks) would otherwise let zeroed-out chunks
            # still collect a rank position above — hard-exclude them from the final pick.
            rrf = np.where(allowed, rrf, 0.0)
        top = np.argsort(-rrf)[:k]
        hits = [Hit(self.chunks[i], float(rrf[i]), float(dense[i]), float(sparse[i])) for i in top if rrf[i] > 0]
        # Merge adjacent chunks of the same video into one context window when they overlap.
        return hits

    def neighbours(self, chunk: Chunk) -> list[Chunk]:
        """Adjacent chunks in the same video (for wider context windows)."""
        i = self._by_id.get(chunk.chunk_id)
        if i is None:
            return []
        out = []
        for j in (i - 1, i + 1):
            if 0 <= j < len(self.chunks) and self.chunks[j].video_id == chunk.video_id:
                out.append(self.chunks[j])
        return out

    def topics(self, n: int = 3, lang: str | None = None) -> list[Video]:
        return list(self.videos.values())[:n]


corpus = Corpus(settings.corpus_dir)
