"""The answer path: gate, grounding, cache, and failing fast.

These are the guarantees the product is sold on — answers come only from the lectures,
every claim carries a timestamp, and an off-topic question is refused. They are enforced
in code rather than in a prompt, so they are testable, so they are tested.
"""
from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest


def drive(chat_mod, message, qvec, history=None, prev_lang=None):
    """Run one question through run_chat and sort the SSE stream into something assertable."""
    chat_mod.embed_queries = lambda texts: np.repeat(qvec, len(texts), axis=0)

    async def go():
        events = []
        async for raw in chat_mod.run_chat("test-student", message, history or [], prev_lang,
                                           {"used": 0, "cap": 60}):
            events.append(json.loads(raw[5:].strip()))
        return events

    events = asyncio.run(go())
    answer = "".join(e["text"] for e in events if e.get("type") == "token")
    replaced = [e["text"] for e in events if e.get("type") == "replace"]
    if replaced:
        answer = replaced[-1]
    done = next((e for e in events if e.get("type") == "done"), None)
    return {
        "events": events,
        "answer": answer,
        "replaced": bool(replaced),
        "source": done.get("source") if done else None,
        "stages": [e["stage"] for e in events if e.get("type") == "status"],
        "citations": next((e["items"] for e in events if e.get("type") == "citations"), []),
        "meta": next((e for e in events if e.get("type") == "meta"), None),
        "error": next((e for e in events if e.get("type") == "error"), None),
    }


@pytest.fixture
def chat(loaded_corpus, monkeypatch):
    from app import chat as mod
    from app.cache import semantic_cache

    # A fresh cache per test: a hit leaking between tests would make one of them lie.
    semantic_cache.ids, semantic_cache.langs = [], []
    semantic_cache.vecs = np.zeros((0, 768), dtype=np.float32)
    monkeypatch.setattr(semantic_cache, "store", lambda *a, **k: None, raising=False)
    return mod


@pytest.fixture
def caching_chat(loaded_corpus):
    """Like `chat`, but with the real cache so store/lookup can be exercised together."""
    from app import chat as mod
    from app.cache import semantic_cache

    semantic_cache.ids, semantic_cache.langs = [], []
    semantic_cache.vecs = np.zeros((0, 768), dtype=np.float32)
    return mod, semantic_cache


def fake_llm(text):
    """Stand in for the router's stream, one word at a time like a real one.

    The caller increments provider.inflight and the real stream() releases it in its
    finally, so a stand-in has to release it too — otherwise the provider looks
    saturated to every later test and they silently fall through to the extractive floor.
    """
    async def stream(provider, messages, est_tokens):
        try:
            for piece in text.split(" "):
                yield piece + " "
        finally:
            provider.inflight -= 1

    return stream


def test_on_topic_question_reaches_the_model_and_cites(chat, fixed_query_vector, monkeypatch):
    monkeypatch.setattr(chat.router, "stream", fake_llm("A peak is a local maximum [C1]"))
    r = drive(chat, "What is a peak?", fixed_query_vector(10))
    assert r["source"] == "llm"
    assert "retrieving" in r["stages"]
    assert r["citations"], "a grounded answer must arrive with citations to jump to"
    assert all("t" in c and "video_id" in c for c in r["citations"])


def test_off_topic_is_refused_at_the_gate(chat, loaded_corpus, monkeypatch):
    called = {"llm": False}

    async def must_not_run(*a, **k):
        called["llm"] = True
        yield ""

    monkeypatch.setattr(chat.router, "stream", must_not_run)
    # A vector pointing nowhere near the lecture: every dense score stays under the gate.
    off = np.zeros((1, loaded_corpus.vecs.shape[1]), dtype=np.float32)
    off[0, 0] = 1.0
    r = drive(chat, "What is the capital of France?", off)

    assert r["source"] == "refusal"
    assert "refusal" in r["stages"]
    assert called["llm"] is False, "an off-topic question must not cost an LLM call"


def test_ungrounded_answer_is_replaced_not_delivered(chat, fixed_query_vector, monkeypatch):
    """The core promise. A model answer with no [C…] marker is not from the lectures, so
    the student gets the extracts instead — never the uncited text."""
    monkeypatch.setattr(chat.router, "stream", fake_llm("Peaks are interesting in general."))
    r = drive(chat, "Explain the divide and conquer step", fixed_query_vector(30))

    assert r["replaced"] is True
    assert "Peaks are interesting in general." not in r["answer"]
    assert "—" in r["answer"] or ":" in r["answer"], "the replacement quotes the lecture"


def test_ungrounded_answer_is_never_cached(caching_chat, fixed_query_vector, monkeypatch):
    chat, cache = caching_chat
    monkeypatch.setattr(chat.router, "stream", fake_llm("No citation here at all."))
    drive(chat, "Explain the divide and conquer step", fixed_query_vector(30))
    assert cache.vecs.shape[0] == 0, "caching an uncited answer would serve it again for free"


def test_a_cited_answer_is_cached_and_served_from_cache(caching_chat, fixed_query_vector, monkeypatch):
    chat, cache = caching_chat
    monkeypatch.setattr(chat.router, "stream", fake_llm("A peak is a local maximum [C1]"))

    first = drive(chat, "How does divide and conquer find a peak?", fixed_query_vector(10))
    assert first["source"] == "llm"
    assert cache.vecs.shape[0] == 1

    async def must_not_run(*a, **k):
        raise AssertionError("a cache hit must not reach the model")
        yield ""

    monkeypatch.setattr(chat.router, "stream", must_not_run)
    second = drive(chat, "How does divide and conquer find a peak?", fixed_query_vector(10))
    assert second["source"] == "cache"
    assert second["answer"].strip() == first["answer"].strip()


def test_language_follows_the_question(chat, fixed_query_vector, monkeypatch):
    monkeypatch.setattr(chat.router, "stream", fake_llm("إجابة [C1]"))
    ar = drive(chat, "يعني إيه peak في المحاضرة؟", fixed_query_vector(25))
    assert ar["meta"]["lang"] == "ar"

    monkeypatch.setattr(chat.router, "stream", fake_llm("An answer [C1]"))
    en = drive(chat, "What is a peak in the lecture?", fixed_query_vector(25))
    assert en["meta"]["lang"] == "en"


def test_missing_encoder_fails_fast_instead_of_hanging(chat, monkeypatch):
    """Retrieval is dense-first and the dense score is also the gate signal, so there is
    no honest degraded answer here — it must say so at once rather than spin."""
    from app.embed import EmbeddingUnavailable

    def unavailable(texts):
        raise EmbeddingUnavailable("weights not downloaded")

    chat.embed_queries = unavailable

    async def go():
        return [json.loads(raw[5:].strip())
                async for raw in chat.run_chat("s", "What is a peak?", [], None, {"used": 0, "cap": 60})]

    events = asyncio.run(go())
    err = next((e for e in events if e.get("type") == "error"), None)
    assert err is not None and err["code"] == "embeddings_unavailable"
    assert not any(e.get("type") == "done" for e in events)


def test_blocked_question_never_reaches_retrieval(chat, monkeypatch):
    monkeypatch.setattr(chat, "is_blocked", lambda m: True)

    def must_not_embed(texts):
        raise AssertionError("a blocked question must be refused before embedding")

    chat.embed_queries = must_not_embed
    r = drive(chat, "something the guard rejects", np.zeros((1, 768), dtype=np.float32))
    assert r["source"] == "refusal"
