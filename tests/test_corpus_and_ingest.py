"""The index itself, and getting course video into it.

A silently wrong index is the worst failure this project has: retrieval still returns
something, the gate still passes it, and the tutor confidently cites the wrong minute.
"""
from __future__ import annotations

import numpy as np
import pytest


# ---------------------------------------------------------------- the index
def test_vectors_are_normalised(loaded_corpus):
    """Cosine is computed as a plain dot product, and the gate thresholds are tuned to
    that. Un-normalised vectors would silently shift every score."""
    norms = np.linalg.norm(loaded_corpus.vecs, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-3)


def test_every_chunk_is_its_own_nearest_neighbour(loaded_corpus):
    """Catches the shard bug that matters: chunks.jsonl and vecs.npy drifting out of
    order, which points every citation at the wrong timestamp."""
    V = loaded_corpus.vecs
    mismatched = [i for i in range(len(loaded_corpus.chunks)) if int(np.argmax(V @ V[i])) != i]
    assert not mismatched, f"chunks not matching their own vector: {mismatched[:5]}"


def test_search_puts_the_right_chunk_first(loaded_corpus, settings):
    """A question that is a passage's own words must cite that passage — or a chunk saying
    the same thing.

    Lectures repeat themselves: a slide read twice, a worked example counting "6 6 7 7 8",
    an overlapping chunk window. Two chunks seconds apart can carry near-identical text, and
    which of them wins top-1 is then a coin toss between equally correct citations. Requiring
    the exact chunk there tests the tie-break, not retrieval, so accept any hit whose text
    matches the source passage; a genuinely wrong passage still fails."""
    norm = lambda s: " ".join(s.split()).casefold()
    for i in (0, 10, 25, 40, 60):
        if i >= len(loaded_corpus.chunks):
            continue
        chunk = loaded_corpus.chunks[i]
        hits = loaded_corpus.search(loaded_corpus.vecs[i:i + 1].astype(np.float32),
                                    [chunk.text[:200]], settings.top_k)
        assert hits, f"chunk {i} returned nothing"
        top, want = hits[0].chunk, norm(chunk.text)
        same_passage = norm(top.text) in (want,) or norm(top.text) in want or want in norm(top.text)
        assert abs(top.t_start - chunk.t_start) < 0.01 or same_passage, (
            f"chunk {i} (t={chunk.t_start:.0f}s) ranked behind an unrelated passage at "
            f"t={top.t_start:.0f}s: {top.text[:60]!r}")


def test_chunks_carry_usable_timestamps(loaded_corpus):
    for c in loaded_corpus.chunks:
        assert c.t_end > c.t_start, "a citation needs a window, not a point"
        assert c.text.strip(), "an empty chunk can be retrieved but never answers anything"


# ---------------------------------------------------------------- add_videos
@pytest.fixture
def add_videos():
    import add_videos as mod

    return mod


def test_slug_keeps_arabic(add_videos):
    """This course is taught in Egyptian Arabic, so Arabic filenames are the common case.
    Slugging on [a-z0-9] turned "محاضرة 3" into the id "3" and collapsed every
    Arabic-titled clip in a folder into one colliding id."""
    assert add_videos.slug("محاضرة 3") == "محاضرة-3"
    assert add_videos.slug("Lecture 01 - Introduction") == "lecture-01-introduction"


def test_slug_never_yields_a_bare_number(add_videos):
    """Ids name files and show up in citations; "3" reads as an accident."""
    out = add_videos.slug("3")
    assert out == "clip-3"
    assert add_videos.slug("!!!") == "clip"


def test_titles_stay_readable(add_videos):
    assert add_videos.title_of("lecture_02_arrays") == "Lecture 02 arrays"


# ---------------------------------------------------------------- media resolution
@pytest.fixture
def ingest():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("ingest_mod", root / "pipeline" / "ingest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_served_url_resolves_to_the_file_on_disk(ingest, tmp_path, monkeypatch):
    """`url` is what the browser plays; ingest has to find the same clip on disk. Before
    this split, ingest opened `url` as a path — so a Windows path satisfied ingest and
    was unplayable, and a served path played and broke ingest."""
    media = tmp_path / "media"
    media.mkdir()
    (media / "lec01.mp4").write_bytes(b"not really a video")
    monkeypatch.setattr(ingest, "WEB_PUBLIC", tmp_path)

    got = ingest.source_media({"id": "lec01", "source": "file", "url": "/media/lec01.mp4"})
    assert got == media / "lec01.mp4"


def test_explicit_file_key_wins(ingest, tmp_path):
    clip = tmp_path / "somewhere-else.mp4"
    clip.write_bytes(b"video")
    got = ingest.source_media({"id": "x", "source": "file", "url": "/media/x.mp4", "file": str(clip)})
    assert got == clip


def test_missing_media_names_the_field_to_fix(ingest, tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "WEB_PUBLIC", tmp_path)
    with pytest.raises(RuntimeError) as e:
        ingest.source_media({"id": "gone", "source": "file", "url": "/media/gone.mp4"})
    assert "file:" in str(e.value), "the error has to say which field to set"


def test_no_source_at_all_is_explained(ingest):
    with pytest.raises(RuntimeError) as e:
        ingest.source_media({"id": "empty", "source": "file"})
    assert "file:" in str(e.value) or "url:" in str(e.value)
