"""Content import for the Admin Dashboard's "Connect" flow — via yt-dlp, the same library
`pipeline/ingest.py` already uses to download lecture audio. No API key, no quota, nothing
to configure: the admin pastes a public playlist OR single-video URL and this reads
exactly what YouTube already shows anyone with the link.

A single video is stored the same way a playlist is (one row in youtube_playlists, its
videos in youtube_videos) so the rest of content.py — sync, assign-to-lesson, disconnect —
needs no separate code path. What tells the two apart later (on resync) is the stored
`youtube_playlist_id` itself: a single video's is prefixed `video:`, see `_VIDEO_PREFIX`.
"""
from __future__ import annotations

import asyncio
import re

from app.config import settings


def _base_opts() -> dict:
    """Shared yt-dlp options for every extraction call. `cookiefile` is omitted entirely
    (not passed as None/"") when unset — yt-dlp treats a present-but-empty cookiefile as an
    error, not "no cookies", so this only adds the key when there's an actual file to use."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    if settings.youtube_cookies_file:
        opts["cookiefile"] = settings.youtube_cookies_file
    return opts


_PLAYLIST_PATTERNS = [
    re.compile(r"(?:[?&]|^)list=([A-Za-z0-9_-]+)"),
    re.compile(r"^((?:PL|UU|FL|LL)[A-Za-z0-9_-]+)$"),
]
_VIDEO_PATTERNS = [
    re.compile(r"(?:[?&]|^)v=([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube\.com/(?:shorts|embed)/([A-Za-z0-9_-]{11})"),
    re.compile(r"^([A-Za-z0-9_-]{11})$"),
]

_VIDEO_PREFIX = "video:"
_SKIP_TITLES = {"[private video]", "[deleted video]"}


class YouTubeError(RuntimeError):
    def __init__(self, message: str, *, code: str = "youtube_error"):
        super().__init__(message)
        self.code = code


def extract_playlist_id(text: str) -> str:
    """Accepts a full playlist URL, a bare `list=` param, or an already-bare id."""
    text = text.strip()
    for pat in _PLAYLIST_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    raise YouTubeError("That doesn't look like a YouTube playlist URL or id.", code="invalid_playlist_url")


def extract_video_id(text: str) -> str:
    """Accepts a full watch/shorts/embed/youtu.be URL, or an already-bare 11-char video id."""
    text = text.strip()
    for pat in _VIDEO_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    raise YouTubeError("That doesn't look like a YouTube video or playlist URL.", code="invalid_url")


def extract_source(text: str) -> tuple[str, str]:
    """A playlist URL (has `list=`, or is a bare playlist id) is checked first since a playlist
    URL often also carries a `v=` param (an "opened from" video) that would otherwise be
    mistaken for the actual target. Returns ("playlist", id) or ("video", id)."""
    try:
        return "playlist", extract_playlist_id(text)
    except YouTubeError:
        return "video", extract_video_id(text)


def _best_thumbnail(thumbs: list[dict] | None) -> str:
    if not thumbs:
        return ""
    # yt-dlp lists thumbnails smallest-first; the last one is the highest resolution available.
    return thumbs[-1].get("url", "")


def _extract(playlist_id: str) -> dict:
    """Blocking network call — always run via asyncio.to_thread, never awaited directly."""
    import yt_dlp

    url = f"https://www.youtube.com/playlist?list={playlist_id}"
    opts = {**_base_opts(), "extract_flat": "in_playlist", "ignoreerrors": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).lower()
        if "does not exist" in msg or "private" in msg or "unavailable" in msg:
            raise YouTubeError("That playlist doesn't exist, or it's private.", code="playlist_not_found") from e
        if "sign in to confirm" in msg or "not a bot" in msg:
            raise YouTubeError(
                "YouTube is blocking this server's IP as a bot. Set YOUTUBE_COOKIES_FILE "
                "to a cookies.txt exported from a real signed-in browser and restart.",
                code="youtube_bot_check",
            ) from e
        raise YouTubeError(f"Couldn't read that playlist: {e}", code="youtube_upstream_error") from e
    if info is None:
        raise YouTubeError("That playlist doesn't exist, or it's private.", code="playlist_not_found")
    return info


async def fetch_playlist(playlist_id: str) -> dict:
    """Playlist metadata: title, channel, thumbnail. Raises YouTubeError (not_found) if the
    playlist doesn't exist or is private."""
    info = await asyncio.to_thread(_extract, playlist_id)
    return {
        "id": playlist_id,
        "title": info.get("title") or "Untitled playlist",
        "channel_title": info.get("channel") or info.get("uploader") or "",
        "thumbnail": _best_thumbnail(info.get("thumbnails")),
    }


async def fetch_playlist_videos(playlist_id: str) -> list[dict]:
    """Every video in the playlist, in playlist order, with duration and thumbnail — all
    included in the single flat-extraction pass above, so this reuses it rather than making
    a second request per video."""
    info = await asyncio.to_thread(_extract, playlist_id)
    items: list[dict] = []
    for i, e in enumerate(info.get("entries") or []):
        if not e:
            continue  # a dead/removed entry yt-dlp couldn't resolve at all
        vid = e.get("id")
        title = (e.get("title") or "").strip()
        if not vid or title.lower() in _SKIP_TITLES:
            continue  # a playlist can reference videos the owner removed/hid; skip, don't crash
        items.append({
            "id": vid,
            "title": title or vid,
            "thumbnail": _best_thumbnail(e.get("thumbnails")) or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "position": i,
            "duration_s": e.get("duration"),
            "published_at": None,
        })
    return items


def _extract_video(video_id: str) -> dict:
    """Blocking network call — always run via asyncio.to_thread, never awaited directly."""
    import yt_dlp

    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(_base_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).lower()
        if "unavailable" in msg or "private" in msg or "removed" in msg:
            raise YouTubeError("That video doesn't exist, or it's private.", code="video_not_found") from e
        if "sign in to confirm" in msg or "not a bot" in msg:
            raise YouTubeError(
                "YouTube is blocking this server's IP as a bot. Set YOUTUBE_COOKIES_FILE "
                "to a cookies.txt exported from a real signed-in browser and restart.",
                code="youtube_bot_check",
            ) from e
        raise YouTubeError(f"Couldn't read that video: {e}", code="youtube_upstream_error") from e
    if info is None:
        raise YouTubeError("That video doesn't exist, or it's private.", code="video_not_found")
    return info


async def fetch_video(video_id: str) -> dict:
    """A single video's metadata, shaped exactly like fetch_playlist's return so main.py can
    pass either straight into content.set_playlist unchanged. `id` is prefixed so a later
    resync (main.py's admin_sync_playlist) knows to re-fetch it as a video, not a playlist —
    see the module docstring."""
    info = await asyncio.to_thread(_extract_video, video_id)
    return {
        "id": _VIDEO_PREFIX + video_id,
        "title": info.get("title") or "Untitled video",
        "channel_title": info.get("channel") or info.get("uploader") or "",
        "thumbnail": _best_thumbnail(info.get("thumbnails")) or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
    }


async def fetch_video_as_list(video_id: str) -> list[dict]:
    """A single video reshaped as the one-item list content.sync_videos expects — same shape
    fetch_playlist_videos returns for each of a playlist's entries."""
    info = await asyncio.to_thread(_extract_video, video_id)
    title = (info.get("title") or "").strip()
    return [{
        "id": video_id,
        "title": title or video_id,
        "thumbnail": _best_thumbnail(info.get("thumbnails")) or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "position": 0,
        "duration_s": info.get("duration"),
        "published_at": None,
    }]


def is_single_video_source(stored_youtube_playlist_id: str) -> bool:
    return stored_youtube_playlist_id.startswith(_VIDEO_PREFIX)


def strip_video_prefix(stored_youtube_playlist_id: str) -> str:
    return stored_youtube_playlist_id[len(_VIDEO_PREFIX):]
