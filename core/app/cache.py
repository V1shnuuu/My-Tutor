"""Semantic answer cache. A hit (same language, cosine ≥ threshold, same corpus
version) skips the LLM entirely and is served in ~150 ms. Entries citing a video that
is re-ingested are evicted. Kept in memory alongside SQLite for O(1 ms) lookups."""
from __future__ import annotations

import json
import threading
import time

import numpy as np

from .config import settings
from .db import query, tx


class SemanticCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.ids: list[int] = []
        self.langs: list[str] = []
        self.video_ids: list[set[str]] = []
        self.vecs = np.zeros((0, 768), dtype=np.float32)

    def load(self, corpus_version: str) -> None:
        rows = query("SELECT id, lang, embedding, video_ids FROM cache WHERE corpus_version = ?", (corpus_version,))
        ids, langs, vids, mats = [], [], [], []
        for r in rows:
            ids.append(r["id"])
            langs.append(r["lang"])
            vids.append(set(json.loads(r["video_ids"])))
            mats.append(np.frombuffer(r["embedding"], dtype=np.float32))
        with self.lock:
            self.ids, self.langs, self.video_ids = ids, langs, vids
            self.vecs = np.vstack(mats) if mats else np.zeros((0, 768), dtype=np.float32)

    def lookup(self, qvec: np.ndarray, lang: str, allowed_video_ids: set[str] | None = None) -> dict | None:
        """`allowed_video_ids` mirrors corpus.search's scope: an entry citing any video
        outside it is treated as a miss, not a hit — otherwise an answer grounded in a
        sample lecture, cached before a course was published, keeps beating the right
        one on similarity forever."""
        with self.lock:
            if self.vecs.shape[0] == 0:
                return None
            sims = self.vecs @ qvec
            mask = np.array([l == lang for l in self.langs])
            if allowed_video_ids is not None:
                mask &= np.array([v <= allowed_video_ids for v in self.video_ids])
            sims = np.where(mask, sims, -1.0)
            i = int(np.argmax(sims))
            if sims[i] < settings.cache_threshold:
                return None
            cid = self.ids[i]
        rows = query("SELECT * FROM cache WHERE id = ?", (cid,))
        if not rows:
            return None
        r = rows[0]
        with tx() as c:
            c.execute("UPDATE cache SET hits = hits + 1 WHERE id = ?", (cid,))
        return {
            "id": cid,
            "question": r["question"],
            "answer": r["answer"],
            "citations": json.loads(r["citations"]),
            "similarity": float(sims[i]),
        }

    def store(self, qvec: np.ndarray, lang: str, question: str, answer: str, citations: list[dict], corpus_version: str) -> None:
        video_ids = sorted({c["video_id"] for c in citations})
        with tx() as c:
            cur = c.execute(
                "INSERT INTO cache (lang, question, answer, citations, embedding, corpus_version, video_ids, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (lang, question, answer, json.dumps(citations, ensure_ascii=False), qvec.astype(np.float32).tobytes(),
                 corpus_version, json.dumps(video_ids), int(time.time())),
            )
            cid = cur.lastrowid
        with self.lock:
            self.ids.append(cid)
            self.langs.append(lang)
            self.video_ids.append(set(video_ids))
            self.vecs = np.vstack([self.vecs, qvec[None, :].astype(np.float32)])

    def evict_videos(self, video_ids: list[str]) -> int:
        if not video_ids:
            return 0
        rows = query("SELECT id, video_ids FROM cache")
        dead = [r["id"] for r in rows if set(json.loads(r["video_ids"])) & set(video_ids)]
        if dead:
            with tx() as c:
                c.executemany("DELETE FROM cache WHERE id = ?", [(d,) for d in dead])
        return len(dead)


semantic_cache = SemanticCache()
