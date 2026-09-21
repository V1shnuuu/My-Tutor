"""Retrieval honours the published course: once an admin publishes one, the Tutor answers
only from that course's ingested lessons — never from the static sample lectures that happen
to share a word with the question. With no course published, the whole corpus stays fair
game (the fresh-install behaviour). Found by asking "what is placement" while a
placement-prep video was on screen and getting an AVL-trees lecture back.
"""
from __future__ import annotations

import numpy as np
import pytest

from app import chat, content
from app.cache import SemanticCache
from app.corpus import Chunk, Corpus
from app.db import connect


def _corpus(chunks: list[Chunk]) -> Corpus:
    c = Corpus(root=None)  # type: ignore[arg-type]
    c.chunks = chunks
    # One-hot vectors: chunk i is a perfect match for query i, near-zero for the others.
    c.vecs = np.eye(len(chunks), 768, dtype=np.float32)
    c._by_id = {ch.chunk_id: i for i, ch in enumerate(chunks)}
    return c


CHUNKS = [
    Chunk("mit-1", "sample-mit", 0.0, 60.0, "placement of a node in an AVL tree"),
    Chunk("course-1", "yt-course", 0.0, 60.0, "campus placement interview preparation"),
    Chunk("course-2", "yt-course", 60.0, 120.0, "how to write a resume"),
]


def test_unscoped_search_can_return_any_video():
    c = _corpus(CHUNKS)
    q = np.eye(1, 768, dtype=np.float32)  # matches chunk 0, the MIT sample
    hits = c.search(q, ["placement"], k=3)
    assert hits[0].chunk.video_id == "sample-mit"


def test_scoped_search_never_returns_a_video_outside_the_course():
    c = _corpus(CHUNKS)
    q = np.eye(1, 768, dtype=np.float32)  # still the MIT chunk's exact vector
    hits = c.search(q, ["placement"], k=3, video_ids={"yt-course"})
    assert hits, "in-scope chunks must still be returned"
    assert all(h.chunk.video_id == "yt-course" for h in hits)


def test_a_scope_smaller_than_the_rank_window_still_excludes_outsiders():
    """k*5 rank positions with only 2 in-scope chunks: the zeroed-out chunk would otherwise
    still collect an RRF score from its rank position and leak back in."""
    c = _corpus(CHUNKS)
    q = np.eye(1, 768, dtype=np.float32)
    hits = c.search(q, ["placement"], k=6, video_ids={"yt-course"})
    assert {h.chunk.video_id for h in hits} == {"yt-course"}


def test_empty_scope_returns_nothing_rather_than_falling_back_to_samples():
    c = _corpus(CHUNKS)
    q = np.eye(1, 768, dtype=np.float32)
    assert c.search(q, ["placement"], k=3, video_ids=set()) == []


def test_cache_skips_an_entry_citing_a_video_outside_the_scope():
    cache = SemanticCache()
    vec = np.zeros(768, dtype=np.float32); vec[0] = 1.0
    cache.ids, cache.langs = [1], ["en"]
    cache.video_ids = [{"sample-mit"}]
    cache.vecs = vec[None, :]
    # Unscoped: the entry is a perfect similarity match, so the lookup reaches the DB read.
    # We don't need a real row for this test — a miss on the scope check must happen before.
    assert cache.lookup(vec, "en", allowed_video_ids={"yt-course"}) is None


def test_the_open_lecture_narrows_the_scope_to_itself(monkeypatch):
    """A student with one video open is asking about that video, not the whole course."""
    monkeypatch.setattr(content, "published_video_ids", lambda course_id=None: {"yt-a", "yt-b", "yt-c"})
    assert chat.scope_for("yt-b") == {"yt-b"}


def test_no_open_lecture_leaves_the_course_scope_untouched(monkeypatch):
    monkeypatch.setattr(content, "published_video_ids", lambda course_id=None: {"yt-a", "yt-b"})
    assert chat.scope_for(None) == {"yt-a", "yt-b"}


def test_an_unpublished_video_id_cannot_widen_the_scope(monkeypatch):
    """The narrowing intersects the published set — it never replaces it. A client asking
    about a video the admin hasn't published gets nothing, not that video's content."""
    monkeypatch.setattr(content, "published_video_ids", lambda course_id=None: {"yt-a"})
    assert chat.scope_for("sample-mit") == set()


def test_with_no_course_published_the_open_lecture_still_scopes(monkeypatch):
    """Fresh install: the whole corpus is fair game, but having a video open still means
    the question is about that video."""
    monkeypatch.setattr(content, "published_video_ids", lambda course_id=None: None)
    assert chat.scope_for("sample-mit") == {"sample-mit"}
    assert chat.scope_for(None) is None


def test_published_video_ids_is_none_with_no_course_and_a_set_once_published(settings):
    connect()
    for r in content.list_courses():
        content.delete_course(r["id"])
    assert content.published_video_ids() is None, "no course published → whole corpus"
    course = content.create_course("admin@example.com", "Scoped")
    content.publish_course(course["id"], True)
    assert content.published_video_ids() == set(), "published but nothing ingested → nothing answerable"
    content.delete_course(course["id"])


def test_multiple_published_courses_each_scope_independently(settings):
    """Two courses live at once: asking with course A's id never sees course B's content,
    asking with no id at all sees the union of both — never a silent guess at one of them."""
    connect()
    for r in content.list_courses():
        content.delete_course(r["id"])
    a = content.create_course("admin@example.com", "Course A")
    b = content.create_course("admin@example.com", "Course B")
    content.publish_course(a["id"], True)
    content.publish_course(b["id"], True)
    # Neither course has any ingested video yet, so each scopes to an empty set — but a
    # *draft* course's id must never widen that to "everything" (None).
    assert content.published_video_ids(a["id"]) == set()
    assert content.published_video_ids(b["id"]) == set()
    c = content.create_course("admin@example.com", "Draft, never published")
    assert content.published_video_ids(c["id"]) == set(), "a draft course's id never unscopes retrieval"
    # With two published courses and no id, the ambiguous case: never fall back to "everything".
    assert content.published_video_ids() == set()
    content.delete_course(a["id"])
    content.delete_course(b["id"])
    content.delete_course(c["id"])


def test_scope_for_narrows_within_the_picked_course(monkeypatch):
    """A student who picked course B and has one of its lectures open gets scoped to that
    lecture — course A's videos, even if scope_for(None, "a") would have included similar
    ids, never leak in just because course_id was omitted from the video-level check."""
    def fake_published_video_ids(course_id=None):
        return {"yt-a1", "yt-a2"} if course_id == "course-a" else {"yt-b1", "yt-b2"}

    monkeypatch.setattr(content, "published_video_ids", fake_published_video_ids)
    assert chat.scope_for("yt-b1", "course-b") == {"yt-b1"}
    assert chat.scope_for(None, "course-b") == {"yt-b1", "yt-b2"}
    assert chat.scope_for("yt-a1", "course-b") == set(), "a video from a different course can't widen this course's scope"
