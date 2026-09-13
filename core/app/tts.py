"""Server-side TTS with Piper (VITS, CPU, ~0.1–0.2 s per sentence). Used by the client when the
device has no usable voice for the language (Arabic on most phones/laptops) — see web/src/lib/speech.ts.
Voices live in DATA_DIR/voices (download once: `python -m app.cli voices`). Everything is optional:
without piper or voices, /tts/voices reports false and the client keeps using the OS voice.

Egyptian Arabic note (docs §5.4): ar_JO-kareem is MSA-accented. Undiacritised text gets
mis-vowelled, so Arabic passes through Mishkal (rule-based tashkeel) when installed.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import re
import threading
import wave
from collections import OrderedDict
from pathlib import Path

from .config import settings

VOICES = {"ar": "ar_JO-kareem-medium", "en": "en_US-lessac-medium", "fr": "fr_FR-siwis-medium"}
_voices: dict[str, object] = {}
_lock = threading.Lock()
_sem = asyncio.Semaphore(3)  # protect the 4-core VM: at most 3 syntheses in flight
_cache: "OrderedDict[str, tuple[bytes, float]]" = OrderedDict()
_CACHE_MAX = 600


def voice_dir() -> Path:
    d = settings.data_dir / "voices"
    d.mkdir(parents=True, exist_ok=True)
    return d


def available(lang: str) -> bool:
    try:
        import piper  # noqa: F401
    except Exception:
        return False
    name = VOICES.get(lang)
    return bool(name) and (voice_dir() / f"{name}.onnx").exists() and (voice_dir() / f"{name}.onnx.json").exists()


def availability() -> dict[str, bool]:
    return {lang: available(lang) for lang in VOICES}


def _load(lang: str):
    with _lock:
        if lang not in _voices:
            from piper import PiperVoice

            name = VOICES[lang]
            _voices[lang] = PiperVoice.load(str(voice_dir() / f"{name}.onnx"), config_path=str(voice_dir() / f"{name}.onnx.json"))
        return _voices[lang]


_diacritizer = None


def prepare_text(text: str, lang: str) -> str:
    t = re.sub(r"\[C\d+\]", " ", text)
    t = re.sub(r"[*_`#>|•▶]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if lang == "ar":
        global _diacritizer
        try:
            if _diacritizer is None:
                import mishkal.tashkeel

                _diacritizer = mishkal.tashkeel.TashkeelClass()
            t = _diacritizer.tashkeel(t)
        except Exception:
            pass  # mishkal missing or failed: speak undiacritised
    return t


def _synth_sync(text: str, lang: str) -> tuple[bytes, float]:
    voice = _load(lang)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        voice.synthesize_wav(text, wf)
    data = buf.getvalue()
    with wave.open(io.BytesIO(data), "rb") as wf:
        duration = wf.getnframes() / float(wf.getframerate())
    return data, duration


async def synthesize(text: str, lang: str) -> tuple[bytes, float]:
    t = prepare_text(text, lang)
    key = hashlib.sha1(f"{lang}|{t}".encode()).hexdigest()
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    async with _sem:
        wav, dur = await asyncio.to_thread(_synth_sync, t, lang)
    _cache[key] = (wav, dur)
    if len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)
    return wav, dur
