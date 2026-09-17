"""AI-assisted syllabus parsing: the LLM call is mocked at router.stream, same pattern as
test_chat_pipeline.py's fake_llm — no real model, no real network.
"""
from __future__ import annotations

import asyncio

import pytest

from app import syllabus_ai
from app.syllabus_ai import SyllabusAIError, ai_parse_syllabus


def fake_llm(text):
    """See test_chat_pipeline.py's fake_llm — provider.inflight must be released in the
    stand-in the same way the real router.stream() releases it in its finally."""
    async def stream(provider, messages, est_tokens):
        try:
            for piece in text.split(" "):
                yield piece + " "
        finally:
            provider.inflight -= 1

    return stream


def test_parses_a_clean_model_response(monkeypatch):
    monkeypatch.setattr(syllabus_ai.router, "stream", fake_llm(
        '{"weeks": [{"title": "Week 1", "lessons": ["Peak Finding", "Document Distance"]}]}',
    ))
    weeks = asyncio.run(ai_parse_syllabus("some messy pasted text"))
    assert weeks == [{"title": "Week 1", "lessons": ["Peak Finding", "Document Distance"]}]


def test_strips_a_markdown_fence_the_model_added_anyway(monkeypatch):
    monkeypatch.setattr(syllabus_ai.router, "stream", fake_llm(
        '```json\n{"weeks": [{"title": "Week 1", "lessons": ["A"]}]}\n```',
    ))
    weeks = asyncio.run(ai_parse_syllabus("x"))
    assert weeks == [{"title": "Week 1", "lessons": ["A"]}]


def test_drops_weeks_with_no_lessons(monkeypatch):
    monkeypatch.setattr(syllabus_ai.router, "stream", fake_llm(
        '{"weeks": [{"title": "Empty", "lessons": []}, {"title": "Week 1", "lessons": ["A"]}]}',
    ))
    weeks = asyncio.run(ai_parse_syllabus("x"))
    assert weeks == [{"title": "Week 1", "lessons": ["A"]}]


def test_garbage_model_output_is_a_typed_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(syllabus_ai.router, "stream", fake_llm("not json at all, sorry"))
    with pytest.raises(SyllabusAIError) as e:
        asyncio.run(ai_parse_syllabus("x"))
    assert e.value.code == "syllabus_ai_bad_output"


def test_no_provider_available_is_a_typed_error(monkeypatch):
    monkeypatch.setattr(syllabus_ai.router, "pick", lambda lang, est: None)
    with pytest.raises(SyllabusAIError) as e:
        asyncio.run(ai_parse_syllabus("x"))
    assert e.value.code == "syllabus_ai_busy"


def test_router_disabled_is_a_typed_error(monkeypatch):
    monkeypatch.setattr(syllabus_ai.router, "providers", [])
    with pytest.raises(SyllabusAIError) as e:
        asyncio.run(ai_parse_syllabus("x"))
    assert e.value.code == "syllabus_ai_unavailable"
