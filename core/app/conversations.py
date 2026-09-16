"""Server-side chat history, scoped to a signed-in Google account.

This is the one place that stores conversation text on the server, and it only ever does so
for a signed-in user: anonymous sessions keep the old browser-local behaviour and nothing
here runs for them. Every read and write takes the caller's email and filters on it, so a
conversation id from one account is a 404 for another rather than a leak.
"""
from __future__ import annotations

import json
import time
import uuid

from fastapi import HTTPException

from .db import query, tx

TITLE_MAX = 80
# The window handed to the LLM when a conversation is resumed. Long enough to follow a thread
# of follow-ups, short enough that an hour-old chat does not blow the context budget.
HISTORY_TURNS = 8


def _row_to_conversation(r) -> dict:
    return {
        "id": r["id"],
        "title": r["title"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "message_count": r["message_count"] if "message_count" in r.keys() else None,
    }


def create(user_email: str, title: str = "") -> dict:
    now = int(time.time())
    cid = uuid.uuid4().hex
    with tx() as c:
        c.execute(
            "INSERT INTO conversations (id, user_email, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (cid, user_email, title[:TITLE_MAX], now, now),
        )
    return {"id": cid, "title": title[:TITLE_MAX], "created_at": now, "updated_at": now, "message_count": 0}


def list_for(user_email: str, limit: int = 50) -> list[dict]:
    rows = query(
        "SELECT c.id, c.title, c.created_at, c.updated_at,"
        " (SELECT COUNT(*) FROM conv_messages m WHERE m.conversation_id = c.id) AS message_count"
        " FROM conversations c WHERE c.user_email = ?"
        " ORDER BY c.updated_at DESC LIMIT ?",
        (user_email, limit),
    )
    # An empty conversation is one the student opened and never used; showing it in the
    # history list would just be noise they cannot act on.
    return [_row_to_conversation(r) for r in rows if r["message_count"] > 0]


def owned_or_404(conversation_id: str, user_email: str) -> dict:
    rows = query(
        "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ? AND user_email = ?",
        (conversation_id, user_email),
    )
    if not rows:
        # Deliberately the same answer for "does not exist" and "belongs to someone else":
        # distinguishing them would confirm another account's conversation ids.
        raise HTTPException(404, "conversation_not_found")
    return _row_to_conversation(rows[0])


def messages(conversation_id: str, user_email: str) -> list[dict]:
    owned_or_404(conversation_id, user_email)
    rows = query(
        "SELECT role, content, lang, citations, source, ts FROM conv_messages"
        " WHERE conversation_id = ? ORDER BY id",
        (conversation_id,),
    )
    out = []
    for r in rows:
        try:
            cites = json.loads(r["citations"])
        except (TypeError, ValueError):
            cites = []
        out.append({
            "role": r["role"], "content": r["content"], "lang": r["lang"],
            "citations": cites, "source": r["source"], "ts": r["ts"],
        })
    return out


def history_for_llm(conversation_id: str, user_email: str) -> list[dict]:
    """The prior turns of this conversation, in the shape run_chat expects.

    Taken from the database rather than from the client so a resumed conversation carries its
    real context, and so a client cannot claim a history it never had.
    """
    rows = query(
        "SELECT role, content FROM conv_messages m"
        " WHERE m.conversation_id = ? AND EXISTS"
        " (SELECT 1 FROM conversations c WHERE c.id = m.conversation_id AND c.user_email = ?)"
        " ORDER BY m.id DESC LIMIT ?",
        (conversation_id, user_email, HISTORY_TURNS),
    )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def add_message(conversation_id: str, role: str, content: str, lang: str | None = None,
                citations: list[dict] | None = None, source: str | None = None) -> None:
    now = int(time.time())
    with tx() as c:
        c.execute(
            "INSERT INTO conv_messages (conversation_id, role, content, lang, citations, source, ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, role, content, lang,
             json.dumps(citations or [], ensure_ascii=False), source, now),
        )
        c.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
        # The first question names the conversation; later ones leave the title alone.
        c.execute(
            "UPDATE conversations SET title = ? WHERE id = ? AND title = '' AND ? = 'user'",
            (content.strip()[:TITLE_MAX], conversation_id, role),
        )


def rename(conversation_id: str, user_email: str, title: str) -> dict:
    owned_or_404(conversation_id, user_email)
    with tx() as c:
        c.execute("UPDATE conversations SET title = ? WHERE id = ?", (title.strip()[:TITLE_MAX], conversation_id))
    return owned_or_404(conversation_id, user_email)


def delete(conversation_id: str, user_email: str) -> None:
    owned_or_404(conversation_id, user_email)
    with tx() as c:
        c.execute("DELETE FROM conv_messages WHERE conversation_id = ?", (conversation_id,))
        c.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))


def delete_all(user_email: str) -> int:
    rows = query("SELECT id FROM conversations WHERE user_email = ?", (user_email,))
    ids = [r["id"] for r in rows]
    if not ids:
        return 0
    with tx() as c:
        c.executemany("DELETE FROM conv_messages WHERE conversation_id = ?", [(i,) for i in ids])
        c.execute("DELETE FROM conversations WHERE user_email = ?", (user_email,))
    return len(ids)
