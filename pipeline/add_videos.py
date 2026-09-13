"""Register a folder of video clips as course lectures.

  python pipeline/add_videos.py --dir "C:/Users/you/Downloads/uploads" --lang ar
  python pipeline/add_videos.py --dir ./clips --lang en --week 2 --dry-run

Copies each clip into web/public/media/ (so the browser can actually play it, with
seeking) and appends one entry per clip to pipeline/videos.yaml. Then run
`python pipeline/ingest.py` to transcribe and index them.

Clips are copied rather than linked because the player loads them over HTTP from the
web app's own origin: a path like C:\\Users\\... is openable by the ingest process but
means nothing to a browser. Pass --no-copy to leave them where they are (the entry then
carries `file:` for ingest, and you arrange serving yourself).

web/public/media/ is gitignored — the clips stay out of the repo; only the transcripts
and index shards under corpus/ get committed.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

import yaml

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
VIDEOS_YAML = ROOT / "pipeline" / "videos.yaml"
MEDIA_DIR = ROOT / "web" / "public" / "media"
EXTS = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".mp3", ".m4a", ".wav"}


def slug(name: str) -> str:
    """A stable id from a filename: lowercase, no spaces, letters kept in any script.

    Arabic filenames are the common case for this course, so \\w (unicode) rather than
    a-z — stripping non-ascii turned "محاضرة 3.mp4" into the id "3", and every
    Arabic-titled clip in a folder into a collision.
    """
    s = re.sub(r"[^\w]+", "-", name, flags=re.UNICODE).strip("-").lower()
    s = re.sub(r"-{2,}", "-", s)
    if not s:
        return "clip"
    # Ids name files and appear in citations; a bare number reads as an accident.
    return f"clip-{s}" if not any(ch.isalpha() for ch in s) else s


def title_of(name: str) -> str:
    s = re.sub(r"[_-]+", " ", name).strip()
    s = re.sub(r"\s{2,}", " ", s)
    return s[:1].upper() + s[1:] if s else name


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, help="folder holding the clips")
    ap.add_argument("--lang", default="en", choices=["ar", "en", "fr"], help="spoken language (primes Whisper)")
    ap.add_argument("--week", type=int, help="week number for all of these")
    ap.add_argument("--prefix", default="", help="prepend to every generated id, e.g. w2-")
    ap.add_argument("--no-copy", action="store_true", help="leave clips in place; reference them with file:")
    ap.add_argument("--dry-run", action="store_true", help="show what would be added, write nothing")
    args = ap.parse_args()

    src_dir = Path(args.dir).expanduser()
    if not src_dir.is_dir():
        print(f"not a folder: {src_dir}")
        return 1

    clips = sorted(p for p in src_dir.iterdir() if p.is_file() and p.suffix.lower() in EXTS)
    if not clips:
        print(f"no video or audio files in {src_dir} (looked for {', '.join(sorted(EXTS))})")
        return 1

    doc = yaml.safe_load(VIDEOS_YAML.read_text(encoding="utf-8")) or {}
    videos = doc.get("videos") or []
    known = {v.get("id") for v in videos}

    added, skipped = [], []
    for clip in clips:
        vid = f"{args.prefix}{slug(clip.stem)}"
        if vid in known:
            # Same id from a different file (two clips whose names differ only in
            # punctuation) must not silently overwrite the first one's shard.
            if not any(vid == e["id"] and c.name == clip.name for e, c in added):
                n = 2
                while f"{vid}-{n}" in known:
                    n += 1
                vid = f"{vid}-{n}"
            else:
                skipped.append(vid)
                continue
        known.add(vid)
        entry = {
            "id": vid,
            "title": title_of(clip.stem),
            "source": "file",
            "transcript": "whisper",
            "lang": args.lang,
        }
        if args.no_copy:
            entry["file"] = str(clip)
            entry["url"] = ""  # nothing to play until you serve it yourself
        else:
            entry["url"] = f"/media/{vid}{clip.suffix.lower()}"
        if args.week is not None:
            entry["week"] = args.week
        videos.append(entry)
        added.append((entry, clip))

    if skipped:
        print(f"already registered, left alone: {', '.join(skipped)}")
    if not added:
        print("nothing new to add.")
        return 0

    for entry, clip in added:
        size_mb = clip.stat().st_size / 1e6
        where = entry.get("file") or f"web/public{entry['url']}"
        print(f"  + {entry['id']:<28} {size_mb:7.1f} MB  {entry['lang']}  -> {where}")

    if args.dry_run:
        print(f"\n--dry-run: {len(added)} entr{'y' if len(added) == 1 else 'ies'} not written.")
        return 0

    if not args.no_copy:
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        for entry, clip in added:
            dest = MEDIA_DIR / Path(entry["url"]).name
            if not dest.exists() or dest.stat().st_size != clip.stat().st_size:
                shutil.copy2(clip, dest)

    doc["videos"] = videos
    VIDEOS_YAML.write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    print(f"\nwrote {len(added)} entr{'y' if len(added) == 1 else 'ies'} to {VIDEOS_YAML.relative_to(ROOT)}")
    print("next: python pipeline/ingest.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
