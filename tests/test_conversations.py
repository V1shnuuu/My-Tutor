"""Signed-in chat history: ownership, isolation, persistence and the anonymous fallback.

The isolation tests are the important ones. Conversation ids are the only thing standing
between one student's saved chats and another's, so every route that takes one must filter on
the caller's email — a missing filter would read as a working feature right up until someone
guessed an id.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth, conversations
from app.db import connect


@pytest.fixture(scope="module")
def client():
    from app.main import app

    connect()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def alice():
    auth.upsert_user("alice@example.com", "Alice", "")
    return {"authorization": f"Bearer {auth.issue_user_token('alice@example.com')}"}


@pytest.fixture
def mallory():
    auth.upsert_user("mallory@example.com", "Mallory", "")
    return {"authorization": f"Bearer {auth.issue_user_token('mallory@example.com')}"}


# ---------------------------------------------------------------- auth surface
def test_conversation_routes_require_a_token(client):
    assert client.get("/conversations").status_code == 401
    assert client.post("/conversations", json={}).status_code == 401
    assert client.get("/conversations/whatever").status_code == 401
    assert client.delete("/conversations/whatever").status_code == 401


def test_a_garbage_token_is_not_a_session(client):
    bad = {"authorization": "Bearer not-a-jwt"}
    assert client.get("/conversations", headers=bad).status_code == 401


def test_an_enrollment_token_cannot_pose_as_a_user(client):
    """The old student JWTs are still issuable by the CLI; they must not unlock accounts."""
    student = auth.issue_token("student-123", "device-abc")
    assert client.get("/conversations", headers={"authorization": f"Bearer {student}"}).status_code == 401


def test_google_signin_is_off_without_a_client_id(client, settings):
    before = settings.google_client_id
    settings.google_client_id = ""
    try:
        assert client.get("/auth/config").json() == {"google_client_id": "", "enabled": False}
        r = client.post("/auth/google", json={"credential": "x" * 32})
        assert r.status_code == 503 and r.json()["detail"] == "google_signin_not_configured"
    finally:
        settings.google_client_id = before


def test_a_forged_google_token_is_rejected(client, settings):
    """An unsigned/self-made token must never mint a session, even with sign-in configured."""
    import jwt as pyjwt

    before = settings.google_client_id
    settings.google_client_id = "test-client-id.apps.googleusercontent.com"
    try:
        forged = pyjwt.encode(
            {"iss": "https://accounts.google.com", "aud": settings.google_client_id,
             "sub": "1", "email": "attacker@example.com", "exp": 9999999999, "iat": 1},
            "attacker-chosen-secret", algorithm="HS256",
        )
        assert client.post("/auth/google", json={"credential": forged}).status_code in (401, 503)
    finally:
        settings.google_client_id = before


# ---------------------------------------------------------------- ownership / isolation
def test_a_conversation_belongs_to_its_creator(client, alice, mallory):
    cid = client.post("/conversations", json={}, headers=alice).json()["id"]
    conversations.add_message(cid, "user", "alice's private question")

    assert client.get(f"/conversations/{cid}", headers=alice).status_code == 200
    # Same answer for "not yours" as for "does not exist": a 403 would confirm the id is real.
    assert client.get(f"/conversations/{cid}", headers=mallory).status_code == 404
    assert client.delete(f"/conversations/{cid}", headers=mallory).status_code == 404
    assert client.patch(f"/conversations/{cid}", json={"title": "hijacked"}, headers=mallory).status_code == 404


def test_another_account_cannot_append_to_your_conversation(client, alice, mallory):
    cid = client.post("/conversations", json={}, headers=alice).json()["id"]
    r = client.post("/chat", json={"message": "hello", "conversation_id": cid}, headers=mallory)
    assert r.status_code == 404


def test_the_list_only_shows_your_own(client, alice, mallory):
    cid = client.post("/conversations", json={}, headers=alice).json()["id"]
    conversations.add_message(cid, "user", "mine")
    mine = {c["id"] for c in client.get("/conversations", headers=alice).json()["conversations"]}
    theirs = {c["id"] for c in client.get("/conversations", headers=mallory).json()["conversations"]}
    assert cid in mine
    assert cid not in theirs


# ---------------------------------------------------------------- behaviour
def test_the_first_question_names_the_conversation(client, alice):
    cid = client.post("/conversations", json={}, headers=alice).json()["id"]
    conversations.add_message(cid, "user", "What is a peak?")
    conversations.add_message(cid, "assistant", "A peak is ...")
    conversations.add_message(cid, "user", "and in 2D?")
    assert conversations.owned_or_404(cid, "alice@example.com")["title"] == "What is a peak?"


def test_empty_conversations_stay_out_of_the_history_list(client, alice):
    """Opening the app creates one; never using it must not leave a blank row behind."""
    cid = client.post("/conversations", json={}, headers=alice).json()["id"]
    listed = {c["id"] for c in client.get("/conversations", headers=alice).json()["conversations"]}
    assert cid not in listed


def test_history_for_the_model_comes_from_the_database(client, alice):
    cid = client.post("/conversations", json={}, headers=alice).json()["id"]
    conversations.add_message(cid, "user", "first question")
    conversations.add_message(cid, "assistant", "first answer")
    hist = conversations.history_for_llm(cid, "alice@example.com")
    assert [h["role"] for h in hist] == ["user", "assistant"]
    assert hist[0]["content"] == "first question"
    # ...and not from someone else's claim about it
    assert conversations.history_for_llm(cid, "mallory@example.com") == []


def test_delete_removes_the_messages_too(client, alice):
    cid = client.post("/conversations", json={}, headers=alice).json()["id"]
    conversations.add_message(cid, "user", "to be deleted")
    assert client.delete(f"/conversations/{cid}", headers=alice).status_code == 200
    assert client.get(f"/conversations/{cid}", headers=alice).status_code == 404
    from app.db import query
    assert query("SELECT COUNT(*) AS n FROM conv_messages WHERE conversation_id = ?", (cid,))[0]["n"] == 0


def test_rename(client, alice):
    cid = client.post("/conversations", json={}, headers=alice).json()["id"]
    conversations.add_message(cid, "user", "original")
    assert client.patch(f"/conversations/{cid}", json={"title": "Renamed"}, headers=alice).json()["title"] == "Renamed"


def test_anonymous_chat_still_works_and_saves_nothing(client):
    """Signing in buys saved history, not access: the tutor must answer without it."""
    from app.db import query

    before = query("SELECT COUNT(*) AS n FROM conv_messages")[0]["n"]
    r = client.post("/chat", json={"message": "What is a peak?"})
    assert r.status_code == 200
    assert query("SELECT COUNT(*) AS n FROM conv_messages")[0]["n"] == before
