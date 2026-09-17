"""Bridges the Admin Dashboard's "Connect Playlist" / "Assign Video" actions to the same
transcribe → chunk → embed pipeline pipeline/ingest.py already uses for the static, curated
corpus — reused directly (ingest_video, write_manifest), not reimplemented, per the standing
rule that grounding is a code guarantee, not a UI convenience: a lesson is only ever answerable
by the Tutor once its video has actually been through this, same as any other video always was.

Runs in a background thread per video (real transcription is minutes, not seconds, on this
hardware) so the admin request that triggers it returns immediately; content.py's
ingest_status column is how the admin UI polls progress instead of blocking on it.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import yaml

from . import content
from .config import settings

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))  # pipeline/ is a sibling of core/, not under it

CORPUS_ID_PREFIX = "yt-"  # namespaced so an admin-added video's shard id can never collide
                          # with a static pipeline/videos.yaml id (e.g. "sample-6006-l01")


def corpus_video_id(youtube_video_id: str) -> str:
    return f"{CORPUS_ID_PREFIX}{youtube_video_id}"


def _already_ingested(cvid: str) -> bool:
    from .corpus import corpus

    meta = corpus.root / "index" / f"{cvid}.meta.json"
    return meta.exists()


def ingest_video_in_background(row_id: str, youtube_video_id: str, title: str) -> None:
    """Fire-and-forget: content.py's ingest_status is the API this is observed through, not
    a return value or an exception the caller waits on."""
    t = threading.Thread(target=_run, args=(row_id, youtube_video_id, title), daemon=True)
    t.start()


def _run(row_id: str, youtube_video_id: str, title: str) -> None:
    cvid = corpus_video_id(youtube_video_id)
    if _already_ingested(cvid):
        # The exact same real video is already a live shard — reached via a different
        # playlist/course row, most likely. Point this row at it and skip straight to ready:
        # re-transcribing identical audio would waste real GPU/CPU minutes for no new grounding.
        content.set_video_ingest_status(row_id, "ready", corpus_video_id=cvid)
        _rebuild_manifest_and_reload()
        return
    content.set_video_ingest_status(row_id, "ingesting")
    try:
        from pipeline.ingest import ingest_video  # local import: pulls in yt_dlp/whisper lazily

        vocab_file = settings.corpus_dir / "vocabulary.txt"
        vocab = vocab_file.read_text(encoding="utf-8") if vocab_file.exists() else ""
        # No lang hint: this project's own STT work already found that a wrong forced hint
        # produces worse transcripts than letting Whisper auto-detect (core/app/stt.py does
        # the same) — an admin course can mix languages across a playlist with no per-video
        # language field to force one anyway.
        entry = {
            "id": cvid, "title": title, "source": "youtube", "youtube_id": youtube_video_id,
            "transcript": "whisper", "lang": None,
        }
        ingest_video(entry, vocab, force=False)
    except Exception as e:
        content.set_video_ingest_status(row_id, "failed", error=str(e)[:500])
        return
    content.set_video_ingest_status(row_id, "ready", corpus_video_id=cvid)
    _rebuild_manifest_and_reload()


def _rebuild_manifest_and_reload() -> None:
    """Manifest = the static curated videos.yaml list UNION every admin video that has ever
    reached `ready` across every course — write_manifest() itself only includes entries whose
    shard actually exists on disk, so it is safe to hand it every known id and let it filter.
    """
    from pipeline.ingest import VIDEOS_YAML, write_manifest

    static = []
    if VIDEOS_YAML.exists():
        cfg = yaml.safe_load(VIDEOS_YAML.read_text(encoding="utf-8")) or {}
        static = cfg.get("videos", [])

    admin_entries = []
    for row in content.all_ready_videos():
        admin_entries.append({
            "id": row["corpus_video_id"], "title": row["title"], "source": "youtube",
            "youtube_id": row["youtube_video_id"], "lang": None, "week": None,
        })
    write_manifest(static + admin_entries)
    _reload_corpus()


def _reload_corpus() -> None:
    from .cache import semantic_cache
    from .corpus import corpus

    corpus.load()
    semantic_cache.load(corpus.version)
