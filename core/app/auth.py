"""Identity and per-student accounting.

Two independent things live here:

1. Google Sign-In (`verify_google_id_token` → `issue_user_token` → `CurrentUser`). The
   browser gets an ID token from Google, we verify its signature against Google's published
   JWKS and swap it for our own JWT. Only GOOGLE_CLIENT_ID is needed and it is public by
   design, so there is no secret to leak; with it unset, sign-in is simply unavailable and
   the app runs anonymously (see `OptionalUser`).
2. Enrollment codes (`create_students` / `redeem`), the pre-SSO scheme. No API route uses
   them any more, but `python -m app.cli codes` and its tests still do.
"""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone

import jwt
from fastapi import Depends, HTTPException, Request

from .config import settings
from .db import query, tx

GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_jwks_client: "jwt.PyJWKClient | None" = None

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def new_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def create_students(n: int, labels: list[str] | None = None, reusable: bool = False) -> list[dict]:
    """Insert n students with fresh codes. Returns [{id, label, code}].

    `reusable` marks a code that never binds to a device, so the same one works on a phone,
    a laptop and a lecture-hall machine, for as long as it exists. That is what you want for
    yourself and for a demo, and what you must not hand to a cohort: everyone redeeming it
    shares one identity, and therefore one daily budget and one conversation history.
    """
    out = []
    now = int(time.time())
    with tx() as c:
        for i in range(n):
            sid = secrets.token_hex(8)
            label = labels[i] if labels and i < len(labels) else f"student-{i + 1:03d}"
            code = new_code()
            c.execute(
                "INSERT INTO students (id, label, code, created_at, reusable) VALUES (?, ?, ?, ?, ?)",
                (sid, label, code, now, 1 if reusable else 0),
            )
            out.append({"id": sid, "label": label, "code": code})
    return out


def redeem(code: str, device_id: str) -> str:
    code = code.strip().upper().replace(" ", "")
    if len(code) == 8:
        code = f"{code[:4]}-{code[4:]}"
    rows = query("SELECT * FROM students WHERE code = ?", (code,))
    if not rows:
        raise HTTPException(401, "invalid_code")
    s = rows[0]
    # A reusable code is deliberately not bound to the device that got there first.
    if not s["reusable"] and s["redeemed_at"] and s["device_id"] != device_id:
        raise HTTPException(403, "code_already_used")
    with tx() as c:
        c.execute(
            "UPDATE students SET redeemed_at = COALESCE(redeemed_at, ?), device_id = ? WHERE id = ?",
            (int(time.time()), device_id, s["id"]),
        )
    return issue_token(s["id"], device_id)


def issue_token(student_id: str, device_id: str) -> str:
    now = int(time.time())
    payload = {"sub": student_id, "dev": device_id, "iat": now, "exp": now + settings.jwt_days * 86400}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "invalid_token")


async def current_student(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "missing_token")
    return decode_token(auth[7:])


# ---------------------------------------------------------------- Google Sign-In
def google_enabled() -> bool:
    return bool(settings.google_client_id)


def _jwks() -> "jwt.PyJWKClient":
    """Google's signing keys, fetched once and cached (they rotate every few days; PyJWKClient
    re-fetches on an unknown kid, which is exactly the rotation case)."""
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(GOOGLE_JWKS_URL, cache_keys=True)
    return _jwks_client


def verify_google_id_token(id_token: str) -> dict:
    """Verify a Google ID token and return its claims. Raises 401 on anything suspect.

    Signature, audience (our client id), issuer and expiry are all checked — skipping any one
    of them would let a token minted for a different app, or an unsigned one, sign someone in.
    """
    if not google_enabled():
        raise HTTPException(503, "google_signin_not_configured")
    try:
        key = _jwks().get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(
            id_token,
            key,
            algorithms=["RS256"],
            audience=settings.google_client_id,
            options={"require": ["exp", "iat", "aud", "iss", "sub"]},
        )
    except jwt.PyJWTError as e:
        raise HTTPException(401, "invalid_google_token") from e
    except Exception as e:  # JWKS unreachable, malformed token header, etc.
        raise HTTPException(503, "google_verification_unavailable") from e
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise HTTPException(401, "invalid_google_issuer")
    email = (claims.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(401, "google_token_has_no_email")
    if claims.get("email_verified") is False:
        raise HTTPException(403, "google_email_unverified")
    return {"email": email, "name": claims.get("name") or "", "picture": claims.get("picture") or ""}


def upsert_user(email: str, name: str, picture: str) -> dict:
    now = int(time.time())
    with tx() as c:
        c.execute(
            "INSERT INTO users (email, name, picture, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(email) DO UPDATE SET name = excluded.name, picture = excluded.picture,"
            " last_seen_at = excluded.last_seen_at",
            (email, name, picture, now, now),
        )
    return {"email": email, "name": name, "picture": picture}


def issue_user_token(email: str) -> str:
    now = int(time.time())
    payload = {"sub": email, "typ": "user", "iat": now, "exp": now + settings.jwt_days * 86400}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


async def current_user(request: Request) -> dict:
    """Require a signed-in user. Every conversation route depends on this."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "missing_token")
    claims = decode_token(auth[7:])
    if claims.get("typ") != "user" or not claims.get("sub"):
        raise HTTPException(401, "invalid_token")
    return {"email": claims["sub"]}


async def optional_user(request: Request) -> dict | None:
    """The signed-in user, or None. Lets /chat keep answering anonymously (no history saved)
    so the tutor still works before sign-in and on a keyless install."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    try:
        claims = decode_token(auth[7:])
    except HTTPException:
        return None
    if claims.get("typ") != "user" or not claims.get("sub"):
        return None
    return {"email": claims["sub"]}


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def check_fair_share(student_id: str, kind: str = "messages") -> dict:
    """Per-student daily/minute caps. Raises 429 with a friendly reason when exceeded.
    Returns the remaining daily budget for the UI."""
    day = today()
    rows = query("SELECT * FROM usage WHERE student_id = ? AND day = ?", (student_id, day))
    used = rows[0][kind] if rows else 0
    cap = settings.student_daily_cap if kind == "messages" else 10_000
    if used >= cap:
        raise HTTPException(429, "daily_cap")
    # minute cap via recent events table (cheap, approximate)
    if kind == "messages":
        recent = query(
            "SELECT COUNT(*) AS n FROM events WHERE kind = 'chat' AND ts > ? AND detail = ?",
            (int(time.time()) - 60, student_id),
        )[0]["n"]
        if recent >= settings.student_minute_cap:
            raise HTTPException(429, "minute_cap")
    return {"used": used, "cap": cap}


def bump_usage(student_id: str, **cols: int) -> None:
    day = today()
    sets = ", ".join(f"{k} = {k} + ?" for k in cols)
    with tx() as c:
        c.execute(
            "INSERT OR IGNORE INTO usage (student_id, day) VALUES (?, ?)", (student_id, day)
        )
        c.execute(
            f"UPDATE usage SET {sets} WHERE student_id = ? AND day = ?",
            (*cols.values(), student_id, day),
        )


def require_admin(request: Request) -> None:
    if request.headers.get("x-admin-token") != settings.admin_token:
        raise HTTPException(403, "admin_only")


Student = Depends(current_student)
CurrentUser = Depends(current_user)
OptionalUser = Depends(optional_user)
