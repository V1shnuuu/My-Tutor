"""Playlist import for the Admin Dashboard's "Connect Playlist" flow — via yt-dlp, the same
library `pipeline/ingest.py` already uses to download lecture audio. No API key, no quota,
nothing to configure: the admin pastes a public playlist URL and this reads exactly what
`https://www.youtube.com/playlist?list=...` already shows to anyone with the link.
"""
from __future__ import annotations

import asyncio
import re

_PATTERNS = [
    re.compile(r"(?:[?&]|^)list=([A-Za-z0-9_-]+)"),
    re.compile(r"^((?:PL|UU|FL|LL)[A-Za-z0-9_-]+)$"),
]

_SKIP_TITLES = {"[private video]", "[deleted video]"}


class YouTubeError(RuntimeError):
    def __init__(self, message: str, *, code: str = "youtube_error"):
        super().__init__(message)
        self.code = code


def extract_playlist_id(text: str) -> str:
    """Accepts a full playlist URL, a bare `list=` param, or an already-bare id."""
    text = text.strip()
    for pat in _PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    raise YouTubeError("That doesn't look like a YouTube playlist URL or id.", code="invalid_playlist_url")


def _best_thumbnail(thumbs: list[dict] | None) -> str:
    if not thumbs:
        return ""
    # yt-dlp lists thumbnails smallest-first; the last one is the highest resolution available.
    return thumbs[-1].get("url", "")


def _extract(playlist_id: str) -> dict:
    """Blocking network call — always run via asyncio.to_thread, never awaited directly."""
    import yt_dlp

    url = f"https://www.youtube.com/playlist?list={playlist_id}"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).lower()
        if "does not exist" in msg or "private" in msg or "unavailable" in msg:
            raise YouTubeError("That playlist doesn't exist, or it's private.", code="playlist_not_found") from e
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
