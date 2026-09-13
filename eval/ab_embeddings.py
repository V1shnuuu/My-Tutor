"""A/B embedding models on the eval set without touching the live index.
python eval/ab_embeddings.py intfloat/multilingual-e5-base BAAI/bge-m3 intfloat/multilingual-e5-large
Reports recall@5 / cite@1 / gate separation per language, plus query latency on this CPU."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "eval"))
TOL = 30


def main(models: list[str]) -> None:
    from sentence_transformers import SentenceTransformer

    from app.corpus import corpus, tokenize
    from app.lang import detect, transliterate_arabizi

    corpus.load()
    chunks = corpus.chunks
    import bm25s

    bm = bm25s.BM25()
    bm.index([tokenize(c.text) for c in chunks], show_progress=False)
    sets = {lang: [json.loads(l) for l in (ROOT / "eval" / f"qa_{lang}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()] for lang in ("en", "ar", "fr")}

    for name in models:
        t0 = time.time()
        model = SentenceTransformer(name, device="cpu")
        load_s = time.time() - t0
        is_e5 = "e5" in name
        pfx_p, pfx_q = ("passage: ", "query: ") if is_e5 else ("", "")
        P = np.asarray(model.encode([pfx_p + c.text for c in chunks], normalize_embeddings=True, batch_size=8), dtype=np.float32)
        print(f"\n== {name}  (load {load_s:.0f}s, dim {P.shape[1]})")
        for lang, rows in sets.items():
            rec = cite = g = 0
            on, off, lat = [], [], []
            for r in rows:
                det = detect(r["q"])
                qs = [r["q"]] + ([transliterate_arabizi(r["q"])] if det.arabizi else [])
                t1 = time.time()
                Q = np.asarray(model.encode([pfx_q + q for q in qs], normalize_embeddings=True), dtype=np.float32)
                lat.append((time.time() - t1) * 1000)
                dense = (P @ Q.T).max(axis=1)
                sparse = np.zeros(len(chunks), dtype=np.float32)
                for q in qs:
                    toks = tokenize(q)
                    if toks:
                        sparse = np.maximum(sparse, bm.get_scores(toks).astype(np.float32))
                if sparse.max() > 0:
                    sparse /= sparse.max()
                rrf = np.zeros(len(chunks), dtype=np.float32)
                for rank, i in enumerate(np.argsort(-dense)[:30]):
                    rrf[i] += 1 / (60 + rank)
                for rank, i in enumerate(np.argsort(-sparse)[:30]):
                    rrf[i] += 0.7 / (60 + rank)
                top = np.argsort(-rrf)[:5]
                best = float(dense.max())
                if r["kind"] == "grounded":
                    g += 1
                    on.append(best)
                    ok = [chunks[i].t_start <= r["t_hi"] + TOL and chunks[i].t_end >= r["t_lo"] - TOL for i in top]
                    rec += any(ok)
                    cite += ok[0]
                else:
                    off.append(best)
            print(f"  [{lang}] recall@5={rec / g:.3f} cite@1={cite / g:.3f} on_min={min(on):.3f} off_max={max(off):.3f} margin={min(on) - max(off):+.3f} q_p50={sorted(lat)[len(lat) // 2]:.0f}ms")


if __name__ == "__main__":
    main(sys.argv[1:] or ["intfloat/multilingual-e5-base"])
