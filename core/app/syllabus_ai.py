"""AI-assisted syllabus parsing: turns messy, unstructured pasted text into the same
{title, lessons: [...]}-per-week shape web/src/lib/syllabus.ts already produces for clean
"Week N" + numbered-list input. Same local LLM router the Tutor's chat uses — no new
dependency, no new key, and local-qwen's priority keeps this free and quota-free like
everything else. This is a fallback path: the fast, free, instant regex parser stays the
default in the admin UI and is tried first; this only runs when that finds nothing.
"""
from __future__ import annotations

import json
import re

from .router import ProviderError, router

SYSTEM_PROMPT = """You turn raw, possibly messy pasted course syllabus text into strict JSON. \
Output ONLY a JSON object — no markdown fences, no commentary, nothing before or after it.

Shape: {"weeks": [{"title": "Week 1", "lessons": ["Lesson title", "Lesson title"]}]}

Rules:
- Infer week/module groupings from the text's own structure (headings, numbering, blank \
lines, topic shifts). If there is truly no grouping, put everything under one "Week 1".
- Each lesson title is short and free of numbering prefixes ("1.", "-", "*") and trailing \
punctuation.
- Keep titles in whatever language the source text uses — never translate them.
- Drop any week that ends up with zero lessons.
- If the text has nothing resembling a syllabus, return {"weeks": []}.
"""


class SyllabusAIError(RuntimeError):
    def __init__(self, message: str, *, code: str = "syllabus_ai_error"):
        super().__init__(message)
        self.code = code


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text.strip(), re.DOTALL)
    if not m:
        raise SyllabusAIError("The model didn't return JSON.", code="syllabus_ai_bad_output")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise SyllabusAIError(f"The model's JSON didn't parse: {e}", code="syllabus_ai_bad_output") from e


async def ai_parse_syllabus(text: str) -> list[dict]:
    """Returns [{"title": str, "lessons": [str, ...]}, ...]."""
    if not router.enabled:
        raise SyllabusAIError("No LLM is configured — check LOCAL_LLM_ENABLED in core/.env.", code="syllabus_ai_unavailable")
    est_tokens = len(text) // 3 + 800
    provider = router.pick("en", est_tokens)
    if provider is None:
        raise SyllabusAIError(
            "The local model is busy right now — try again in a moment, or paste in a plain "
            "\"Week N\" + numbered-list format.",
            code="syllabus_ai_busy",
        )
    provider.inflight += 1
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text[:8000]},
    ]
    prompt_tokens = sum(len(m["content"]) for m in messages) // 3
    buf: list[str] = []
    try:
        async for delta in router.stream(provider, messages, prompt_tokens):
            buf.append(delta)
    except ProviderError as e:
        raise SyllabusAIError(f"The local model failed: {e}", code="syllabus_ai_upstream_error") from e
    data = _extract_json("".join(buf))
    weeks = data.get("weeks")
    if not isinstance(weeks, list):
        raise SyllabusAIError("Unexpected shape from the model.", code="syllabus_ai_bad_output")
    out = []
    for w in weeks:
        if not isinstance(w, dict):
            continue
        title = str(w.get("title") or "").strip()
        lessons = [str(x).strip() for x in (w.get("lessons") or []) if str(x).strip()]
        if title and lessons:
            out.append({"title": title, "lessons": lessons})
    return out
