"""Captions/subtitles always come back in English or Arabic, whatever language a video's
audio actually is — an admin-connected YouTube playlist can be in any language (Tamil,
French, anything), and this project's caption UI only ever offers the two. Same local LLM
the Tutor's chat already uses (no new key); translated once per (video, target language)
and cached to disk, so re-watching or another student never re-translates.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import settings
from .router import ProviderError, router

TARGET_LANGS = ("en", "ar")
_BATCH = 40  # cues per call — small enough that the model reliably keeps the exact count
_LANG_NAMES = {"en": "English", "ar": "Arabic"}


class TranslateError(RuntimeError):
    def __init__(self, message: str, *, code: str = "captions_translate_error"):
        super().__init__(message)
        self.code = code


def _cues_path(video_id: str) -> Path:
    return settings.corpus_dir / "transcripts" / f"{video_id}.cues.json"


def _cache_path(video_id: str, lang: str) -> Path:
    return settings.corpus_dir / "transcripts" / f"{video_id}.{lang}.cues.json"


def _source_lang(video_id: str) -> str:
    from .corpus import corpus

    v = corpus.videos.get(video_id)
    return (v.lang if v and v.lang else None) or "en"


async def _translate_batch(texts: list[str], target: str) -> list[str]:
    system = (
        f"Translate each of the following {len(texts)} subtitle lines into {_LANG_NAMES[target]}. "
        f"Reply with ONLY a JSON array of exactly {len(texts)} strings, same order, one "
        "translated line per input line — never merge, split, or drop a line. No commentary, "
        "no markdown fences."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(texts, ensure_ascii=False)},
    ]
    prompt_tokens = sum(len(m["content"]) for m in messages) // 3
    provider = router.pick(target, prompt_tokens + 800)
    if provider is None:
        raise TranslateError("local model is busy", code="captions_translate_busy")
    provider.inflight += 1
    buf: list[str] = []
    try:
        async for delta in router.stream(provider, messages, prompt_tokens):
            buf.append(delta)
    except ProviderError as e:
        raise TranslateError(str(e), code="captions_translate_upstream_error") from e
    m = re.search(r"\[.*\]", "".join(buf), re.DOTALL)
    if not m:
        raise TranslateError("model returned no JSON array", code="captions_translate_bad_output")
    try:
        out = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise TranslateError(f"bad JSON: {e}", code="captions_translate_bad_output") from e
    if not isinstance(out, list) or len(out) != len(texts):
        raise TranslateError("translation count mismatch", code="captions_translate_bad_output")
    return [str(x) for x in out]


async def get_cues(video_id: str, target_lang: str) -> list[dict]:
    """Cues in `target_lang` ("en" or "ar") regardless of the video's real spoken language."""
    if target_lang not in TARGET_LANGS:
        raise TranslateError(f"unsupported target language: {target_lang}", code="captions_translate_bad_lang")
    src = _cues_path(video_id)
    if not src.exists():
        raise TranslateError("no transcript for this video", code="no_transcript")
    cues = json.loads(src.read_text(encoding="utf-8"))
    if _source_lang(video_id) == target_lang:
        return cues  # already the right language — the common case, zero LLM calls
    cache = _cache_path(video_id, target_lang)
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))

    texts = [c["text"] for c in cues]
    translated: list[str] = []
    fully_translated = True
    for i in range(0, len(texts), _BATCH):
        batch = texts[i:i + _BATCH]
        try:
            translated.extend(await _translate_batch(batch, target_lang))
        except TranslateError:
            # Never let a translation hiccup break the caption timeline — fall back to the
            # original text for this batch. Not caching the result means the next viewer
            # gets a fresh attempt instead of being stuck with a half-translated file forever.
            translated.extend(batch)
            fully_translated = False
    out = [{"start": c["start"], "end": c["end"], "text": tr} for c, tr in zip(cues, translated)]
    if fully_translated:
        cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def _ts(s: float) -> str:
    h, rem = divmod(max(0.0, s), 3600)
    m, sec = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{sec:06.3f}"


def cues_to_vtt(cues: list[dict]) -> str:
    lines = ["WEBVTT", ""]
    for c in cues:
        lines.append(f"{_ts(c['start'])} --> {_ts(c['end'])}")
        lines.append(c["text"])
        lines.append("")
    return "\n".join(lines)
