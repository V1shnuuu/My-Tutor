"""Offline RAG evaluation (no LLM calls): retrieval recall, citation accuracy, gate/refusal
precision and language detection, per language. Runs in CI and gates deploys.

  python eval/run_eval.py            # all languages
  python eval/run_eval.py --lang ar  # one language
Exit code 1 when any threshold fails.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

# Offline thresholds (no LLM in the loop). The LLM prompt is the final off-topic guard, so the gate
# is measured as a cost-saver (refused_at_gate, informational) and must never refuse grounded questions.
THRESHOLDS = {"recall@5": 0.85, "cite@1": 0.60, "gate_recall": 0.95, "lang_acc": 0.95, "guard_ok": 1.0}
TOL = 30  # seconds of tolerance around a gold window: lecture topics bleed across chunk edges


def overlaps(chunk, lo, hi) -> bool:
    return chunk.t_start <= hi + TOL and chunk.t_end >= lo - TOL


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", choices=["ar", "en", "fr"])
    ap.add_argument("--json", help="write a JSON report here")
    args = ap.parse_args()

    from app.config import settings
    from app.corpus import corpus
    from app.embed import embed_queries
    from app.guard import is_blocked
    from app.lang import detect, transliterate_arabizi

    corpus.load()
    if corpus.size == 0:
        print("corpus is empty — run pipeline/ingest.py first")
        return 1

    report = {}
    failed = False
    for lang in [args.lang] if args.lang else ["en", "ar", "fr"]:
        path = ROOT / "eval" / f"qa_{lang}.jsonl"
        if not path.exists():
            continue
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        m = {"n": len(rows), "recall@5": 0, "cite@1": 0, "grounded": 0, "gated_in": 0, "offtopic": 0, "refused": 0, "lang_ok": 0, "latency_ms": [], "false_block": 0}
        scores_on, scores_off = [], []
        for r in rows:
            t0 = time.time()
            det = detect(r["q"])
            if det.lang == lang:
                m["lang_ok"] += 1
            queries = [r["q"]] + ([transliterate_arabizi(r["q"])] if det.arabizi else [])
            qv = embed_queries(queries)
            hits = corpus.search(qv, queries, settings.top_k)
            m["latency_ms"].append(int((time.time() - t0) * 1000))
            best = max((h.dense for h in hits), default=0.0)
            blocked = is_blocked(r["q"])
            gated_in = bool(hits) and best >= settings.gate_threshold(lang) and not blocked
            if r["kind"] == "grounded":
                m["grounded"] += 1
                m["false_block"] += blocked
                scores_on.append(best)
                m["gated_in"] += gated_in
                top5 = [h.chunk for h in hits[:5] if h.chunk.video_id == r["video_id"]]
                if any(overlaps(c, r["t_lo"], r["t_hi"]) for c in top5):
                    m["recall@5"] += 1
                if hits and hits[0].chunk.video_id == r["video_id"] and overlaps(hits[0].chunk, r["t_lo"], r["t_hi"]):
                    m["cite@1"] += 1
            else:
                m["offtopic"] += 1
                scores_off.append(best)
                m["refused"] += (not gated_in)
        g, o = max(1, m["grounded"]), max(1, m["offtopic"])
        res = {
            "n": m["n"],
            "recall@5": round(m["recall@5"] / g, 3),
            "cite@1": round(m["cite@1"] / g, 3),
            "gate_recall": round(m["gated_in"] / g, 3),
            "refused_at_gate": round(m["refused"] / o, 3),
            "guard_ok": 1.0 if m["false_block"] == 0 else 0.0,
            "lang_acc": round(m["lang_ok"] / m["n"], 3),
            "p50_ms": sorted(m["latency_ms"])[len(m["latency_ms"]) // 2],
            "gate_threshold": settings.gate_threshold(lang),
            "score_on_topic_min": round(min(scores_on), 3) if scores_on else None,
            "score_off_topic_max": round(max(scores_off), 3) if scores_off else None,
        }
        report[lang] = res
        bad = [k for k, v in THRESHOLDS.items() if res[k] < v]
        failed |= bool(bad)
        print(f"[{lang}] " + "  ".join(f"{k}={v}" for k, v in res.items()) + ("  FAIL: " + ",".join(bad) if bad else "  OK"))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
