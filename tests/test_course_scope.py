"""Retrieval honours the published course: once an admin publishes one, the Tutor answers
only from that course's ingested lessons — never from the static sample lectures that happen
to share a word with the question. With no course published, the whole corpus stays fair
game (the fresh-install behaviour). Found by asking "what is placement" while a
placement-prep video was on screen and getting an AVL-trees lecture back.
"""
from __future__ import annotations

import numpy as np
import pytest

from app import content
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


def test_published_video_ids_is_none_with_no_course_and_a_set_once_published(settings):
    connect()
    for r in content.list_courses():
        content.delete_course(r["id"])
    assert content.published_video_ids() is None, "no course published → whole corpus"
    course = content.create_course("admin@example.com", "Scoped")
    content.publish_course(course["id"], True)
    assert content.published_video_ids() == set(), "published but nothing ingested → nothing answerable"
    content.delete_course(course["id"])
