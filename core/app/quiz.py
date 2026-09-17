"""Practice questions per lesson, generated from the lecture's own transcript chunks via the
local LLM — same grounding principle as the Tutor's chat: every question must reference one
specific excerpt of the actual transcript by label, so the answer (and the timestamp shown
for "review this") comes from what the lecture really says, never the model's own guess at
what a course "probably" covers.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import settings
from .corpus import corpus
from .router import ProviderError, router

N_QUESTIONS = 5
MAX_EXCERPTS = 12


class QuizError(RuntimeError):
    def __init__(self, message: str, *, code: str = "quiz_error"):
        super().__init__(message)
        self.code = code


def _cache_path(video_id: str) -> Path:
    return settings.corpus_dir / "quizzes" / f"{video_id}.json"


def _excerpts_for(video_id: str) -> list[dict]:
    """Evenly-spaced chunks across the whole lecture, not just the first N — a quiz drawn
    only from the opening minutes would miss most of the material."""
    chunks = [c for c in corpus.chunks if c.video_id == video_id]
    if not chunks:
        return []
    if len(chunks) > MAX_EXCERPTS:
        step = len(chunks) / MAX_EXCERPTS
        chunks = [chunks[int(i * step)] for i in range(MAX_EXCERPTS)]
    return [{"ref": f"E{i + 1}", "t": c.t_start, "text": c.text} for i, c in enumerate(chunks)]


def _extract_json_array(text: str) -> list:
    m = re.search(r"\[.*\]", text.strip(), re.DOTALL)
    if not m:
        raise QuizError("model returned no JSON array", code="quiz_bad_output")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise QuizError(f"model's JSON didn't parse: {e}", code="quiz_bad_output") from e


async def get_quiz(video_id: str) -> list[dict]:
    """Returns [{"question": str, "options": [str, ...], "answer_index": int, "t": float}].
    Cached to disk after first generation — the questions don't change unless the transcript
    does, so every later student gets the cached set instantly."""
    cache = _cache_path(video_id)
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    excerpts = _excerpts_for(video_id)
    if not excerpts:
        raise QuizError("no transcript chunks for this video", code="no_transcript")
    if not router.enabled:
        raise QuizError("no LLM is configured — check LOCAL_LLM_ENABLED in core/.env", code="quiz_unavailable")

    body = "\n\n".join(f"[{e['ref']}] {e['text']}" for e in excerpts)
    system = (
        f"You write {N_QUESTIONS} multiple-choice practice questions from the lecture excerpts "
        "below, for a student reviewing the material. Each question must be answerable from "
        "exactly one excerpt, referencing that excerpt's label. Reply with ONLY a JSON array, "
        "no markdown fences, no commentary, of exactly this shape: "
        '[{"question": "...", "options": ["...", "...", "...", "..."], "answer_index": 0, '
        '"excerpt": "E3"}] — four options each, answer_index is the 0-based index of the '
        "correct option, and vary which position the correct answer sits in across questions."
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": body[:8000]}]
    prompt_tokens = sum(len(m["content"]) for m in messages) // 3
    provider = router.pick("en", prompt_tokens + 800)
    if provider is None:
        raise QuizError("the local model is busy right now — try again shortly", code="quiz_busy")
    provider.inflight += 1
    buf: list[str] = []
    try:
        async for delta in router.stream(provider, messages, prompt_tokens):
            buf.append(delta)
    except ProviderError as e:
        raise QuizError(str(e), code="quiz_upstream_error") from e

    raw = _extract_json_array("".join(buf))
    by_ref = {e["ref"]: e["t"] for e in excerpts}
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        q, opts, ai, ref = item.get("question"), item.get("options"), item.get("answer_index"), item.get("excerpt")
        if not (isinstance(q, str) and q.strip() and isinstance(opts, list) and len(opts) >= 2 and isinstance(ai, int) and 0 <= ai < len(opts)):
            continue
        out.append({"question": q.strip(), "options": [str(o) for o in opts], "answer_index": ai, "t": by_ref.get(ref, excerpts[0]["t"])})
    if not out:
        raise QuizError("the model produced no usable questions", code="quiz_bad_output")

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out
