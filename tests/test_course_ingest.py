"""course_ingest.py's bridge logic — dedup-by-real-video-id, status transitions, and manifest
rebuilding — with the actual transcribe/embed call (pipeline.ingest.ingest_video) mocked out.
No real Whisper run here; that's covered by this project's existing corpus/ingest tests and by
manually running pipeline/ingest.py itself. This is specifically about the *bridging* logic:
does the right thing happen in the database and does the real pipeline function get called
with the right arguments — not once or twice.
"""
from __future__ import annotations

import json

import pytest

from app import content, course_ingest
from app.db import connect


@pytest.fixture(autouse=True)
def _db():
    connect()
    yield


@pytest.fixture
def video_row():
    c = content.create_course("admin@example.com", "Course")
    content.set_playlist(c["id"], {"id": "PLxyz", "title": "T", "channel_title": "", "thumbnail": ""})
    pl = content.get_playlist(c["id"])
    content.sync_videos(pl["id"], [{"id": "vid-new", "title": "Lecture 1", "thumbnail": "", "position": 0, "duration_s": None, "published_at": None}])
    return content.list_videos(pl["id"])[0]


def test_a_never_before_seen_video_actually_gets_ingested(video_row, monkeypatch, tmp_path):
    calls = []
    warm_model = object()

    def fake_ingest_video(entry, vocab, force, model=None):
        calls.append((entry, model))
        # Simulate the real function's side effect: it writes the meta.json shard.
        idx = tmp_path / "index"
        idx.mkdir(exist_ok=True)
        (idx / f"{entry['id']}.meta.json").write_text("{}", encoding="utf-8")
        return True

    monkeypatch.setattr("pipeline.ingest.ingest_video", fake_ingest_video)
    monkeypatch.setattr(course_ingest, "_already_ingested", lambda cvid: False)
    monkeypatch.setattr(course_ingest, "_rebuild_manifest_and_reload", lambda: None)
    # The live STT model is what admin ingestion must transcribe with — it's already loaded
    # on the configured device. Stand in for it rather than loading a real one in a test.
    from app import stt
    monkeypatch.setattr(stt, "local_available", lambda: True)
    monkeypatch.setattr(stt, "_local_whisper", lambda: warm_model)

    course_ingest._run(video_row["id"], "vid-new", "Lecture 1")

    assert len(calls) == 1
    entry, model = calls[0]
    assert entry["youtube_id"] == "vid-new"
    assert entry["id"] == "yt-vid-new"
    assert entry["lang"] is None  # never forces a language hint — see the module docstring
    assert model is warm_model, "must reuse the already-loaded STT model, not build a CPU one"

    from app.db import query
    updated = query("SELECT ingest_status, corpus_video_id FROM youtube_videos WHERE id = ?", (video_row["id"],))[0]
    assert updated["ingest_status"] == "ready"
    assert updated["corpus_video_id"] == "yt-vid-new"


def test_a_video_already_ingested_elsewhere_is_not_re_transcribed(video_row, monkeypatch):
    calls = []
    monkeypatch.setattr("pipeline.ingest.ingest_video", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(course_ingest, "_already_ingested", lambda cvid: True)
    monkeypatch.setattr(course_ingest, "_rebuild_manifest_and_reload", lambda: None)

    course_ingest._run(video_row["id"], "vid-new", "Lecture 1")

    assert calls == [], "the real (expensive) ingest_video must not run when the shard already exists"
    from app.db import query
    updated = query("SELECT ingest_status, corpus_video_id FROM youtube_videos WHERE id = ?", (video_row["id"],))[0]
    assert updated["ingest_status"] == "ready"
    assert updated["corpus_video_id"] == "yt-vid-new"


def test_a_failed_ingestion_is_recorded_not_silently_dropped(video_row, monkeypatch):
    def boom(entry, vocab, force, model=None):
        raise RuntimeError("yt-dlp: video unavailable")

    monkeypatch.setattr("pipeline.ingest.ingest_video", boom)
    monkeypatch.setattr(course_ingest, "_already_ingested", lambda cvid: False)
    from app import stt
    monkeypatch.setattr(stt, "local_available", lambda: False)

    course_ingest._run(video_row["id"], "vid-new", "Lecture 1")

    from app.db import query
    updated = query("SELECT ingest_status, ingest_error FROM youtube_videos WHERE id = ?", (video_row["id"],))[0]
    assert updated["ingest_status"] == "failed"
    assert "video unavailable" in updated["ingest_error"]


def test_a_failed_video_can_be_retried_and_a_stale_ingesting_one_is_reset_at_boot(video_row, monkeypatch):
    """Two dead ends that used to be permanent: a failed video had no retry path, and a
    video left 'ingesting' by a restart (its daemon thread died with the process) was
    refused by retry as already-in-progress, forever."""
    from fastapi.testclient import TestClient

    from app import auth
    from app.config import settings as s
    from app.db import query
    from app.main import app

    calls = []
    monkeypatch.setattr(course_ingest, "ingest_video_in_background", lambda *a, **k: calls.append(a))
    before, s.admin_emails = s.admin_emails, "admin@example.com"
    auth.upsert_user("admin@example.com", "Admin", "")
    headers = {"authorization": f"Bearer {auth.issue_user_token('admin@example.com')}"}
    try:
        content.set_video_ingest_status(video_row["id"], "ingesting")
        with TestClient(app) as client:  # startup runs here: stale 'ingesting' → 'failed'
            row = query("SELECT ingest_status, ingest_error FROM youtube_videos WHERE id = ?", (video_row["id"],))[0]
            assert row["ingest_status"] == "failed"
            assert "restart" in row["ingest_error"]

            r = client.post(f"/admin/course/videos/{video_row['id']}/retry_ingest", headers=headers)
            assert r.status_code == 200
            assert len(calls) == 1 and calls[0][0] == video_row["id"]
            assert query("SELECT ingest_status FROM youtube_videos WHERE id = ?", (video_row["id"],))[0]["ingest_status"] == "pending"

            content.set_video_ingest_status(video_row["id"], "ingesting")
            assert client.post(f"/admin/course/videos/{video_row['id']}/retry_ingest", headers=headers).status_code == 409
            assert client.post("/admin/course/videos/no-such-id/retry_ingest", headers=headers).status_code == 404
    finally:
        s.admin_emails = before


def test_corpus_video_id_is_namespaced_so_it_cannot_collide_with_the_static_corpus():
    assert course_ingest.corpus_video_id("abc123").startswith("yt-")
    assert course_ingest.corpus_video_id("abc123") != "abc123"
