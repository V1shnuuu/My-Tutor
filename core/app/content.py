"""Admin-authored course content: courses, weeks, lessons, connected YouTube playlists, and
the lesson-to-video mapping the student dashboard reads. Mirrors conversations.py's shape —
plain functions, explicit ownership/scope checks, no ORM.

Exactly one course is ever "published" at a time (see db.py's schema comment): the student
dashboard has no course switcher, so `published_course()` is the one query it actually needs.
"""
from __future__ import annotations

import time
import uuid

from fastapi import HTTPException

from .db import query, tx

TITLE_MAX = 200


# ---------------------------------------------------------------- courses
def create_course(admin_email: str, title: str, description: str = "") -> dict:
    now = int(time.time())
    cid = uuid.uuid4().hex
    with tx() as c:
        c.execute(
            "INSERT INTO courses (id, title, description, status, created_by, created_at, updated_at)"
            " VALUES (?, ?, ?, 'draft', ?, ?, ?)",
            (cid, title[:TITLE_MAX], description, admin_email, now, now),
        )
    return get_course(cid)


def list_courses() -> list[dict]:
    rows = query("SELECT id, title, description, status, created_at, updated_at FROM courses ORDER BY updated_at DESC")
    return [dict(r) for r in rows]


def get_course(course_id: str) -> dict:
    rows = query("SELECT id, title, description, status, created_at, updated_at FROM courses WHERE id = ?", (course_id,))
    if not rows:
        raise HTTPException(404, "course_not_found")
    return dict(rows[0])


def update_course(course_id: str, title: str | None = None, description: str | None = None) -> dict:
    get_course(course_id)  # 404s cleanly if missing
    sets, params = [], []
    if title is not None:
        sets.append("title = ?"); params.append(title[:TITLE_MAX])
    if description is not None:
        sets.append("description = ?"); params.append(description)
    if sets:
        sets.append("updated_at = ?"); params.append(int(time.time()))
        params.append(course_id)
        with tx() as c:
            c.execute(f"UPDATE courses SET {', '.join(sets)} WHERE id = ?", params)
    return get_course(course_id)


def publish_course(course_id: str, published: bool) -> dict:
    get_course(course_id)
    status = "published" if published else "draft"
    with tx() as c:
        if published:
            # One published course at a time: publishing this one un-publishes any other,
            # rather than leaving the student dashboard to guess which of several to show.
            c.execute("UPDATE courses SET status = 'draft' WHERE status = 'published' AND id != ?", (course_id,))
        c.execute("UPDATE courses SET status = ?, updated_at = ? WHERE id = ?", (status, int(time.time()), course_id))
    return get_course(course_id)


def delete_course(course_id: str) -> None:
    get_course(course_id)
    with tx() as c:
        c.execute("DELETE FROM courses WHERE id = ?", (course_id,))  # cascades weeks/lessons/playlists/videos


def published_course() -> dict | None:
    rows = query("SELECT id, title, description, status, created_at, updated_at FROM courses WHERE status = 'published' LIMIT 1")
    return dict(rows[0]) if rows else None


# ---------------------------------------------------------------- weeks
def _next_position(table: str, fk_col: str, fk_val: str) -> int:
    rows = query(f"SELECT COALESCE(MAX(position), -1) AS m FROM {table} WHERE {fk_col} = ?", (fk_val,))
    return rows[0]["m"] + 1


def add_week(course_id: str, title: str) -> dict:
    get_course(course_id)
    wid = uuid.uuid4().hex
    pos = _next_position("weeks", "course_id", course_id)
    with tx() as c:
        c.execute("INSERT INTO weeks (id, course_id, title, position) VALUES (?, ?, ?, ?)", (wid, course_id, title[:TITLE_MAX], pos))
    return {"id": wid, "course_id": course_id, "title": title[:TITLE_MAX], "position": pos}


def rename_week(week_id: str, title: str) -> None:
    if not query("SELECT 1 FROM weeks WHERE id = ?", (week_id,)):
        raise HTTPException(404, "week_not_found")
    with tx() as c:
        c.execute("UPDATE weeks SET title = ? WHERE id = ?", (title[:TITLE_MAX], week_id))


def delete_week(week_id: str) -> None:
    if not query("SELECT 1 FROM weeks WHERE id = ?", (week_id,)):
        raise HTTPException(404, "week_not_found")
    with tx() as c:
        c.execute("DELETE FROM weeks WHERE id = ?", (week_id,))  # cascades lessons


def reorder_weeks(course_id: str, week_ids_in_order: list[str]) -> None:
    """`week_ids_in_order` must be exactly the course's current week ids — a partial or
    foreign list would silently leave some weeks with a stale position instead of failing."""
    have = {r["id"] for r in query("SELECT id FROM weeks WHERE course_id = ?", (course_id,))}
    if set(week_ids_in_order) != have:
        raise HTTPException(400, "reorder_must_include_every_week_exactly_once")
    with tx() as c:
        for i, wid in enumerate(week_ids_in_order):
            c.execute("UPDATE weeks SET position = ? WHERE id = ?", (i, wid))


# ---------------------------------------------------------------- lessons
def add_lesson(week_id: str, title: str) -> dict:
    if not query("SELECT 1 FROM weeks WHERE id = ?", (week_id,)):
        raise HTTPException(404, "week_not_found")
    lid = uuid.uuid4().hex
    pos = _next_position("lessons", "week_id", week_id)
    with tx() as c:
        c.execute("INSERT INTO lessons (id, week_id, title, position, video_id) VALUES (?, ?, ?, ?, NULL)", (lid, week_id, title[:TITLE_MAX], pos))
    return {"id": lid, "week_id": week_id, "title": title[:TITLE_MAX], "position": pos, "video_id": None}


def rename_lesson(lesson_id: str, title: str) -> None:
    if not query("SELECT 1 FROM lessons WHERE id = ?", (lesson_id,)):
        raise HTTPException(404, "lesson_not_found")
    with tx() as c:
        c.execute("UPDATE lessons SET title = ? WHERE id = ?", (title[:TITLE_MAX], lesson_id))


def delete_lesson(lesson_id: str) -> None:
    if not query("SELECT 1 FROM lessons WHERE id = ?", (lesson_id,)):
        raise HTTPException(404, "lesson_not_found")
    with tx() as c:
        c.execute("DELETE FROM lessons WHERE id = ?", (lesson_id,))


def reorder_lessons(week_id: str, lesson_ids_in_order: list[str]) -> None:
    have = {r["id"] for r in query("SELECT id FROM lessons WHERE week_id = ?", (week_id,))}
    if set(lesson_ids_in_order) != have:
        raise HTTPException(400, "reorder_must_include_every_lesson_exactly_once")
    with tx() as c:
        for i, lid in enumerate(lesson_ids_in_order):
            c.execute("UPDATE lessons SET position = ? WHERE id = ?", (i, lid))


def assign_video(lesson_id: str, video_id: str | None) -> None:
    """video_id=None explicitly un-assigns — "not assigned yet" is a real, supported state,
    not just the absence of a call."""
    if not query("SELECT 1 FROM lessons WHERE id = ?", (lesson_id,)):
        raise HTTPException(404, "lesson_not_found")
    if video_id is not None and not query("SELECT 1 FROM youtube_videos WHERE id = ?", (video_id,)):
        raise HTTPException(404, "video_not_found")
    with tx() as c:
        c.execute("UPDATE lessons SET video_id = ? WHERE id = ?", (video_id, lesson_id))


# ---------------------------------------------------------------- playlists / videos
def set_playlist(course_id: str, meta: dict) -> dict:
    """Replaces any playlist already connected to this course — a course has at most one."""
    get_course(course_id)
    existing = query("SELECT id FROM youtube_playlists WHERE course_id = ?", (course_id,))
    pid = existing[0]["id"] if existing else uuid.uuid4().hex
    now = int(time.time())
    with tx() as c:
        if existing:
            c.execute(
                "UPDATE youtube_playlists SET youtube_playlist_id = ?, title = ?, channel_title = ?, thumbnail = ?, synced_at = ? WHERE id = ?",
                (meta["id"], meta["title"], meta["channel_title"], meta["thumbnail"], now, pid),
            )
        else:
            c.execute(
                "INSERT INTO youtube_playlists (id, course_id, youtube_playlist_id, title, channel_title, thumbnail, synced_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pid, course_id, meta["id"], meta["title"], meta["channel_title"], meta["thumbnail"], now),
            )
    return get_playlist(course_id)


def get_playlist(course_id: str) -> dict | None:
    rows = query("SELECT id, youtube_playlist_id, title, channel_title, thumbnail, synced_at FROM youtube_playlists WHERE course_id = ?", (course_id,))
    return dict(rows[0]) if rows else None


def sync_videos(playlist_id: str, fetched: list[dict]) -> dict:
    """Upserts by (playlist_id, youtube_video_id) — see db.py's schema comment for why the
    real YouTube id can't be a global key — so an existing lesson→video mapping (which points
    at this row's synthetic id) survives a resync even if every other column (title,
    thumbnail, position) changed. Videos no longer in the playlist are left in the table
    rather than deleted, so a lesson pointing at one doesn't suddenly dangle — they just stop
    appearing in "videos found" counts going forward; `removed` reports their YouTube ids so
    the admin UI can flag them instead of the API silently forgetting a mapping existed.
    `fetched` items use "id" for the YouTube video id, matching youtube.py's return shape.
    """
    have = {r["youtube_video_id"]: r["id"] for r in query("SELECT id, youtube_video_id FROM youtube_videos WHERE playlist_id = ?", (playlist_id,))}
    fetched_yt_ids = {v["id"] for v in fetched}
    added, updated = 0, 0
    with tx() as c:
        for v in fetched:
            if v["id"] in have:
                c.execute(
                    "UPDATE youtube_videos SET title = ?, thumbnail = ?, position = ?, duration_s = ?, published_at = ? WHERE id = ?",
                    (v["title"], v["thumbnail"], v["position"], v.get("duration_s"), v.get("published_at"), have[v["id"]]),
                )
                updated += 1
            else:
                c.execute(
                    "INSERT INTO youtube_videos (id, playlist_id, youtube_video_id, title, thumbnail, position, duration_s, published_at, ingest_status)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                    (uuid.uuid4().hex, playlist_id, v["id"], v["title"], v["thumbnail"], v["position"], v.get("duration_s"), v.get("published_at")),
                )
                added += 1
        c.execute("UPDATE youtube_playlists SET synced_at = ? WHERE id = ?", (int(time.time()), playlist_id))
    removed = sorted(set(have) - fetched_yt_ids)
    return {"added": added, "updated": updated, "removed": removed, "total": len(fetched)}


def list_videos(playlist_id: str) -> list[dict]:
    rows = query(
        "SELECT id, youtube_video_id, title, thumbnail, position, duration_s, published_at, ingest_status, ingest_error"
        " FROM youtube_videos WHERE playlist_id = ? ORDER BY position",
        (playlist_id,),
    )
    return [dict(r) for r in rows]


def unassigned_videos(course_id: str) -> list[dict]:
    """Playlist videos no lesson currently points at — shown to the admin to assign manually,
    never auto-forced into a lesson (a playlist commonly has more videos than the syllabus:
    recitations, office hours, extras)."""
    pl = get_playlist(course_id)
    if not pl:
        return []
    rows = query(
        "SELECT v.id, v.youtube_video_id, v.title, v.thumbnail, v.position, v.ingest_status FROM youtube_videos v"
        " WHERE v.playlist_id = ? AND v.id NOT IN ("
        "   SELECT video_id FROM lessons WHERE video_id IS NOT NULL"
        " ) ORDER BY v.position",
        (pl["id"],),
    )
    return [dict(r) for r in rows]


def all_ready_videos() -> list[dict]:
    """Every video, across every course/playlist, that has finished ingestion — the source
    course_ingest.py rebuilds corpus/manifest.json from. Deliberately not scoped to the
    published course: a shard stays live once built even if its course later gets unpublished,
    so re-publishing it doesn't mean re-transcribing everything from scratch."""
    rows = query("SELECT youtube_video_id, title, corpus_video_id FROM youtube_videos WHERE ingest_status = 'ready' AND corpus_video_id IS NOT NULL")
    return [dict(r) for r in rows]


def published_video_ids() -> set[str] | None:
    """The corpus video ids the Tutor is allowed to answer from right now.

    None means "no course is published — the whole corpus is fair game", which is how a
    fresh install with only the static sample lectures always behaved. Once an admin
    publishes a course, this is exactly the set of that course's lessons whose video has
    finished ingesting: the Lessons list tells the student "this is what you can ask about",
    and this is what makes retrieval honour that instead of also matching every sample
    lecture that happens to share a word with the question. An empty set (course published,
    nothing ingested yet) is a legitimate answer — it means nothing is answerable yet, not
    "fall back to the samples"."""
    course = published_course()
    if not course:
        return None
    rows = query(
        "SELECT DISTINCT v.corpus_video_id FROM lessons l"
        " JOIN weeks w ON w.id = l.week_id"
        " JOIN youtube_videos v ON v.id = l.video_id"
        " WHERE w.course_id = ? AND v.ingest_status = 'ready' AND v.corpus_video_id IS NOT NULL",
        (course["id"],),
    )
    return {r["corpus_video_id"] for r in rows}


def set_video_ingest_status(video_id: str, status: str, error: str | None = None, corpus_video_id: str | None = None) -> None:
    with tx() as c:
        c.execute(
            "UPDATE youtube_videos SET ingest_status = ?, ingest_error = ?, corpus_video_id = COALESCE(?, corpus_video_id) WHERE id = ?",
            (status, error, corpus_video_id, video_id),
        )


# ---------------------------------------------------------------- full-tree reads
def _course_tree(course_id: str) -> dict:
    course = get_course(course_id)
    weeks = query("SELECT id, title, position FROM weeks WHERE course_id = ? ORDER BY position", (course_id,))
    out_weeks = []
    for w in weeks:
        lessons = query(
            "SELECT l.id, l.title, l.position, l.video_id,"
            " v.youtube_video_id AS video_youtube_id, v.title AS video_title,"
            " v.thumbnail AS video_thumbnail, v.duration_s AS video_duration_s,"
            " v.ingest_status AS video_ingest_status, v.ingest_error AS video_ingest_error,"
            " v.corpus_video_id AS video_corpus_id"
            " FROM lessons l LEFT JOIN youtube_videos v ON v.id = l.video_id"
            " WHERE l.week_id = ? ORDER BY l.position",
            (w["id"],),
        )
        out_weeks.append({"id": w["id"], "title": w["title"], "position": w["position"], "lessons": [dict(r) for r in lessons]})
    playlist = get_playlist(course_id)
    return {**course, "weeks": out_weeks, "playlist": playlist}


def admin_course_tree(course_id: str) -> dict:
    """Everything the admin editor needs, including unpublished drafts and per-lesson
    ingest status — never served to students."""
    tree = _course_tree(course_id)
    tree["unassigned_videos"] = unassigned_videos(course_id)
    return tree


def student_course_tree() -> dict | None:
    """The published course, shaped for the student dashboard: only lessons with a video that
    has actually finished ingesting are usable for the Tutor, but every lesson (assigned or
    not, ingested or not) is shown — "not assigned yet" is a real state, not hidden."""
    course = published_course()
    if not course:
        return None
    return _course_tree(course["id"])
