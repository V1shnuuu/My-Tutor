"""Router token-budget accounting.

Regression cover: router.stream() used to record est_tokens + out_tokens, where est_tokens
already included a full settings.max_output_tokens allowance meant only for the pre-flight
pick()/headroom() gate. Recording it again on top of the real streamed output double-counted
the completion budget on every request, making providers look saturated far sooner than they
actually were.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time

import httpx

from app.router import Provider, Router


def make_provider(**overrides) -> Provider:
    cfg = {
        "id": "test-provider",
        "base_url": "https://example.invalid/v1",
        "model": "test-model",
        "tier": "B",
        "priority": 0,
        "api_key": "test-key",
        "rpm": 0, "rpd": 0, "tpm": 0, "tpd": 0,
        "concurrency": 2,
    }
    cfg.update(overrides)
    return Provider(cfg)


class _FakeStreamResponse:
    def __init__(self, chunks: list[str]):
        self.status_code = 200
        self._chunks = chunks

    async def aiter_lines(self):
        for c in self._chunks:
            yield "data: " + json.dumps({"choices": [{"delta": {"content": c}}]})
        yield "data: [DONE]"

    async def aread(self):
        return b""


def test_stream_records_prompt_plus_real_output_not_the_gating_estimate(monkeypatch):
    router = Router.__new__(Router)  # skip __init__'s providers.yaml load
    router.providers = []
    router.client = httpx.AsyncClient()
    router.queue_depth = 0

    provider = make_provider()
    words = ["Hello", " world", " this", " is", " a", " reply"]  # 6 short deltas

    @contextlib.asynccontextmanager
    async def fake_stream(method, url, headers=None, json=None):
        yield _FakeStreamResponse(words)

    monkeypatch.setattr(router.client, "stream", fake_stream)

    prompt_tokens = 12  # deliberately NOT including any max_output_tokens allowance

    async def go():
        out = []
        async for delta in router.stream(provider, [{"role": "user", "content": "hi"}], prompt_tokens):
            out.append(delta)
        return out

    out = asyncio.run(go())
    assert out == words

    # out_tokens is computed inside stream() as max(1, len(delta)//4) per delta
    expected_out_tokens = sum(max(1, len(w) // 4) for w in words)
    assert provider.day_tokens == prompt_tokens + expected_out_tokens, (
        "recorded usage must be prompt + real output only — "
        "if this fails with a much larger number, max_output_tokens is being "
        "double-counted again (see settings.max_output_tokens in chat.py's est_tokens)"
    )
    assert provider.day_reqs == 1
    assert provider.errors == 0


class _FakeErrorResponse:
    """A non-200 response — status_code plus a readable body, nothing else touched."""
    def __init__(self, status_code: int, body: bytes = b'{"error": "boom"}'):
        self.status_code = status_code
        self._body = body

    async def aread(self):
        return self._body


def _run_one_failing_stream(status_code: int) -> Provider:
    """Drives router.stream() once against a fake `status_code` response and returns the
    provider afterward — cooldown_until is what each test below inspects."""
    router = Router.__new__(Router)
    router.providers = []
    router.client = httpx.AsyncClient()
    router.queue_depth = 0
    provider = make_provider()

    @contextlib.asynccontextmanager
    async def fake_stream(method, url, headers=None, json=None):
        yield _FakeErrorResponse(status_code)

    monkeypatch_stream = fake_stream

    async def go():
        try:
            async for _ in router.stream(provider, [{"role": "user", "content": "hi"}], 10):
                pass
        except Exception:
            pass

    router.client.stream = monkeypatch_stream
    asyncio.run(go())
    return provider


def test_a_rate_limit_gets_the_long_cooldown():
    """429 means the window is genuinely exhausted — retrying sooner just burns another
    call against it, so this one stays long."""
    provider = _run_one_failing_stream(429)
    assert 55 <= provider.cooldown_until - time.time() <= 60


def test_a_provider_side_outage_gets_a_short_cooldown_not_a_long_one():
    """This is the regression: a bare 502 from Groq's own Cloudflare front door (a real,
    observed outage, not hypothetical — see the commit this test landed with) used to cost
    every student 20s of no cloud generation at all on a deployment with only one provider
    configured. 5xx recovers on its own in seconds; the cooldown should match that, not
    the same duration as a config error that needs a human to fix."""
    provider = _run_one_failing_stream(502)
    assert 0 < provider.cooldown_until - time.time() <= 5


def test_a_config_error_gets_a_medium_cooldown_between_the_other_two():
    """A 404 (bad model name — the exact bug that shipped once already, see providers.yaml's
    history) won't recover on its own, but locking it out for the full 429 duration isn't
    right either since it's a different failure class."""
    provider = _run_one_failing_stream(404)
    cooldown = provider.cooldown_until - time.time()
    assert 5 < cooldown <= 20
