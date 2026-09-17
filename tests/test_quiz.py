"""Per-lesson practice questions: the LLM call is mocked at router.stream, same pattern as
test_syllabus_ai.py; corpus_dir is redirected to a tmp_path so nothing touches the real
corpus/, and corpus.chunks is populated with real Chunk objects rather than a real corpus.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import quiz
from app.corpus import Chunk, corpus
from app.quiz import QuizError, get_quiz


@pytest.fixture(autouse=True)
def isolated_corpus_dir(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_dir", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def some_chunks(monkeypatch):
    chunks = [Chunk(chunk_id=f"vid1-{i}", video_id="vid1", t_start=float(i * 60), t_end=float(i * 60 + 60), text=f"Chunk {i} content") for i in range(3)]
    monkeypatch.setattr(corpus, "chunks", chunks)
    return chunks


def fake_llm(reply):
    """See test_syllabus_ai.py's fake_llm — provider.inflight must be released."""
    async def stream(provider, messages, est_tokens):
        try:
            for piece in reply.split(" "):
                yield piece + " "
        finally:
            provider.inflight -= 1

    return stream


def test_generates_and_caches_grounded_questions(monkeypatch, isolated_corpus_dir):
    monkeypatch.setattr(quiz.router, "stream", fake_llm(json.dumps([
        {"question": "What is in chunk 1?", "options": ["Chunk 1 content", "wrong", "wrong2", "wrong3"], "answer_index": 0, "excerpt": "E2"},
    ])))
    out = asyncio.run(get_quiz("vid1"))
    assert len(out) == 1
    assert out[0]["question"] == "What is in chunk 1?"
    assert out[0]["answer_index"] == 0
    assert out[0]["t"] == 60.0  # E2 == chunks[1], t_start = 60
    assert (isolated_corpus_dir / "quizzes" / "vid1.json").exists()


def test_cache_hit_never_calls_the_llm(monkeypatch, isolated_corpus_dir):
    cached = [{"question": "cached?", "options": ["yes", "no"], "answer_index": 0, "t": 0.0}]
    (isolated_corpus_dir / "quizzes").mkdir()
    (isolated_corpus_dir / "quizzes" / "vid1.json").write_text(json.dumps(cached), encoding="utf-8")

    def must_not_run(*a, **k):
        raise AssertionError("router.stream should not have been called")

    monkeypatch.setattr(quiz.router, "stream", must_not_run)
    assert asyncio.run(get_quiz("vid1")) == cached


def test_no_chunks_for_video_is_a_typed_error():
    with pytest.raises(QuizError) as e:
        asyncio.run(get_quiz("no-such-video"))
    assert e.value.code == "no_transcript"


def test_router_disabled_is_a_typed_error(monkeypatch):
    monkeypatch.setattr(quiz.router, "providers", [])
    with pytest.raises(QuizError) as e:
        asyncio.run(get_quiz("vid1"))
    assert e.value.code == "quiz_unavailable"


def test_no_provider_available_is_a_typed_error(monkeypatch):
    monkeypatch.setattr(quiz.router, "pick", lambda lang, est: None)
    with pytest.raises(QuizError) as e:
        asyncio.run(get_quiz("vid1"))
    assert e.value.code == "quiz_busy"


def test_garbage_model_output_is_a_typed_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(quiz.router, "stream", fake_llm("not json, sorry"))
    with pytest.raises(QuizError) as e:
        asyncio.run(get_quiz("vid1"))
    assert e.value.code == "quiz_bad_output"


def test_malformed_items_are_dropped_not_crashed_on(monkeypatch):
    monkeypatch.setattr(quiz.router, "stream", fake_llm(json.dumps([
        {"question": "good one", "options": ["a", "b"], "answer_index": 0, "excerpt": "E1"},
        {"question": "missing options"},
        {"question": "bad index", "options": ["a", "b"], "answer_index": 5, "excerpt": "E1"},
    ])))
    out = asyncio.run(get_quiz("vid1"))
    assert len(out) == 1
    assert out[0]["question"] == "good one"


def test_more_chunks_than_the_excerpt_cap_still_samples_across_the_whole_video(monkeypatch):
    chunks = [Chunk(chunk_id=f"vid2-{i}", video_id="vid2", t_start=float(i * 30), t_end=float(i * 30 + 30), text=f"c{i}") for i in range(40)]
    monkeypatch.setattr(corpus, "chunks", chunks)
    excerpts = quiz._excerpts_for("vid2")
    assert len(excerpts) == quiz.MAX_EXCERPTS
    # Spans close to the full video, not just the first MAX_EXCERPTS chunks (which would all
    # be under t=360s here).
    assert excerpts[-1]["t"] > 900.0
