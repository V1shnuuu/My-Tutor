"""Server-side proxy for HeyGen's LiveAvatar (real-time streaming avatar). Local/dev-only:
LiveAvatar bills per minute from its own cloud, so it cannot be the free, no-quota default
this project promises at scale — see LIVEAVATAR_ENABLED in core/.env.example. The raw API
key is minted into a short-lived session token here and never reaches the browser; the
browser only ever sees `session_token`/`livekit_client_token`.

Contract verified empirically against the live API (its docs omit exact request/response
shapes): POST /v1/sessions/token (X-API-KEY) -> session_token, then POST /v1/sessions/start
(Bearer session_token) -> a LiveKit room to join. Once connected, the browser drives the
avatar itself by publishing `{event_id, event_type: "avatar.speak_text", session_id, text}`
on the LiveKit "agent-control" data topic — see web/src/lib/liveavatar.ts. `is_sandbox: true`
swaps in a fixed demo avatar ("Wayne"), caps the session at ~60s, and burns no credits; it's
the fallback this module reaches for automatically when the account is out of credits.
"""
from __future__ import annotations

import httpx

from .config import settings

API_BASE = "https://api.liveavatar.com"
SANDBOX_AVATAR_ID = "dd73ea75-1218-4ef3-92ce-606d5f7fbc0a"  # the only avatar `is_sandbox` allows


class LiveAvatarError(RuntimeError):
    def __init__(self, message: str, *, no_credits: bool = False):
        super().__init__(message)
        self.no_credits = no_credits


def _json(r: httpx.Response) -> dict:
    """Their errors are not always JSON (gateway HTML, empty 502s); surface the status
    rather than letting a JSONDecodeError escape as an uninformative 500."""
    try:
        data = r.json()
    except ValueError:
        raise LiveAvatarError(f"LiveAvatar returned HTTP {r.status_code} (not JSON)") from None
    if not isinstance(data, dict):
        raise LiveAvatarError(f"LiveAvatar returned HTTP {r.status_code} (unexpected body)")
    return data


def _persona() -> dict:
    persona: dict = {}
    if settings.liveavatar_voice_id:
        persona["voice_id"] = settings.liveavatar_voice_id
        persona["voice_settings"] = {
            "speed": settings.liveavatar_tts_speed,
            "stability": settings.liveavatar_tts_stability,
            "style": settings.liveavatar_tts_style,
            "model": settings.liveavatar_tts_model,
        }
    return persona


async def start_session(lang: str) -> dict:
    """Mint a session token and start it. Falls back to is_sandbox on a credits error so a
    dev session never hard-fails just because the account ran dry."""
    if not settings.liveavatar_enabled or not settings.liveavatar_api_key:
        raise LiveAvatarError("LiveAvatar is disabled (no LIVEAVATAR_API_KEY)")

    sandbox = settings.liveavatar_sandbox
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            return await _start_once(client, lang, sandbox)
        except LiveAvatarError as e:
            if e.no_credits and not sandbox:
                return await _start_once(client, lang, True)
            raise
        except httpx.HTTPError as e:
            # Their cloud is unreachable (outage, DNS, egress policy, timeout). Without this
            # the transport error escapes as a bare 500, which CORSMiddleware never gets to
            # annotate — so the browser reports a misleading CORS failure and the panel can
            # neither show the real reason nor fall back cleanly.
            raise LiveAvatarError(f"LiveAvatar unreachable: {type(e).__name__}") from e


async def _start_once(client: httpx.AsyncClient, lang: str, sandbox: bool) -> dict:
    persona = {} if sandbox else _persona()
    persona["language"] = lang
    body = {
        "mode": "FULL",
        "is_sandbox": sandbox,
        "avatar_id": SANDBOX_AVATAR_ID if sandbox else settings.liveavatar_avatar_id,
        "avatar_persona": persona,
        "interactivity_type": "CONVERSATIONAL",
    }
    r = await client.post(
        f"{API_BASE}/v1/sessions/token",
        headers={"X-API-KEY": settings.liveavatar_api_key},
        json=body,
    )
    data = _json(r)
    if data.get("code") != 1000:
        msg = str(data.get("message", ""))
        raise LiveAvatarError(msg, no_credits="credit" in msg.lower())
    session_token = data["data"]["session_token"]

    r = await client.post(
        f"{API_BASE}/v1/sessions/start",
        headers={"Authorization": f"Bearer {session_token}"},
        json={},
    )
    data = _json(r)
    if data.get("code") != 1000:
        msg = str(data.get("message", ""))
        raise LiveAvatarError(msg, no_credits="credit" in msg.lower())
    d = data["data"]
    return {
        "session_id": d["session_id"],
        "livekit_url": d["livekit_url"],
        "livekit_client_token": d["livekit_client_token"],
        "max_session_duration": d["max_session_duration"],
        "sandbox": sandbox,
    }


async def stop_session(session_id: str) -> None:
    if not settings.liveavatar_api_key:
        return
    # Best-effort: the session also expires on its own, so a failure to tell them about it
    # must not turn the caller's teardown into a 500.
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{API_BASE}/v1/sessions/stop",
                headers={"X-API-KEY": settings.liveavatar_api_key},
                json={"session_id": session_id, "reason": "USER_CLOSED"},
            )
    except httpx.HTTPError:
        pass
