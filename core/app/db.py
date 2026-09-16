"""SQLite: roster/enrollment codes, per-student usage, semantic cache, provider usage.
Single-process, WAL mode. Backed up continuously by Litestream in production."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

from .config import settings

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
  id TEXT PRIMARY KEY,           -- opaque student id (sub in JWT)
  label TEXT,                    -- optional name / roster label
  code TEXT UNIQUE NOT NULL,     -- enrollment code (single use unless reusable)
  redeemed_at INTEGER,           -- unix seconds
  device_id TEXT,                -- bound on first redeem
  created_at INTEGER NOT NULL,
  reusable INTEGER NOT NULL DEFAULT 0  -- 1 = never binds to a device (teacher/demo code)
);
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
"""


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = settings.data_dir / "tutor.sqlite"
        _conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _migrate(_conn)
    return _conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that CREATE TABLE IF NOT EXISTS cannot add to an existing database.

    Every install that predates a column has a real tutor.sqlite with real students in it,
    so the schema string alone would silently never reach them.
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(students)")}
    if "reusable" not in have:
        conn.execute("ALTER TABLE students ADD COLUMN reusable INTEGER NOT NULL DEFAULT 0")


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
