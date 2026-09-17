"""Captions always come back in English or Arabic regardless of a video's real spoken
language. The LLM call is mocked at router.stream, same pattern as test_chat_pipeline.py's
fake_llm; corpus_dir is redirected to a tmp_path so nothing touches the real corpus/.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import captions_translate as ct
from app.captions_translate import TranslateError, cues_to_vtt, get_cues

CUES = [
    {"start": 0.0, "end": 1.0, "text": "வணக்கம்"},
    {"start": 1.0, "end": 2.0, "text": "இது ஒரு பாடம்"},
]


@pytest.fixture(autouse=True)
def isolated_corpus_dir(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_dir", tmp_path)
    (tmp_path / "transcripts").mkdir()
    (tmp_path / "transcripts" / "vid1.cues.json").write_text(json.dumps(CUES), encoding="utf-8")
    return tmp_path


def _set_source_lang(monkeypatch, lang):
    from app.corpus import Video, corpus

    monkeypatch.setattr(corpus, "videos", {"vid1": Video(id="vid1", title="x", source="youtube", lang=lang)})


def fake_llm(reply):
    """See test_chat_pipeline.py's fake_llm — provider.inflight must be released."""
    async def stream(provider, messages, est_tokens):
        try:
            for piece in reply.split(" "):
                yield piece + " "
        finally:
            provider.inflight -= 1

    return stream


def must_not_run(*a, **k):
    raise AssertionError("router.stream should not have been called")


def test_same_language_as_source_skips_translation_entirely(monkeypatch):
    _set_source_lang(monkeypatch, "en")
    monkeypatch.setattr(ct.router, "stream", must_not_run)
    cues = asyncio.run(get_cues("vid1", "en"))
    assert cues == CUES


def test_translates_unknown_source_language_and_caches(monkeypatch, isolated_corpus_dir):
    _set_source_lang(monkeypatch, "ta")  # Tamil — neither of the two caption targets
    monkeypatch.setattr(ct.router, "stream", fake_llm(
        '["Hello", "This is a lesson"]',
    ))
    cues = asyncio.run(get_cues("vid1", "en"))
    assert [c["text"] for c in cues] == ["Hello", "This is a lesson"]
    assert cues[0]["start"] == 0.0 and cues[1]["end"] == 2.0
    assert (isolated_corpus_dir / "transcripts" / "vid1.en.cues.json").exists()


def test_cache_hit_never_calls_the_llm(monkeypatch, isolated_corpus_dir):
    _set_source_lang(monkeypatch, "ta")
    cached = [{"start": 0.0, "end": 1.0, "text": "Hi"}, {"start": 1.0, "end": 2.0, "text": "Lesson"}]
    (isolated_corpus_dir / "transcripts" / "vid1.en.cues.json").write_text(json.dumps(cached), encoding="utf-8")
    monkeypatch.setattr(ct.router, "stream", must_not_run)
    assert asyncio.run(get_cues("vid1", "en")) == cached


def test_a_failed_translation_falls_back_to_original_text_and_is_not_cached(monkeypatch, isolated_corpus_dir):
    _set_source_lang(monkeypatch, "ta")
    monkeypatch.setattr(ct.router, "pick", lambda lang, est: None)  # no provider available at all
    cues = asyncio.run(get_cues("vid1", "en"))
    assert [c["text"] for c in cues] == [c["text"] for c in CUES]  # original text, not lost
    assert cues[0]["start"] == 0.0 and cues[1]["end"] == 2.0  # timestamps still intact
    assert not (isolated_corpus_dir / "transcripts" / "vid1.en.cues.json").exists()


def test_unsupported_target_language_is_a_typed_error(monkeypatch):
    _set_source_lang(monkeypatch, "ta")
    with pytest.raises(TranslateError) as e:
        asyncio.run(get_cues("vid1", "fr"))
    assert e.value.code == "captions_translate_bad_lang"


def test_missing_transcript_is_a_typed_error():
    with pytest.raises(TranslateError) as e:
        asyncio.run(get_cues("no-such-video", "en"))
    assert e.value.code == "no_transcript"


def test_cues_to_vtt_basic_format():
    vtt = cues_to_vtt([{"start": 0.0, "end": 1.5, "text": "Hi"}])
    assert vtt.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:01.500" in vtt
    assert "Hi" in vtt
