"""Live STT, cloud first then local, so voice input never depends on a key.

  1. Groq Whisper-large-v3-turbo — best Egyptian accuracy, under a daily budget.
  2. Local faster-whisper — the same engine the ingest pipeline uses. No key, no quota,
     nothing leaves the machine; slower, and sized by LOCAL_STT_MODEL.
  3. Neither: the client falls back to the browser's Web Speech API — see web/src/lib/stt.ts.

Only when all three are unavailable is the student asked to type instead."""
from __future__ import annotations

import asyncio
import io
import threading
import time

import httpx
from fastapi import HTTPException

from .auth import today
from .config import settings
from .db import query, tx

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_minute: list[float] = []
_local_model = None
_local_lock = threading.Lock()
_local_sem: asyncio.Semaphore | None = None


def local_available() -> bool:
    if not settings.local_stt_enabled:
        return False
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        return False
    return True


def budget() -> dict:
    day = today()
    rows = query("SELECT requests FROM provider_usage WHERE provider = 'groq-stt' AND day = ?", (day,))
    used = rows[0]["requests"] if rows else 0
    groq_ok = bool(settings.groq_api_key) and used < settings.stt_daily_cap
    local_ok = local_available()
    return {
        "used": used,
        "cap": settings.stt_daily_cap,
        # What the client asks: is server-side STT worth uploading to at all? Local counts,
        # otherwise a keyless install would send every student to the browser recogniser.
        "available": groq_ok or local_ok,
        "engine": "groq" if groq_ok else ("local" if local_ok else "none"),
        "local": local_ok,
    }


def _local_whisper():
    global _local_model
    if _local_model is None:
        with _local_lock:
            if _local_model is None:
                from faster_whisper import WhisperModel

                _local_model = WhisperModel(
                    settings.local_stt_model,
                    device=settings.local_stt_device,
                    compute_type=settings.local_stt_compute,
                )
    return _local_model


# Whisper's Arabic is overwhelmingly Modern Standard, so an Egyptian question comes back
# "corrected" into MSA — كيف for إزاي, ماذا for إيه. initial_prompt sets the register it
# continues in, so a line of ordinary Cairo speech in front of the course jargon keeps the
# transcript in the dialect the student actually spoke.
DIALECT_PRIMER = {
    "ar": "إزاي الطريقة دي بتشتغل؟ إيه الفرق بين الحاجتين دول؟ ليه بنعمل كده؟",
    "en": "",
    "fr": "",
}


def _prompt_for(lang_hint: str | None, vocab: str) -> str | None:
    primer = DIALECT_PRIMER.get(lang_hint or "", "")
    prompt = f"{primer} {vocab}".strip() if primer else vocab
    return prompt[:800] or None


def _transcribe_local_sync(audio: bytes, lang_hint: str | None, vocab: str) -> dict:
    model = _local_whisper()
    segments, info = model.transcribe(
        io.BytesIO(audio),
        # Never force the decode language from lang_hint (the UI toggle): a student who
        # hasn't switched the toggle yet, or code-switches mid-sentence, would otherwise have
        # their actual speech force-decoded as the wrong language and come back garbled —
        # detect() in lang.py then answers in whatever that garbled text happens to look like.
        # Auto-detect what was actually spoken; the dialect primer below still biases Arabic
        # decoding toward Egyptian without forcing it.
        language=None,
        initial_prompt=_prompt_for(lang_hint, vocab),
        beam_size=1,  # a live question is short: greedy keeps it responsive
        vad_filter=True,  # drop the silence around the utterance before decoding
    )
    text = " ".join(seg.text for seg in segments).strip()
    return {"text": text, "language": info.language, "duration": info.duration}


async def _transcribe_local(audio: bytes, lang_hint: str | None, vocab: str) -> dict:
    global _local_sem
    if _local_sem is None:
        _local_sem = asyncio.Semaphore(max(1, settings.local_stt_concurrency))
    # Decoding is CPU-bound and would otherwise stall the event loop for every other
    # student on the box.
    async with _local_sem:
        try:
            return await asyncio.to_thread(_transcribe_local_sync, audio, lang_hint, vocab)
        except Exception as e:
            raise HTTPException(503, "stt_local_failed") from e


async def transcribe(audio: bytes, filename: str, content_type: str, lang_hint: str | None, vocab: str) -> dict:
    b = budget()
    if not b["available"]:
        raise HTTPException(429, "stt_budget")
    if b["engine"] == "local":
        return await _transcribe_local(audio, lang_hint, vocab)
    now = time.time()
    while _minute and now - _minute[0] > 60:
        _minute.pop(0)
    if len(_minute) >= 18:  # Groq free: 20 RPM
        if local_available():
            return await _transcribe_local(audio, lang_hint, vocab)
        raise HTTPException(429, "stt_rpm")
    _minute.append(now)

    data = {"model": "whisper-large-v3-turbo", "response_format": "verbose_json", "temperature": "0"}
    # Same reasoning as the local path above: let Groq auto-detect the spoken language rather
    # than forcing lang_hint (the UI toggle), which may not match what was actually said.
    prompt = _prompt_for(lang_hint, vocab)
    if prompt:
        data["prompt"] = prompt
    files = {"file": (filename or "audio.webm", audio, content_type or "audio/webm")}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(GROQ_URL, headers={"Authorization": f"Bearer {settings.groq_api_key}"}, data=data, files=files)
    ok = r.status_code < 400
    with tx() as c:
        c.execute("INSERT OR IGNORE INTO provider_usage (provider, day) VALUES ('groq-stt', ?)", (today(),))
        c.execute("UPDATE provider_usage SET requests = requests + 1, errors = errors + ? WHERE provider = 'groq-stt' AND day = ?", (0 if ok else 1, today()))
    if not ok:
        # Their outage should cost a student a slower answer, not a lost question.
        if local_available():
            return await _transcribe_local(audio, lang_hint, vocab)
        raise HTTPException(502, f"stt_upstream_{r.status_code}")
    j = r.json()
    return {"text": (j.get("text") or "").strip(), "language": j.get("language"), "duration": j.get("duration")}
