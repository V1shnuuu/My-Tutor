"""LiveAvatar session handling.

Regression cover: a transport error used to escape as a bare 500, which CORSMiddleware
never annotates — so the browser reported a misleading CORS failure and the panel could
neither show the reason nor fall back.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app import liveavatar
from app.liveavatar import LiveAvatarError


@pytest.fixture
def enabled(settings):
    before = (settings.liveavatar_enabled, settings.liveavatar_api_key, settings.liveavatar_sandbox)
    settings.liveavatar_enabled = True
    settings.liveavatar_api_key = "test-key"
    settings.liveavatar_sandbox = True
    yield settings
    (settings.liveavatar_enabled, settings.liveavatar_api_key, settings.liveavatar_sandbox) = before


def test_disabled_is_a_typed_error(settings):
    before = settings.liveavatar_enabled
    settings.liveavatar_enabled = False
    try:
        with pytest.raises(LiveAvatarError):
            asyncio.run(liveavatar.start_session("ar"))
    finally:
        settings.liveavatar_enabled = before


def test_unreachable_cloud_becomes_a_typed_error(enabled, monkeypatch):
    """The bug: httpx.HTTPError escaping start_session, so main.py never mapped it to 502."""
    async def boom(*a, **k):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    with pytest.raises(LiveAvatarError) as e:
        asyncio.run(liveavatar.start_session("ar"))
    assert "unreachable" in str(e.value).lower()


def test_non_json_body_is_a_typed_error(enabled, monkeypatch):
    """Gateways answer with HTML; r.json() would raise ValueError straight out of the handler."""
    async def html(*a, **k):
        return httpx.Response(502, text="<html>bad gateway</html>")

    monkeypatch.setattr(httpx.AsyncClient, "post", html)
    with pytest.raises(LiveAvatarError) as e:
        asyncio.run(liveavatar.start_session("ar"))
    assert "502" in str(e.value)


def test_stop_is_best_effort(enabled, monkeypatch):
    """Teardown runs while things are already broken; it must not raise on top of that."""
    async def boom(*a, **k):
        raise httpx.ConnectError("gone")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    asyncio.run(liveavatar.stop_session("session-123"))  # must not raise


def test_sandbox_sends_the_demo_avatar_and_no_persona(enabled, monkeypatch):
    """Sandbox ignores the configured avatar and voice — worth pinning, because the
    opposite assumption ("my Arabic voice will be used") is the natural one to make."""
    enabled.liveavatar_avatar_id = "my-custom-avatar"
    enabled.liveavatar_voice_id = "my-arabic-voice"
    seen = {}

    async def capture(self, url, **kw):
        seen.update(kw.get("json") or {})
        raise httpx.ConnectError("stop here; the request body is what we are asserting on")

    monkeypatch.setattr(httpx.AsyncClient, "post", capture)
    with pytest.raises(LiveAvatarError):
        asyncio.run(liveavatar.start_session("ar"))

    assert seen["is_sandbox"] is True
    assert seen["avatar_id"] == liveavatar.SANDBOX_AVATAR_ID
    assert "voice_id" not in seen["avatar_persona"]
    assert seen["avatar_persona"]["language"] == "ar"


def test_configured_avatar_and_voice_are_used_outside_sandbox(enabled, monkeypatch):
    enabled.liveavatar_sandbox = False
    enabled.liveavatar_avatar_id = "my-custom-avatar"
    enabled.liveavatar_voice_id = "my-arabic-voice"
    seen = {}

    async def capture(self, url, **kw):
        seen.update(kw.get("json") or {})
        raise httpx.ConnectError("stop here")

    monkeypatch.setattr(httpx.AsyncClient, "post", capture)
    with pytest.raises(LiveAvatarError):
        asyncio.run(liveavatar.start_session("ar"))

    assert seen["is_sandbox"] is False
    assert seen["avatar_id"] == "my-custom-avatar"
    assert seen["avatar_persona"]["voice_id"] == "my-arabic-voice"
