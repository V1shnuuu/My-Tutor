"""Which engine answers a spoken question, and what happens when it can't.

Regression cover for the keyless install: before the local lane existed, an install with
no GROQ_API_KEY reported STT unavailable and sent every student to the browser's
recogniser — the weakest option for Egyptian Arabic.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException


@pytest.fixture
def stt(settings):
    from app import stt as mod

    before = (settings.groq_api_key, settings.local_stt_enabled)
    yield mod
    settings.groq_api_key, settings.local_stt_enabled = before


def test_groq_wins_when_it_has_a_key(stt, settings, monkeypatch):
    settings.groq_api_key = "gsk-test"
    settings.local_stt_enabled = True
    monkeypatch.setattr(stt, "local_available", lambda: True)
    b = stt.budget()
    assert b["engine"] == "groq"
    assert b["available"] is True


def test_local_serves_a_keyless_install(stt, settings, monkeypatch):
    settings.groq_api_key = ""
    settings.local_stt_enabled = True
    monkeypatch.setattr(stt, "local_available", lambda: True)
    b = stt.budget()
    assert b["engine"] == "local"
    # The client reads `available` to decide whether to upload at all. False here is what
    # used to push every keyless install onto the browser recogniser.
    assert b["available"] is True
    assert b["local"] is True


def test_no_engine_when_both_are_off(stt, settings, monkeypatch):
    settings.groq_api_key = ""
    settings.local_stt_enabled = False
    monkeypatch.setattr(stt, "local_available", lambda: False)
    b = stt.budget()
    assert b["engine"] == "none"
    assert b["available"] is False


def test_groq_daily_cap_falls_back_to_local(stt, settings, monkeypatch):
    settings.groq_api_key = "gsk-test"
    monkeypatch.setattr(stt, "local_available", lambda: True)
    monkeypatch.setattr(stt, "query", lambda *a, **k: [{"requests": settings.stt_daily_cap}])
    b = stt.budget()
    assert b["engine"] == "local", "a spent Groq budget must hand over, not go dark"
    assert b["available"] is True


def test_transcribe_uses_local_when_that_is_the_engine(stt, settings, monkeypatch):
    settings.groq_api_key = ""
    monkeypatch.setattr(stt, "local_available", lambda: True)
    called = {}

    async def fake_local(audio, lang_hint, vocab):
        called["args"] = (audio, lang_hint, vocab)
        return {"text": "مرحبا", "language": "ar", "duration": 1.0}

    monkeypatch.setattr(stt, "_transcribe_local", fake_local)
    out = asyncio.run(stt.transcribe(b"audio", "q.webm", "audio/webm", "ar", "peak finding"))
    assert out["text"] == "مرحبا"
    # Course jargon must prime the local lane too, exactly as it primes Groq.
    assert called["args"][2] == "peak finding"


def test_transcribe_refuses_when_no_engine(stt, settings, monkeypatch):
    settings.groq_api_key = ""
    monkeypatch.setattr(stt, "local_available", lambda: False)
    with pytest.raises(HTTPException) as e:
        asyncio.run(stt.transcribe(b"audio", "q.webm", "audio/webm", "ar", ""))
    assert e.value.status_code == 429


def test_local_model_failure_is_503_not_a_crash(stt, monkeypatch):
    """A missing model must be a typed error the client can act on, not a bare 500."""
    def boom(*a, **k):
        raise RuntimeError("model weights not downloaded")

    monkeypatch.setattr(stt, "_transcribe_local_sync", boom)
    with pytest.raises(HTTPException) as e:
        asyncio.run(stt._transcribe_local(b"audio", "ar", ""))
    assert e.value.status_code == 503
    assert e.value.detail == "stt_local_failed"
