"""YouTube playlist import: URL parsing and the actual playlist read, mocked at the
yt_dlp.YoutubeDL layer — no real network, no API key, matching how yt-dlp itself is only
ever invoked through `YoutubeDL(...).extract_info(...)`.
"""
from __future__ import annotations

import asyncio

import pytest
import yt_dlp

from app.youtube import YouTubeError, extract_playlist_id, fetch_playlist, fetch_playlist_videos


# ---------------------------------------------------------------- extract_playlist_id
@pytest.mark.parametrize("text,expected", [
    ("https://www.youtube.com/playlist?list=PLUl4u3cNGP63WbdFxL8giv4yhgdMGaZNA", "PLUl4u3cNGP63WbdFxL8giv4yhgdMGaZNA"),
    ("https://www.youtube.com/watch?v=abc123&list=PLxyz789", "PLxyz789"),
    ("list=PLxyz789", "PLxyz789"),
    ("PLUl4u3cNGP63WbdFxL8giv4yhgdMGaZNA", "PLUl4u3cNGP63WbdFxL8giv4yhgdMGaZNA"),
    ("  https://youtube.com/playlist?list=PLabc  ", "PLabc"),  # stray whitespace, pasted from a chat
])
def test_extracts_playlist_id_from_every_realistic_shape(text, expected):
    assert extract_playlist_id(text) == expected


def test_rejects_garbage_input():
    with pytest.raises(YouTubeError) as e:
        extract_playlist_id("this is not a url at all")
    assert e.value.code == "invalid_playlist_url"


def test_rejects_a_plain_video_url_not_a_playlist():
    with pytest.raises(YouTubeError):
        extract_playlist_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ")


# ---------------------------------------------------------------- reading a playlist (mocked)
def _mock_extract(monkeypatch, result=None, error: Exception | None = None):
    """Stands in for yt_dlp.YoutubeDL(...).extract_info(...) — the one blocking call
    core/app/youtube.py makes, run inside asyncio.to_thread."""
    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            if error:
                raise error
            return result

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)


def test_fetch_playlist_not_found_when_extraction_returns_nothing(monkeypatch):
    _mock_extract(monkeypatch, result=None)
    with pytest.raises(YouTubeError) as e:
        asyncio.run(fetch_playlist("PLdoesnotexist"))
    assert e.value.code == "playlist_not_found"


def test_fetch_playlist_not_found_maps_the_download_error_message(monkeypatch):
    _mock_extract(monkeypatch, error=yt_dlp.utils.DownloadError("This playlist does not exist."))
    with pytest.raises(YouTubeError) as e:
        asyncio.run(fetch_playlist("PLdoesnotexist"))
    assert e.value.code == "playlist_not_found"


def test_an_unrelated_download_error_is_a_generic_upstream_error(monkeypatch):
    _mock_extract(monkeypatch, error=yt_dlp.utils.DownloadError("network reset by peer"))
    with pytest.raises(YouTubeError) as e:
        asyncio.run(fetch_playlist("PLxyz"))
    assert e.value.code == "youtube_upstream_error"


def test_fetch_playlist_metadata(monkeypatch):
    _mock_extract(monkeypatch, result={
        "title": "MIT 6.006", "channel": "MIT OpenCourseWare",
        "thumbnails": [{"url": "https://img/small.jpg"}, {"url": "https://img/thumb.jpg"}],
        "entries": [],
    })
    meta = asyncio.run(fetch_playlist("PLUl4u3cNGP63WbdFxL8giv4yhgdMGaZNA"))
    assert meta == {
        "id": "PLUl4u3cNGP63WbdFxL8giv4yhgdMGaZNA", "title": "MIT 6.006",
        "channel_title": "MIT OpenCourseWare", "thumbnail": "https://img/thumb.jpg",
    }


def test_fetch_playlist_videos_preserves_order_and_reads_durations(monkeypatch):
    def entry(vid, title, duration):
        return {"id": vid, "title": title, "duration": duration, "thumbnails": []}

    _mock_extract(monkeypatch, result={
        "title": "X", "entries": [
            entry("vid1", "Lecture 1: Peak Finding", 3202),
            entry("vid2", "Lecture 2: Document Distance", 2890),
        ],
    })
    videos = asyncio.run(fetch_playlist_videos("PLxyz"))
    assert [v["id"] for v in videos] == ["vid1", "vid2"]
    assert [v["title"] for v in videos] == ["Lecture 1: Peak Finding", "Lecture 2: Document Distance"]
    assert videos[0]["duration_s"] == 3202
    assert videos[1]["duration_s"] == 2890
    assert videos[0]["position"] == 0
    assert videos[1]["position"] == 1


def test_fetch_playlist_videos_skips_private_deleted_and_dead_entries(monkeypatch):
    _mock_extract(monkeypatch, result={
        "title": "X", "entries": [
            {"id": "vid1", "title": "Real video", "duration": 60, "thumbnails": []},
            {"id": "vid2", "title": "[Private video]", "duration": None, "thumbnails": []},
            {"id": "vid3", "title": "[Deleted video]", "duration": None, "thumbnails": []},
            None,  # yt-dlp represents an entry it couldn't resolve at all as None
        ],
    })
    videos = asyncio.run(fetch_playlist_videos("PLxyz"))
    assert [v["id"] for v in videos] == ["vid1"]


def test_fetch_playlist_videos_falls_back_to_a_default_thumbnail(monkeypatch):
    _mock_extract(monkeypatch, result={
        "title": "X", "entries": [{"id": "vid1", "title": "No thumbs", "duration": 10, "thumbnails": []}],
    })
    videos = asyncio.run(fetch_playlist_videos("PLxyz"))
    assert videos[0]["thumbnail"] == "https://i.ytimg.com/vi/vid1/hqdefault.jpg"
