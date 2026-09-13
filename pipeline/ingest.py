"""Video → transcript (timestamps preserved) → chunks → embeddings → per-video index shard.

Usage:
  python pipeline/ingest.py                 # ingest every video in pipeline/videos.yaml that changed
  python pipeline/ingest.py --only lec01    # one video
  python pipeline/ingest.py --force         # re-transcribe everything

videos.yaml entry:
  - id: lec01                      # stable id, used in URLs and citations
    title: "Lecture 1 — Intro"
    source: youtube                # youtube | file
    youtube_id: ZA-tUyM_y7s        # for source: youtube
    url: https://.../lec01.mp4     # for source: file (served to the player)
    transcript: whisper            # whisper | auto (YouTube captions) | file
    transcript_file: path.vtt      # for transcript: file
    lang: en                       # hint for Whisper / caption language (ar, en, fr)
    week: 1

Runs on GitHub Actions CPU (faster-whisper large-v3-turbo int8) or Kaggle GPU for bulk.
Each video is an independent shard, so adding a clip never re-indexes the others.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import yaml

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
CORPUS = ROOT / "corpus"
WORK = ROOT / "pipeline" / ".work"
VIDEOS_YAML = ROOT / "pipeline" / "videos.yaml"

TARGET_S, MIN_S, MAX_S, OVERLAP_S = 60.0, 30.0, 90.0, 15.0


# ------------------------------------------------------------------ transcripts
def ffmpeg_path() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def yt_download_audio(youtube_id: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{youtube_id}.m4a"
    if target.exists():
        return target
    import yt_dlp

    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(out_dir / f"{youtube_id}.%(ext)s"),
        "ffmpeg_location": os.path.dirname(ffmpeg_path()),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}],
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={youtube_id}"])
    if not target.exists():
        cands = list(out_dir.glob(f"{youtube_id}.*"))
        if not cands:
            raise RuntimeError("audio download failed")
        return cands[0]
    return target


def yt_auto_captions(youtube_id: str, lang: str, out_dir: Path) -> Path:
    """YouTube's own captions (manual if present, else auto). Zero compute."""
    out_dir.mkdir(parents=True, exist_ok=True)
    import yt_dlp

    opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [lang, f"{lang}-orig"],
        "subtitlesformat": "vtt",
        "outtmpl": str(out_dir / f"{youtube_id}.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={youtube_id}"])
    prefer = [out_dir / f"{youtube_id}.{lang}.vtt", out_dir / f"{youtube_id}.{lang}-orig.vtt"]
    cands = [p for p in prefer if p.exists()] or sorted(out_dir.glob(f"{youtube_id}.{lang}*.vtt"))
    if not cands:
        raise RuntimeError(f"no captions found for {youtube_id} ({lang})")
    return cands[0]


def whisper_transcribe(audio: Path, lang: str | None, vocab: str) -> list[dict]:
    from faster_whisper import WhisperModel

    model_name = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
    device = "cuda" if os.environ.get("WHISPER_DEVICE") == "cuda" else "cpu"
    compute = "float16" if device == "cuda" else "int8"
    print(f"  whisper {model_name} on {device}/{compute} …", flush=True)
    model = WhisperModel(model_name, device=device, compute_type=compute)
    segments, info = model.transcribe(
        str(audio),
        language=lang if lang in ("ar", "en", "fr") else None,
        initial_prompt=vocab[:600] or None,
        vad_filter=True,
        beam_size=5,
        word_timestamps=False,
    )
    cues = []
    for s in segments:
        text = s.text.strip()
        if text:
            cues.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": text})
    print(f"  detected language: {info.language} (p={info.language_probability:.2f}), {len(cues)} segments")
    return cues


# ------------------------------------------------------------------ VTT helpers
_TS = re.compile(r"(\d+):(\d\d):(\d\d)[.,](\d{3})|(\d\d):(\d\d)[.,](\d{3})")


def _parse_ts(s: str) -> float:
    m = _TS.match(s.strip())
    if not m:
        raise ValueError(s)
    if m.group(1) is not None:
        h, mi, se, ms = (int(m.group(i)) for i in (1, 2, 3, 4))
    else:
        h, mi, se, ms = 0, int(m.group(5)), int(m.group(6)), int(m.group(7))
    return h * 3600 + mi * 60 + se + ms / 1000


def parse_vtt(path: Path) -> list[dict]:
    """Parse WebVTT/SRT into cues; collapses YouTube auto-caption roll-up duplicates."""
    text = path.read_text(encoding="utf-8", errors="replace")
    cues: list[dict] = []
    block: list[str] = []
    for line in text.splitlines() + [""]:
        if line.strip() == "":
            if block:
                cues.extend(_cue_from_block(block))
                block = []
        else:
            block.append(line)
    # collapse roll-ups: drop a cue whose text is fully contained in the next cue's text
    out: list[dict] = []
    for c in cues:
        if out and (c["text"] in out[-1]["text"] or out[-1]["text"] in c["text"]):
            if len(c["text"]) > len(out[-1]["text"]):
                out[-1]["text"] = c["text"]
            out[-1]["end"] = max(out[-1]["end"], c["end"])
            continue
        out.append(c)
    return [c for c in out if c["text"]]


def _cue_from_block(block: list[str]) -> list[dict]:
    for i, line in enumerate(block):
        if "-->" in line:
            a, b = line.split("-->")[:2]
            b = b.strip().split(" ")[0]
            body = " ".join(block[i + 1:])
            body = re.sub(r"<[^>]+>", "", body)        # strip <c> timing tags
            body = re.sub(r"\s+", " ", body).strip()
            if not body:
                return []
            return [{"start": _parse_ts(a), "end": _parse_ts(b), "text": body}]
    return []


def _fmt(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def write_vtt(cues: list[dict], path: Path) -> None:
    lines = ["WEBVTT", ""]
    for c in cues:
        lines += [f"{_fmt(c['start'])} --> {_fmt(c['end'])}", c["text"], ""]
    path.write_text("\n".join(lines), encoding="utf-8")


# ------------------------------------------------------------------ chunking
def chunk_cues(cues: list[dict], video_id: str) -> list[dict]:
    """Time-windowed chunks (~60 s, 30–90 s) aligned to cue boundaries with ~15 s overlap.
    Every chunk keeps t_start/t_end so the answer can link back into the player."""
    chunks = []
    i, n = 0, len(cues)
    while i < n:
        start = cues[i]["start"]
        j = i
        while j < n and (cues[j]["end"] - start) < TARGET_S:
            j += 1
        # extend to a sentence end if close, but never beyond MAX_S
        while j < n and (cues[j]["end"] - start) < MAX_S and not re.search(r"[.!?؟。]\s*$", cues[j - 1]["text"]):
            j += 1
        j = max(j, i + 1)
        window = cues[i:j]
        text = " ".join(c["text"] for c in window).strip()
        chunks.append({
            "chunk_id": f"{video_id}:{len(chunks):04d}",
            "video_id": video_id,
            "t_start": round(window[0]["start"], 2),
            "t_end": round(window[-1]["end"], 2),
            "text": text,
        })
        # next window starts ~OVERLAP_S before this one's end
        end = window[-1]["end"]
        k = j
        while k - 1 > i and cues[k - 1]["start"] > end - OVERLAP_S:
            k -= 1
        i = k if k > i else j
    # merge a tiny trailing chunk into the previous one
    if len(chunks) >= 2 and (chunks[-1]["t_end"] - chunks[-1]["t_start"]) < MIN_S / 2:
        last = chunks.pop()
        chunks[-1]["text"] += " " + last["text"]
        chunks[-1]["t_end"] = last["t_end"]
    return chunks


# ------------------------------------------------------------------ driver
def fingerprint(v: dict) -> str:
    keys = {k: v.get(k) for k in ("id", "source", "youtube_id", "url", "transcript", "transcript_file", "lang")}
    h = hashlib.sha256(json.dumps(keys, sort_keys=True).encode()).hexdigest()[:12]
    if v.get("transcript") == "file" and v.get("transcript_file"):
        p = ROOT / v["transcript_file"]
        if p.exists():
            h += "-" + hashlib.sha256(p.read_bytes()).hexdigest()[:8]
    return h


def ingest_video(v: dict, vocab: str, force: bool) -> bool:
    vid = v["id"]
    idx = CORPUS / "index"
    tr = CORPUS / "transcripts"
    idx.mkdir(parents=True, exist_ok=True)
    tr.mkdir(parents=True, exist_ok=True)
    fp = fingerprint(v)
    meta_path = idx / f"{vid}.meta.json"
    if meta_path.exists() and not force:
        old = json.loads(meta_path.read_text(encoding="utf-8"))
        if old.get("fingerprint") == fp:
            print(f"- {vid}: unchanged, skipping")
            return False
    print(f"- {vid}: {v.get('title')}")
    t0 = time.time()
    mode = v.get("transcript", "whisper")
    if mode == "file":
        cues = parse_vtt(ROOT / v["transcript_file"])
    elif mode == "auto":
        cues = parse_vtt(yt_auto_captions(v["youtube_id"], v.get("lang", "en"), WORK / "subs"))
    else:
        if v.get("source") == "youtube":
            audio = yt_download_audio(v["youtube_id"], WORK / "audio")
        else:
            audio = Path(v["url"]) if not str(v["url"]).startswith("http") else _download(v["url"], WORK / "audio")
        cues = whisper_transcribe(audio, v.get("lang"), vocab)
    if not cues:
        raise RuntimeError(f"{vid}: empty transcript")

    write_vtt(cues, tr / f"{vid}.vtt")
    (tr / f"{vid}.cues.json").write_text(json.dumps(cues, ensure_ascii=False), encoding="utf-8")
    chunks = chunk_cues(cues, vid)
    from app.embed import embed_passages

    vecs = embed_passages([c["text"] for c in chunks])
    np.save(idx / f"{vid}.vecs.npy", vecs)
    with open(idx / f"{vid}.chunks.jsonl", "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    meta_path.write_text(json.dumps({
        "fingerprint": fp, "cues": len(cues), "chunks": len(chunks), "duration": cues[-1]["end"],
        "ingested_at": int(time.time()), "mode": mode,
    }), encoding="utf-8")
    print(f"  {len(cues)} cues → {len(chunks)} chunks in {time.time() - t0:.0f}s")
    return True


def _download(url: str, out_dir: Path) -> Path:
    import httpx

    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / hashlib.sha1(url.encode()).hexdigest()[:12]
    if not target.exists():
        with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r, open(target, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    return target


def write_manifest(videos: list[dict]) -> None:
    idx = CORPUS / "index"
    entries = []
    parts = []
    for v in videos:
        meta_path = idx / f"{v['id']}.meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        chunks_sha = hashlib.sha256((idx / f"{v['id']}.chunks.jsonl").read_bytes()).hexdigest()[:8]
        parts.append(f"{v['id']}:{meta['fingerprint']}:{chunks_sha}")
        entries.append({
            "id": v["id"], "title": v.get("title", v["id"]), "source": v.get("source", "youtube"),
            "youtube_id": v.get("youtube_id"), "url": v.get("url"), "duration": meta.get("duration"),
            "lang": v.get("lang"), "week": v.get("week"),
        })
    version = hashlib.sha256("|".join(sorted(parts)).encode()).hexdigest()[:10]
    (CORPUS / "manifest.json").write_text(json.dumps({"corpus_version": version, "videos": entries}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"manifest: {len(entries)} videos, corpus_version={version}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    cfg = yaml.safe_load(VIDEOS_YAML.read_text(encoding="utf-8"))
    videos = cfg["videos"]
    vocab_file = CORPUS / "vocabulary.txt"
    vocab = vocab_file.read_text(encoding="utf-8") if vocab_file.exists() else ""
    changed = 0
    for v in videos:
        if args.only and v["id"] != args.only:
            continue
        try:
            changed += ingest_video(v, vocab, args.force)
        except Exception as e:
            print(f"  !! {v['id']} failed: {e}")
    write_manifest(videos)
    print(f"done: {changed} shard(s) updated")


if __name__ == "__main__":
    main()
