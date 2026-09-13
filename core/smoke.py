"""Quick manual smoke test against a running core: python smoke.py CODE"""
import json
import sys

import httpx

BASE = "http://127.0.0.1:8000"
code = sys.argv[1]
tok = httpx.post(f"{BASE}/auth/redeem", json={"code": code, "device_id": "dev-device-0001"}).json()["token"]
H = {"authorization": f"Bearer {tok}"}

tests = [
    ("EN on-topic", "How does the peak finding algorithm work in one dimension?"),
    ("AR off-topic", "إيه أحسن مطعم كشري في القاهرة؟"),
    ("AR on-topic", "اشرحلي إزاي بنلاقي الـ peak في الـ array بطريقة binary search"),
    ("Arabizi on-topic", "ezay el peak finding algorithm beyeshta8al fel 1D array?"),
    ("FR on-topic", "Comment fonctionne l'algorithme de recherche de pic en une dimension ?"),
    ("FR off-topic", "Quelle est la capitale de l'Australie ?"),
]
for name, msg in tests:
    meta = status = done = None
    cites = []
    with httpx.stream("POST", f"{BASE}/chat", headers=H, json={"message": msg}, timeout=60) as r:
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:])
            if ev["type"] == "meta":
                meta = ev
            elif ev["type"] == "status":
                status = ev["stage"]
            elif ev["type"] == "citations":
                cites = ev["items"]
            elif ev["type"] == "done":
                done = ev
    top = cites[0]["score"] if cites else None
    print(f"{name:18s} lang={meta['lang']} arabizi={meta['arabizi']} stage={status} source={done['source']} top_score={top} ms={done['ms']}")
    print("   ", done["answer"][:140].replace("\n", " | "))
