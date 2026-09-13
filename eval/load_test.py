"""Class-session burst test: N students each ask a few questions within a window.
Measures time-to-first-token / time-to-done p50/p95 and error rate against a running core.

  python eval/load_test.py --base http://127.0.0.1:8000 --students 400 --questions 2 --window 300 --admin dev-admin-token
Creates throwaway enrollment codes through /admin/codes (they count as students in the DB;
delete them afterwards with `DELETE FROM students WHERE label LIKE 'load-%'`).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import random
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def load_questions() -> list[str]:
    qs = []
    for lang in ("en", "ar", "fr"):
        p = ROOT / "eval" / f"qa_{lang}.jsonl"
        if p.exists():
            qs += [json.loads(l)["q"] for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    return qs


async def student(client: httpx.AsyncClient, base: str, code: str, i: int, questions: list[str], n: int, window: float, results: list) -> None:
    await asyncio.sleep(random.uniform(0, window))
    try:
        r = await client.post(f"{base}/auth/redeem", json={"code": code, "device_id": f"load-{i:04d}-device"})
        token = r.json()["token"]
    except Exception as e:
        results.append({"ok": False, "stage": "auth", "err": str(e)[:80]})
        return
    H = {"authorization": f"Bearer {token}"}
    for _ in range(n):
        q = random.choice(questions)
        t0 = time.perf_counter()
        ttft = None
        source = None
        try:
            async with client.stream("POST", f"{base}/chat", headers=H, json={"message": q}, timeout=90) as resp:
                if resp.status_code != 200:
                    results.append({"ok": False, "stage": "chat", "err": f"http_{resp.status_code}"})
                    continue
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    ev = json.loads(line[5:])
                    if ev["type"] == "token" and ttft is None:
                        ttft = time.perf_counter() - t0
                    if ev["type"] == "done":
                        source = ev["source"]
            results.append({"ok": True, "ttft": ttft or 0, "total": time.perf_counter() - t0, "source": source})
        except Exception as e:
            results.append({"ok": False, "stage": "chat", "err": str(e)[:80]})
        await asyncio.sleep(random.uniform(8, 20))  # think time (also respects the 6/min cap)


def pct(v: list[float], p: float) -> float:
    if not v:
        return 0.0
    v = sorted(v)
    return v[min(len(v) - 1, int(p * len(v)))]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--students", type=int, default=100)
    ap.add_argument("--questions", type=int, default=2)
    ap.add_argument("--window", type=float, default=120, help="seconds over which students start")
    ap.add_argument("--admin", required=True)
    args = ap.parse_args()
    questions = load_questions()
    async with httpx.AsyncClient(timeout=30, limits=httpx.Limits(max_connections=600)) as client:
        labels = "\n".join(f"load-{i:04d}" for i in range(args.students))
        r = await client.post(f"{args.base}/admin/codes", files={"labels": ("labels.csv", labels)}, headers={"x-admin-token": args.admin})
        codes = [row["code"] for row in csv.DictReader(io.StringIO(r.text))]
        results: list = []
        t0 = time.time()
        await asyncio.gather(*(student(client, args.base, c, i, questions, args.questions, args.window, results) for i, c in enumerate(codes)))
        wall = time.time() - t0
    ok = [x for x in results if x["ok"]]
    bad = [x for x in results if not x["ok"]]
    ttft = [x["ttft"] for x in ok]
    total = [x["total"] for x in ok]
    by_source = {}
    for x in ok:
        by_source[x["source"]] = by_source.get(x["source"], 0) + 1
    print(f"students={args.students} requests={len(results)} ok={len(ok)} errors={len(bad)} wall={wall:.0f}s")
    print(f"TTFT p50={pct(ttft, .5) * 1000:.0f}ms p95={pct(ttft, .95) * 1000:.0f}ms max={max(ttft or [0]) * 1000:.0f}ms")
    print(f"done p50={pct(total, .5) * 1000:.0f}ms p95={pct(total, .95) * 1000:.0f}ms  sources={by_source}")
    if bad:
        errs = {}
        for b in bad:
            errs[b["err"]] = errs.get(b["err"], 0) + 1
        print("errors:", errs)


if __name__ == "__main__":
    asyncio.run(main())
