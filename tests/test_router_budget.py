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
