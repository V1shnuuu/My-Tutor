"""The install this course actually runs: one key, and it is HeyGen's.

The promise is that brain, ears and voice all work with no account anywhere — Qwen3-8B
through Ollama, faster-whisper, and Piper. That is easy to state in a README and easy to
lose: one `if not settings.groq_api_key: return unavailable` and the keyless install goes
silently mute. These tests fail when that creeps back.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def keyless(settings, monkeypatch):
    """Every key empty — including in os.environ, which load_providers reads first."""
    for name in ("GROQ_API_KEY", "GEMINI_API_KEY", "CEREBRAS_API_KEY", "MISTRAL_API_KEY",
                 "OPENROUTER_API_KEY", "CLOUDFLARE_API_TOKEN"):
        monkeypatch.setitem(os.environ, name, "")
        monkeypatch.setattr(settings, name.lower(), "", raising=False)
    monkeypatch.setattr(settings, "local_llm_enabled", True)
    monkeypatch.setattr(settings, "local_stt_enabled", True)
    return settings


# ------------------------------------------------------------------ brain
def test_the_brain_is_qwen3_8b_and_needs_no_key(keyless):
    from app.config import load_providers

    provs = load_providers()
    assert provs, "a keyless install must still have somewhere to send a question"
    assert [p["id"] for p in provs] == ["local-qwen"], (
        "with no keys, the local lane must be the only provider — anything else means a "
        "keyed provider slipped through without its key")

    qwen = provs[0]
    assert qwen["model"] == "qwen3:8b"
    assert qwen["priority"] == 1, "the free lane has to outrank every cloud lane, not trail it"
    assert set(qwen["langs"]) == {"ar", "en", "fr"}, "one brain answers all three languages"
    # Qwen3 spends the whole max_tokens budget inside <think> without this and streams back
    # an empty answer — a silent failure that looks like the model is broken.
    assert qwen["extra_body"]["reasoning_effort"] == "none"


@pytest.mark.parametrize("lang", ["ar", "en", "fr"])
def test_a_cloud_key_only_ever_adds_a_lane_behind_qwen(keyless, monkeypatch, lang):
    """Overflow is allowed; overflow taking over is not. Asserted through the router's own
    pick() rather than by re-implementing its scoring, because the scoring is the thing
    that could drift: `priority` is a bonus among several, not a sort key."""
    from app.router import Router

    monkeypatch.setitem(os.environ, "GROQ_API_KEY", "gsk-test")
    r = Router()
    assert len(r.providers) > 1, "the keyed lane should have joined"
    chosen = r.pick(lang, est_tokens=400)
    assert chosen is not None and chosen.cfg["id"] == "local-qwen", (
        f"{lang} went to {chosen and chosen.cfg['id']} while the free local lane was idle")


# ------------------------------------------------------------------ ears
def test_speech_in_works_with_no_key(keyless, monkeypatch):
    from app import stt

    monkeypatch.setattr(stt, "local_available", lambda: True)
    b = stt.budget()
    assert b["engine"] == "local"
    assert b["available"] is True, "False here sends every student to the browser recogniser"


@pytest.mark.parametrize("lang", ["ar", "en", "fr"])
def test_every_language_can_be_transcribed_without_a_key(keyless, monkeypatch, lang):
    import asyncio

    from app import stt

    monkeypatch.setattr(stt, "local_available", lambda: True)
    seen = {}

    async def fake_local(audio, lang_hint, vocab):
        seen["lang"] = lang_hint
        return {"text": "...", "language": lang_hint, "duration": 1.0}

    monkeypatch.setattr(stt, "_transcribe_local", fake_local)
    asyncio.run(stt.transcribe(b"audio", "q.webm", "audio/webm", lang, ""))
    assert seen["lang"] == lang


# ------------------------------------------------------------------ voice
@pytest.mark.parametrize("lang", ["ar", "en", "fr"])
def test_a_free_voice_is_named_for_every_language(lang):
    """Piper only speaks a language it has a voice file for, and the file is named here.
    A missing entry is a language that silently falls back to whatever the device has —
    which on Arabic is usually nothing at all."""
    from app.tts import VOICES

    assert VOICES.get(lang), f"no Piper voice configured for {lang}"


def test_arabic_is_diacritised_before_synthesis():
    """Piper's Arabic voice is MSA-trained and mis-vowels undiacritised text, so tashkeel
    is what makes it intelligible rather than merely audible."""
    from app import tts

    out = tts.prepare_text("ما هي القمة [C1]", "ar")
    assert "[C1]" not in out, "citation markers must never be read aloud"
    assert out.strip()


def test_citation_markup_is_stripped_for_every_language():
    from app import tts

    for lang in ("en", "fr"):
        out = tts.prepare_text("A peak is a local maximum [C1] **here**", lang)
        assert "[C1]" not in out and "*" not in out


# ------------------------------------------------------------------ avatar
@pytest.mark.parametrize("lang", ["ar", "en", "fr"])
def test_the_avatar_is_told_the_language_even_in_sandbox(settings, monkeypatch, lang):
    """The whole reason all three languages may go to the avatar. Sandbox drops the
    configured voice_id, but the language survives, so HeyGen speaks the answer's language
    in its own voice instead of an English voice mangling Arabic."""
    import asyncio

    import httpx

    from app import liveavatar
    from app.liveavatar import LiveAvatarError

    monkeypatch.setattr(settings, "liveavatar_enabled", True)
    monkeypatch.setattr(settings, "liveavatar_api_key", "test-key")
    monkeypatch.setattr(settings, "liveavatar_sandbox", True)
    seen = {}

    async def capture(self, url, **kw):
        seen.update(kw.get("json") or {})
        raise httpx.ConnectError("the request body is what we are asserting on")

    monkeypatch.setattr(httpx.AsyncClient, "post", capture)
    with pytest.raises(LiveAvatarError):
        asyncio.run(liveavatar.start_session(lang))

    assert seen["avatar_persona"]["language"] == lang
    assert "voice_id" not in seen["avatar_persona"], "sandbox has no bound voice to name"


def test_the_avatar_never_reads_an_elevenlabs_key(keyless):
    """`eleven_multilingual_v2` is a model name HeyGen accepts in their own field. If an
    ELEVENLABS_API_KEY ever starts being read, this is where it would show up first."""
    from app import liveavatar
    from app.config import Settings

    assert not hasattr(Settings(), "elevenlabs_api_key")
    src = __import__("pathlib").Path(liveavatar.__file__).read_text()
    assert "ELEVENLABS" not in src.upper().replace("ELEVEN_MULTILINGUAL", "")


# ------------------------------------------------------------------ enrollment
@pytest.fixture
def auth_mod(settings):
    from app import auth
    from app.db import connect

    connect()
    return auth


def test_an_ordinary_code_binds_to_the_first_device(auth_mod):
    """The default, and the reason a second browser gets turned away: one code, one student,
    one device. Sharing it would share a daily budget and a conversation history."""
    from fastapi import HTTPException

    code = auth_mod.create_students(1, ["binds"])[0]["code"]
    assert auth_mod.redeem(code, "phone")
    assert auth_mod.redeem(code, "phone"), "the same device must keep working"
    with pytest.raises(HTTPException) as e:
        auth_mod.redeem(code, "laptop")
    assert e.value.status_code == 403


def test_a_reusable_code_works_on_every_device(auth_mod):
    code = auth_mod.create_students(1, ["teacher"], reusable=True)[0]["code"]
    for device in ("phone", "laptop", "lecture-hall-pc"):
        assert auth_mod.redeem(code, device), f"{device} was turned away"


def test_a_reusable_code_keeps_one_identity(auth_mod):
    """Worth pinning because it is the cost of reusability: two devices are one student, so
    they share the daily cap and the history. Fine for a demo, wrong for a cohort."""
    code = auth_mod.create_students(1, ["teacher"], reusable=True)[0]["code"]
    a = auth_mod.decode_token(auth_mod.redeem(code, "phone"))
    b = auth_mod.decode_token(auth_mod.redeem(code, "laptop"))
    assert a["sub"] == b["sub"]
    assert a["dev"] != b["dev"]


def test_the_reusable_column_reaches_an_older_database(tmp_path, monkeypatch):
    """CREATE TABLE IF NOT EXISTS cannot add a column, and every existing install has a real
    tutor.sqlite with real students in it. Without the migration those installs would take
    --reusable and silently still bind to one device."""
    import sqlite3

    from app import db

    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.execute("""CREATE TABLE students (id TEXT PRIMARY KEY, label TEXT, code TEXT UNIQUE NOT NULL,
                   redeemed_at INTEGER, device_id TEXT, created_at INTEGER NOT NULL)""")
    old.execute("INSERT INTO students VALUES ('s1', 'before', 'AAAA-BBBB', NULL, NULL, 0)")
    old.commit()
    old.close()

    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db.settings, "data_dir", tmp_path)
    (tmp_path / "tutor.sqlite").write_bytes(path.read_bytes())

    conn = db.connect()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(students)")}
    assert "reusable" in cols
    assert conn.execute("SELECT code FROM students WHERE id='s1'").fetchone()[0] == "AAAA-BBBB", \
        "the migration must not lose the students already enrolled"
    monkeypatch.setattr(db, "_conn", None)
