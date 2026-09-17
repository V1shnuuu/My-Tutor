"""SQLite: per-student usage, semantic cache, provider usage, courses, and conversations.
Single-process, WAL mode. Backed up continuously by Litestream in production."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

from .config import settings

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
  student_id TEXT NOT NULL,
  day TEXT NOT NULL,             -- YYYY-MM-DD (UTC)
  messages INTEGER NOT NULL DEFAULT 0,
  llm_calls INTEGER NOT NULL DEFAULT 0,
  stt_calls INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (student_id, day)
);
CREATE TABLE IF NOT EXISTS cache (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lang TEXT NOT NULL,
  question TEXT NOT NULL,
  answer TEXT NOT NULL,
  citations TEXT NOT NULL,       -- JSON
  embedding BLOB NOT NULL,       -- float32[dim]
  corpus_version TEXT NOT NULL,
  video_ids TEXT NOT NULL,       -- JSON list, for eviction on re-index
  hits INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_usage (
  provider TEXT NOT NULL,
  day TEXT NOT NULL,
  requests INTEGER NOT NULL DEFAULT 0,
  tokens INTEGER NOT NULL DEFAULT 0,
  errors INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (provider, day)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  kind TEXT NOT NULL,            -- chat | cache_hit | gate_refuse | floor | queue | stt | error
  lang TEXT,
  ms INTEGER,
  detail TEXT
);
CREATE TABLE IF NOT EXISTS users (
  email TEXT PRIMARY KEY,        -- verified Google account, the identity everything hangs off
  name TEXT,
  picture TEXT,
  created_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,           -- uuid4 hex, generated server-side
  user_email TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',-- first question, trimmed; shown in the history list
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  FOREIGN KEY (user_email) REFERENCES users (email) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations (user_email, updated_at DESC);
CREATE TABLE IF NOT EXISTS conv_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL,
  role TEXT NOT NULL,            -- user | assistant
  content TEXT NOT NULL,
  lang TEXT,
  citations TEXT NOT NULL DEFAULT '[]',  -- JSON, so a resumed chat still has its timestamps
  source TEXT,                   -- llm | cache | floor | refusal
  ts INTEGER NOT NULL,
  FOREIGN KEY (conversation_id) REFERENCES conversations (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_conv_messages ON conv_messages (conversation_id, id);

-- Admin-authored courses: syllabus (weeks/lessons) + a connected YouTube playlist, mapped
-- lesson-by-lesson to specific videos. Deliberately one course "published" at a time — the
-- student UI has no course switcher, mirroring the single flat corpus it already renders.
CREATE TABLE IF NOT EXISTS courses (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',   -- draft | published
  created_by TEXT NOT NULL,               -- admin email
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS weeks (
  id TEXT PRIMARY KEY,
  course_id TEXT NOT NULL,
  title TEXT NOT NULL,
  position INTEGER NOT NULL,
  FOREIGN KEY (course_id) REFERENCES courses (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_weeks_course ON weeks (course_id, position);
CREATE TABLE IF NOT EXISTS lessons (
  id TEXT PRIMARY KEY,
  week_id TEXT NOT NULL,
  title TEXT NOT NULL,
  position INTEGER NOT NULL,
  video_id TEXT,                          -- FK to youtube_videos.id, nullable: "not assigned yet"
  FOREIGN KEY (week_id) REFERENCES weeks (id) ON DELETE CASCADE,
  FOREIGN KEY (video_id) REFERENCES youtube_videos (id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_lessons_week ON lessons (week_id, position);
CREATE TABLE IF NOT EXISTS youtube_playlists (
  id TEXT PRIMARY KEY,
  course_id TEXT NOT NULL,
  youtube_playlist_id TEXT NOT NULL,      -- the stable id from the URL, e.g. PLxxxx
  title TEXT NOT NULL,
  channel_title TEXT NOT NULL DEFAULT '',
  thumbnail TEXT NOT NULL DEFAULT '',
  synced_at INTEGER,
  FOREIGN KEY (course_id) REFERENCES courses (id) ON DELETE CASCADE
);
-- `id` is a synthetic row key, NOT the YouTube video id: the same real video can legitimately
-- appear in two different playlists (a shared lecture reused across two course offerings, or
-- two draft courses both pointing at the same MIT OCW playlist), so the real id
-- (youtube_video_id) can only be unique *within one playlist*, not globally. lessons.video_id
-- references this synthetic id — one specific playlist's copy of the video — while ingestion
-- keys the actual corpus/ shard by youtube_video_id, so re-syncing/re-using the same real
-- video never re-transcribes audio that is already ingested under another playlist's row.
CREATE TABLE IF NOT EXISTS youtube_videos (
  id TEXT PRIMARY KEY,
  playlist_id TEXT NOT NULL,
  youtube_video_id TEXT NOT NULL,
  title TEXT NOT NULL,
  thumbnail TEXT NOT NULL DEFAULT '',
  position INTEGER NOT NULL,
  duration_s REAL,
  published_at TEXT,
  -- pending: fetched from YouTube, not yet transcribed. ingesting: the real Whisper/embed
  -- pipeline is running now. ready: corpus_video_id is a live, queryable shard. failed: see
  -- ingest_error; the lesson stays playable, the Tutor just can't ground answers on it yet.
  ingest_status TEXT NOT NULL DEFAULT 'pending',
  ingest_error TEXT,
  corpus_video_id TEXT,                   -- the id this video was written to corpus/ under, once ready
  FOREIGN KEY (playlist_id) REFERENCES youtube_playlists (id) ON DELETE CASCADE,
  UNIQUE (playlist_id, youtube_video_id)
);
CREATE INDEX IF NOT EXISTS idx_yt_videos_playlist ON youtube_videos (playlist_id, position);
CREATE INDEX IF NOT EXISTS idx_yt_videos_ytid ON youtube_videos (youtube_video_id);
"""


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = settings.data_dir / "tutor.sqlite"
        _conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        # Off by default in SQLite. The course/week/lesson/playlist/video tables lean on
        # ON DELETE CASCADE/SET NULL rather than manual cascading deletes, so this has to be
        # on for those to actually fire.
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
    return _conn


@contextmanager
def tx():
    conn = connect()
    with _lock:
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = connect()
    with _lock:
        return conn.execute(sql, params).fetchall()


def log_event(kind: str, lang: str | None = None, ms: int | None = None, detail: str | None = None) -> None:
    import time

    with tx() as c:
        c.execute(
            "INSERT INTO events (ts, kind, lang, ms, detail) VALUES (?, ?, ?, ?, ?)",
            (int(time.time()), kind, lang, ms, detail),
        )
