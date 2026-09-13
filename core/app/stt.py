"""Live STT: Groq Whisper-large-v3-turbo (best Egyptian accuracy) under a daily budget.
When the budget is spent or the key is absent the client is told to fall back to the
browser's Web Speech API (unlimited) — see web/src/lib/stt.ts."""
from __future__ import annotations

import time

import httpx
from fastapi import HTTPException

from .auth import today
from .config import settings
from .db import query, tx

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_minute: list[float] = []


def budget() -> dict:
    day = today()
    rows = query("SELECT requests FROM provider_usage WHERE provider = 'groq-stt' AND day = ?", (day,))
    used = rows[0]["requests"] if rows else 0
    return {"used": used, "cap": settings.stt_daily_cap, "available": bool(settings.groq_api_key) and used < settings.stt_daily_cap}


async def transcribe(audio: bytes, filename: str, content_type: str, lang_hint: str | None, vocab: str) -> dict:
    b = budget()
    if not b["available"]:
        raise HTTPException(429, "stt_budget")
    now = time.time()
    while _minute and now - _minute[0] > 60:
        _minute.pop(0)
    if len(_minute) >= 18:  # Groq free: 20 RPM
        raise HTTPException(429, "stt_rpm")
    _minute.append(now)

    data = {"model": "whisper-large-v3-turbo", "response_format": "verbose_json", "temperature": "0"}
    if lang_hint in ("ar", "en", "fr"):
        data["language"] = lang_hint
    if vocab:
        data["prompt"] = vocab[:800]
    files = {"file": (filename or "audio.webm", audio, content_type or "audio/webm")}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(GROQ_URL, headers={"Authorization": f"Bearer {settings.groq_api_key}"}, data=data, files=files)
    ok = r.status_code < 400
    with tx() as c:
        c.execute("INSERT OR IGNORE INTO provider_usage (provider, day) VALUES ('groq-stt', ?)", (today(),))
        c.execute("UPDATE provider_usage SET requests = requests + 1, errors = errors + ? WHERE provider = 'groq-stt' AND day = ?", (0 if ok else 1, today()))
    if not ok:
        raise HTTPException(502, f"stt_upstream_{r.status_code}")
    j = r.json()
    return {"text": (j.get("text") or "").strip(), "language": j.get("language"), "duration": j.get("duration")}
