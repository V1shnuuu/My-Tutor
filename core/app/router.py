"""Budget-aware LLM router over OpenAI-compatible free-tier endpoints.

- Every provider has rpm/rpd/tpm/tpd windows (providers.yaml) and a concurrency cap.
- Selection prefers tier A for Arabic and providers whose windows have the most
  headroom; the router keeps 20% headroom under every limit.
- A 429/5xx puts the provider in cooldown and the request falls through to the next.
- When nothing is available the caller waits (queue) up to router_max_wait_s and
  then uses the LLM-free extractive floor. Students never see a rate-limit error.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import AsyncIterator

import httpx

from .auth import today
from .config import load_providers, settings
from .db import tx

HEADROOM = 0.8


@dataclass
class Provider:
    cfg: dict
    minute_reqs: deque = field(default_factory=deque)   # timestamps
    minute_tokens: deque = field(default_factory=deque) # (ts, tokens)
    day_key: str = ""
    day_reqs: int = 0
    day_tokens: int = 0
    inflight: int = 0
    cooldown_until: float = 0.0
    errors: int = 0

    @property
    def id(self) -> str:
        return self.cfg["id"]

    def _roll(self) -> None:
        now = time.time()
        while self.minute_reqs and now - self.minute_reqs[0] > 60:
            self.minute_reqs.popleft()
        while self.minute_tokens and now - self.minute_tokens[0][0] > 60:
            self.minute_tokens.popleft()
        d = today()
        if d != self.day_key:
            self.day_key, self.day_reqs, self.day_tokens = d, 0, 0

    def headroom(self, est_tokens: int) -> float:
        """Fraction of the tightest window still free (0 = unavailable)."""
        self._roll()
        if time.time() < self.cooldown_until or self.inflight >= self.cfg.get("concurrency", 2):
            return 0.0
        fr = []
        c = self.cfg
        if c.get("rpm"):
            fr.append(1 - (len(self.minute_reqs) + 1) / (c["rpm"] * HEADROOM))
        if c.get("rpd"):
            fr.append(1 - (self.day_reqs + 1) / (c["rpd"] * HEADROOM))
        if c.get("tpm"):
            used = sum(t for _, t in self.minute_tokens)
            fr.append(1 - (used + est_tokens) / (c["tpm"] * HEADROOM))
        if c.get("tpd"):
            fr.append(1 - (self.day_tokens + est_tokens) / (c["tpd"] * HEADROOM))
        return max(0.0, min(fr) if fr else 1.0)

    def record(self, tokens: int, ok: bool) -> None:
        now = time.time()
        self._roll()
        self.minute_reqs.append(now)
        self.minute_tokens.append((now, tokens))
        self.day_reqs += 1
        self.day_tokens += tokens
        if not ok:
            self.errors += 1
        with tx() as c:
            c.execute("INSERT OR IGNORE INTO provider_usage (provider, day) VALUES (?, ?)", (self.id, self.day_key))
            c.execute(
                "UPDATE provider_usage SET requests = requests + 1, tokens = tokens + ?, errors = errors + ? WHERE provider = ? AND day = ?",
                (tokens, 0 if ok else 1, self.id, self.day_key),
            )

    def snapshot(self) -> dict:
        self._roll()
        c = self.cfg
        return {
            "id": self.id,
            "model": c["model"],
            "tier": c["tier"],
            "rpm_used": len(self.minute_reqs), "rpm": c.get("rpm"),
            "rpd_used": self.day_reqs, "rpd": c.get("rpd"),
            "tpd_used": self.day_tokens, "tpd": c.get("tpd"),
            "inflight": self.inflight,
            "cooldown_s": max(0, int(self.cooldown_until - time.time())),
            "errors": self.errors,
        }


class Router:
    def __init__(self):
        self.providers: list[Provider] = [Provider(p) for p in load_providers()]
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        self.queue_depth = 0

    @property
    def enabled(self) -> bool:
        return bool(self.providers)

    def pick(self, lang: str, est_tokens: int) -> Provider | None:
        best, best_score = None, 0.0
        for p in self.providers:
            h = p.headroom(est_tokens)
            if h <= 0:
                continue
            score = h + p.cfg.get("priority", 0)
            if p.cfg["tier"] == "A":
                score += 0.5
            if lang in p.cfg.get("langs", []):
                score += 0.25
            elif lang == "ar":
                score -= 0.5  # keep Arabic away from weak-Arabic bulk lanes unless nothing else
            if score > best_score:
                best, best_score = p, score
        return best

    async def acquire(self, lang: str, est_tokens: int, on_wait=None) -> Provider | None:
        """Pick a provider, waiting (queue) if all are saturated. None = use the floor."""
        deadline = time.time() + settings.router_max_wait_s
        waited = False
        while True:
            p = self.pick(lang, est_tokens)
            if p is not None:
                p.inflight += 1
                if waited:
                    self.queue_depth -= 1
                return p
            if time.time() >= deadline:
                if waited:
                    self.queue_depth -= 1
                return None
            if not waited:
                waited = True
                self.queue_depth += 1
            if on_wait:
                await on_wait(self.queue_depth, int(deadline - time.time()))
            await asyncio.sleep(1.0)

    async def stream(self, provider: Provider, messages: list[dict], prompt_tokens: int) -> AsyncIterator[str]:
        """Stream text deltas from an OpenAI-compatible chat/completions endpoint.

        `prompt_tokens` is an estimate of the request alone — unlike the est_tokens passed to
        `pick()`, it must NOT include a max_output_tokens allowance, since this method adds the
        real measured completion size (`out_tokens`) itself before recording usage.
        """
        cfg = provider.cfg
        url = cfg["base_url"].rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
        if cfg["id"].startswith("openrouter"):
            headers["HTTP-Referer"] = "https://adaptive-tutor.local"
            headers["X-Title"] = "Adaptive Tutor"
        body = {
            "model": cfg["model"],
            "messages": messages,
            "stream": True,
            "temperature": 0.2,
            "max_tokens": settings.max_output_tokens,
        }
        body.update(cfg.get("extra_body") or {})
        out_tokens = 0
        ok = False
        try:
            async with self.client.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code >= 400:
                    text = (await r.aread())[:300].decode("utf-8", "replace")
                    # 429 (real rate limit): a long cooldown — retrying sooner just burns
                    # another call against the same window. 5xx (the provider's own outage,
                    # e.g. Groq returning a bare 502 from its Cloudflare front door — seen for
                    # real, not hypothetical) recovers on its own in seconds, and with only one
                    # or two cloud providers actually configured, a 20s cooldown here used to
                    # lock out every student's cloud generation for the full 20s over one blip.
                    # Any other 4xx (bad model name, bad request) needs a human fix, not a
                    # retry — cooldown long enough to stop hammering it, short of the 429 tier.
                    provider.cooldown_until = time.time() + (60 if r.status_code == 429 else 5 if r.status_code >= 500 else 20)
                    raise ProviderError(f"{cfg['id']} HTTP {r.status_code}: {text}")
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    for ch in obj.get("choices", []):
                        delta = ch.get("delta", {}).get("content")
                        if delta:
                            out_tokens += max(1, len(delta) // 4)
                            yield delta
            ok = True
        except httpx.HTTPError as e:
            # A dropped connection/timeout is the same "transient, recovers fast" class as a
            # 5xx above — same short cooldown, for the same reason.
            provider.cooldown_until = time.time() + 5
            raise ProviderError(f"{cfg['id']} network: {e}") from e
        finally:
            provider.inflight -= 1
            provider.record(prompt_tokens + out_tokens, ok)

    def snapshot(self) -> dict:
        return {"queue_depth": self.queue_depth, "providers": [p.snapshot() for p in self.providers]}


class ProviderError(RuntimeError):
    pass


router = Router()
