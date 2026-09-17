"""Course-admin API surface: the security boundary (student vs admin) is the point of this
file, per the master prompt's own explicit requirement — every admin route must reject a
non-admin caller, never trusting a role the frontend claims. Real TestClient, real SQLite,
real Google-shaped JWTs minted via auth.issue_user_token — only the outbound yt-dlp playlist
read is mocked (no real network in tests).
"""
from __future__ import annotations

import pytest
import yt_dlp
from fastapi.testclient import TestClient

from app import auth
from app.db import connect


@pytest.fixture(scope="module")
def client():
    from app.main import app

    connect()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_headers(settings):
    before = settings.admin_emails
    settings.admin_emails = "admin@example.com"
    auth.upsert_user("admin@example.com", "Admin", "")
    yield {"authorization": f"Bearer {auth.issue_user_token('admin@example.com')}"}
    settings.admin_emails = before


@pytest.fixture
def student_headers():
    auth.upsert_user("student@example.com", "Student", "")
    return {"authorization": f"Bearer {auth.issue_user_token('student@example.com')}"}


# ---------------------------------------------------------------- the security boundary
ADMIN_ROUTES = [
    ("GET", "/admin/course/analytics", None),
    ("POST", "/admin/course/courses", {"title": "X"}),
    ("GET", "/admin/course/courses", None),
    ("PATCH", "/admin/course/courses/whatever", {"title": "X"}),
    ("DELETE", "/admin/course/courses/whatever", None),
    ("POST", "/admin/course/courses/whatever/publish", None),
    ("POST", "/admin/course/courses/whatever/weeks", {"title": "Week 1"}),
    ("PATCH", "/admin/course/weeks/whatever", {"title": "X"}),
    ("DELETE", "/admin/course/weeks/whatever", None),
    ("POST", "/admin/course/weeks/whatever/lessons", {"title": "Lesson"}),
    ("PATCH", "/admin/course/lessons/whatever", {"title": "X"}),
    ("DELETE", "/admin/course/lessons/whatever", None),
    ("POST", "/admin/course/lessons/whatever/video", {"video_id": None}),
    ("POST", "/admin/course/courses/whatever/playlist", {"url": "https://youtube.com/playlist?list=PLx"}),
    ("POST", "/admin/course/courses/whatever/playlist/sync", None),
    ("GET", "/admin/course/courses/whatever/playlist/videos", None),
    ("POST", "/admin/course/courses/whatever/syllabus/bulk", {"weeks": []}),
    ("POST", "/admin/course/syllabus/ai_parse", {"text": "Week 1\n1. Intro"}),
]


@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES)
def test_every_admin_route_rejects_an_unauthenticated_caller(client, method, path, body):
    r = client.request(method, path, json=body)
    assert r.status_code == 401, f"{method} {path} should 401 with no token, got {r.status_code}"


@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES)
def test_every_admin_route_rejects_a_signed_in_non_admin(client, method, path, body, student_headers):
    r = client.request(method, path, json=body, headers=student_headers)
    assert r.status_code == 403, f"{method} {path} should 403 for a non-admin, got {r.status_code}: {r.text}"


def test_a_forged_admin_email_in_the_request_body_is_ignored(client, student_headers):
    """The frontend cannot claim a role — there is no role field to claim, and even a body
    that tries to look like one changes nothing: authorization comes only from the caller's
    verified email against ADMIN_EMAILS, checked server-side."""
    r = client.post("/admin/course/courses", json={"title": "X", "role": "admin", "is_admin": True}, headers=student_headers)
    assert r.status_code == 403


def test_analytics_derives_rates_from_real_events(client, admin_headers):
    from app.db import log_event

    log_event("chat", "en", 500, "student-a")
    log_event("chat", "en", 700, "student-a")
    log_event("cache_hit", "en", 50, "student-a")
    log_event("gate_refuse", "en", 100, "student-a best=0.10")
    r = client.get("/admin/course/analytics", headers=admin_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["questions_24h"] >= 4
    # Exact rate depends on other tests' events sharing the same 24h window (real ts, real
    # DB) — assert the relationship holds rather than a brittle exact number.
    assert 0 <= data["cache_hit_rate_24h"] <= 1
    assert 0 <= data["refusal_rate_24h"] <= 1
    assert "providers" in data and isinstance(data["providers"], list)
    assert "stt" in data


def test_admin_status_endpoint_reflects_reality(client, admin_headers, student_headers):
    assert client.get("/admin/course/is_admin", headers=admin_headers).json() == {"is_admin": True}
    assert client.get("/admin/course/is_admin", headers=student_headers).json() == {"is_admin": False}
    assert client.get("/admin/course/is_admin").json() == {"is_admin": False}  # anonymous


# ---------------------------------------------------------------- the actual admin flow works
def test_full_course_authoring_flow(client, admin_headers):
    course = client.post("/admin/course/courses", json={"title": "MIT 6.006", "description": "Intro to algorithms"}, headers=admin_headers).json()
    assert course["status"] == "draft"

    bulk = client.post(f"/admin/course/courses/{course['id']}/syllabus/bulk", json={
        "weeks": [
            {"title": "Week 1", "lessons": ["Algorithmic Thinking, Peak Finding", "Models of Computation"]},
            {"title": "Week 2", "lessons": ["Insertion Sort, Merge Sort"]},
        ],
    }, headers=admin_headers).json()
    assert len(bulk["weeks"]) == 2
    assert len(bulk["weeks"][0]["lessons"]) == 2

    tree = client.get(f"/admin/course/courses/{course['id']}", headers=admin_headers).json()
    assert [w["title"] for w in tree["weeks"]] == ["Week 1", "Week 2"]
    assert tree["weeks"][0]["lessons"][0]["video_id"] is None  # nothing assigned yet

    client.post(f"/admin/course/courses/{course['id']}/publish", headers=admin_headers)
    assert client.get(f"/admin/course/courses/{course['id']}", headers=admin_headers).json()["status"] == "published"

    # The student-facing endpoint now serves it, with no auth required — matching /videos.
    published = client.get("/course").json()
    assert published is not None
    assert published["id"] == course["id"]
    assert published["weeks"][0]["lessons"][0]["video_id"] is None


def _mock_yt_extract(monkeypatch, result):
    """Stands in for yt_dlp.YoutubeDL(...).extract_info(...) — the one blocking call
    core/app/youtube.py makes. See tests/test_youtube.py for the unit-level coverage of
    that mapping; here it's just enough to drive the admin API end to end."""
    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return result

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)


def test_connect_playlist_end_to_end_with_youtube_mocked(client, admin_headers, monkeypatch):
    course = client.post("/admin/course/courses", json={"title": "Course"}, headers=admin_headers).json()

    _mock_yt_extract(monkeypatch, {
        "title": "MIT 6.006", "channel": "MIT OCW", "thumbnails": [],
        "entries": [{"id": "HtSuA80QTyo", "title": "Lecture 1: Peak Finding", "duration": 3202, "thumbnails": []}],
    })
    # Assigning a video kicks off real background ingestion (see main.py's admin_assign_video)
    # — a real download + Whisper run has no place in this test, and would otherwise keep
    # going as a daemon thread past this test's own completion. Stub the trigger itself
    # rather than the (already-tested-elsewhere, in test_course_ingest.py) function it calls.
    from app import course_ingest
    monkeypatch.setattr(course_ingest, "ingest_video_in_background", lambda *a, **k: None)

    r = client.post(f"/admin/course/courses/{course['id']}/playlist", json={
        "url": "https://www.youtube.com/playlist?list=PLUl4u3cNGP63WbdFxL8giv4yhgdMGaZNA",
    }, headers=admin_headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["playlist"]["title"] == "MIT 6.006"
    assert data["sync"]["added"] == 1

    week = client.post(f"/admin/course/courses/{course['id']}/weeks", json={"title": "Week 1"}, headers=admin_headers).json()
    lesson = client.post(f"/admin/course/weeks/{week['id']}/lessons", json={"title": "Peak Finding"}, headers=admin_headers).json()

    tree = client.get(f"/admin/course/courses/{course['id']}", headers=admin_headers).json()
    video_row_id = tree["unassigned_videos"][0]["id"]
    assert tree["unassigned_videos"][0]["youtube_video_id"] == "HtSuA80QTyo"

    assign = client.post(f"/admin/course/lessons/{lesson['id']}/video", json={"video_id": video_row_id}, headers=admin_headers)
    assert assign.status_code == 200

    tree = client.get(f"/admin/course/courses/{course['id']}", headers=admin_headers).json()
    assert tree["weeks"][0]["lessons"][0]["video_youtube_id"] == "HtSuA80QTyo"
    # Assigning a never-before-seen video starts ingestion in the background immediately.
    assert tree["weeks"][0]["lessons"][0]["video_ingest_status"] in ("pending", "ingesting", "ready")


def test_invalid_playlist_url_is_a_clean_422_not_a_500(client, admin_headers):
    course = client.post("/admin/course/courses", json={"title": "Course"}, headers=admin_headers).json()
    r = client.post(f"/admin/course/courses/{course['id']}/playlist", json={"url": "not a url at all"}, headers=admin_headers)
    assert r.status_code in (400, 422, 502)
    assert r.status_code != 500


def test_ai_parse_syllabus_end_to_end_with_the_llm_mocked(client, admin_headers, monkeypatch):
    from app import syllabus_ai
    from tests.test_syllabus_ai import fake_llm

    monkeypatch.setattr(syllabus_ai.router, "stream", fake_llm(
        '{"weeks": [{"title": "Week 1", "lessons": ["Peak Finding"]}]}',
    ))
    r = client.post("/admin/course/syllabus/ai_parse", json={"text": "some messy pasted notes"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"weeks": [{"title": "Week 1", "lessons": ["Peak Finding"]}]}


def test_ai_parse_syllabus_maps_no_provider_to_a_clean_503(client, admin_headers, monkeypatch):
    from app import syllabus_ai

    monkeypatch.setattr(syllabus_ai.router, "providers", [])
    r = client.post("/admin/course/syllabus/ai_parse", json={"text": "x"}, headers=admin_headers)
    assert r.status_code == 503
    assert r.json()["detail"] == "syllabus_ai_unavailable"
