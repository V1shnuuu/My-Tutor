"""/chat's per-student fair-use cap: auth.check_fair_share existed but nothing ever called it,
so there was no server-side limit on chat requests at all (a real, undocumented gap this
session's audit found — see main.py's /chat handler for the fix and its comment). Also covers
the fix for anonymous students sharing one identity ("anon"): each browser now gets its own
budget via X-Anon-Id.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import settings
from app.db import connect, tx


def _set_used(student_id: str, messages: int) -> None:
    from app.auth import today

    with tx() as c:
        c.execute("INSERT OR IGNORE INTO usage (student_id, day) VALUES (?, ?)", (student_id, today()))
        c.execute("UPDATE usage SET messages = ? WHERE student_id = ? AND day = ?", (messages, student_id, today()))


@pytest.fixture(scope="module")
def client():
    from app.main import app

    connect()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def reset_shared_anon_bucket():
    """Several tests here deliberately exhaust the real "anon" identity's budget — the whole
    point is testing that literal fallback bucket. Reset it after each test regardless of
    outcome, or the next test file's anonymous-chat tests (which assume a fresh budget) fail
    on leftover state from here rather than anything they actually broke."""
    yield
    _set_used("anon", 0)


def test_at_the_daily_cap_the_very_first_check_blocks_before_any_llm_work(client):
    """This is the regression test: before this session's fix, check_fair_share was never
    called from /chat at all, so this request would have gone through regardless of usage."""
    _set_used("budget-test-anon-1", settings.student_daily_cap)
    r = client.post("/chat", json={"message": "anything", "history": []}, headers={"X-Anon-Id": "budget-test-anon-1"})
    assert r.status_code == 429
    assert r.json()["detail"] == "daily_cap"


def test_a_different_anon_id_has_its_own_independent_budget(client):
    _set_used("budget-test-anon-2", settings.student_daily_cap)
    r = client.post("/chat", json={"message": "anything", "history": []}, headers={"X-Anon-Id": "budget-test-anon-3"})
    assert r.status_code != 429, "a fresh identity must not inherit another browser's exhausted budget"


def test_a_signed_in_students_budget_is_keyed_by_email_not_the_shared_anon_bucket(client):
    _set_used("anon", settings.student_daily_cap)  # exhaust the fallback shared bucket
    auth.upsert_user("budget-student@example.com", "Budget Student", "")
    tok = auth.issue_user_token("budget-student@example.com")
    r = client.post("/chat", json={"message": "anything", "history": []}, headers={"authorization": f"Bearer {tok}"})
    assert r.status_code != 429, "a signed-in student must not be capped by anonymous traffic sharing 'anon'"


def test_missing_anon_id_header_falls_back_to_the_shared_bucket(client):
    """Old cached bundles or non-browser callers with no X-Anon-Id still get *some* cap
    (the shared fallback) rather than silently bypassing the limit entirely."""
    _set_used("anon", settings.student_daily_cap)
    r = client.post("/chat", json={"message": "anything", "history": []})
    assert r.status_code == 429
    assert r.json()["detail"] == "daily_cap"
