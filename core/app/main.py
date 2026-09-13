"""Adaptive Tutor core API."""
from __future__ import annotations

import csv
import io
import json
import time
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from . import auth, stt, tts
from .cache import semantic_cache
from .chat import run_chat
from .config import settings
from .corpus import corpus
from .db import connect, query
from .router import router

app = FastAPI(title="Adaptive Tutor Core", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.allowed_origins.split(",") if o.strip()],
    allow_origin_regex=r"https://.*\.pages\.dev",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

STARTED = time.time()
VOCAB_FILE = settings.corpus_dir / "vocabulary.txt"


@app.on_event("startup")
def _startup() -> None:
    connect()
    corpus.load()
    semantic_cache.load(corpus.version)
    # Warm the embedding model so the first student does not pay the load time.
    try:
        from .embed import embed_queries

        embed_queries(["warmup"])
    except Exception as e:  # model download may be pending on first boot
        print("embedding warmup failed:", e)
    # Warm server voices so the first spoken answer does not pay the ONNX load.
    for lang, ok in tts.availability().items():
        if ok:
            try:
                tts._load(lang)
                tts.prepare_text("warmup", lang)
            except Exception as e:
                print("tts warmup failed:", lang, e)


# ---------------------------------------------------------------- health / meta
@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "uptime_s": int(time.time() - STARTED),
        "corpus_version": corpus.version,
        "chunks": corpus.size,
        "videos": len(corpus.videos),
        "providers": [p.id for p in router.providers],
        "stt": stt.budget()["available"],
    }


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    """Prometheus text format for Grafana Cloud free tier."""
    lines = [f"tutor_uptime_seconds {int(time.time() - STARTED)}", f"tutor_corpus_chunks {corpus.size}", f"tutor_queue_depth {router.queue_depth}"]
    for p in router.snapshot()["providers"]:
        lab = f'provider="{p["id"]}"'
        if p["rpd"]:
            lines.append(f"tutor_provider_budget_used_ratio{{{lab},window=\"rpd\"}} {p['rpd_used'] / p['rpd']:.4f}")
        if p["tpd"]:
            lines.append(f"tutor_provider_budget_used_ratio{{{lab},window=\"tpd\"}} {p['tpd_used'] / p['tpd']:.4f}")
        lines.append(f"tutor_provider_errors_total{{{lab}}} {p['errors']}")
    b = stt.budget()
    lines.append(f"tutor_stt_budget_used_ratio {b['used'] / max(1, b['cap']):.4f}")
    day = auth.today()
    for kind in ("chat", "cache_hit", "gate_refuse", "floor", "queue", "stt"):
        n = query("SELECT COUNT(*) AS n FROM events WHERE kind = ? AND ts > ?", (kind, int(time.time()) - 86400))[0]["n"]
        lines.append(f'tutor_events_24h{{kind="{kind}"}} {n}')
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- auth
class RedeemIn(BaseModel):
    code: str
    device_id: str = Field(min_length=8, max_length=64)


@app.post("/auth/redeem")
def redeem(body: RedeemIn):
    token = auth.redeem(body.code, body.device_id)
    return {"token": token}


@app.get("/me")
def me(student=auth.Student):
    day = auth.today()
    rows = query("SELECT messages FROM usage WHERE student_id = ? AND day = ?", (student["sub"], day))
    used = rows[0]["messages"] if rows else 0
    return {"id": student["sub"], "budget": {"used": used, "cap": settings.student_daily_cap}, "stt": stt.budget()["available"], "tts": tts.availability()}


# ---------------------------------------------------------------- corpus
@app.get("/videos")
def videos(student=auth.Student):
    return {"corpus_version": corpus.version, "videos": [v.__dict__ for v in corpus.videos.values()]}


@app.get("/videos/{video_id}/captions.vtt")
def captions(video_id: str):
    path = settings.corpus_dir / "transcripts" / f"{video_id}.vtt"
    if not path.exists():
        raise HTTPException(404, "no_captions")
    return FileResponse(path, media_type="text/vtt")


@app.get("/videos/{video_id}/transcript")
def transcript(video_id: str, student=auth.Student):
    """Cue list for the in-app caption overlay (works with the YouTube IFrame too)."""
    path = settings.corpus_dir / "transcripts" / f"{video_id}.cues.json"
    if not path.exists():
        raise HTTPException(404, "no_transcript")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- chat
class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=800)
    history: list[dict] = Field(default_factory=list)
    prev_lang: str | None = None


@app.post("/chat")
async def chat(body: ChatIn, student=auth.Student):
    budget = auth.check_fair_share(student["sub"])
    gen = run_chat(student["sub"], body.message, body.history, body.prev_lang, budget)
    return StreamingResponse(gen, media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- stt
@app.get("/stt/budget")
def stt_budget(student=auth.Student):
    return stt.budget()


@app.post("/stt")
async def stt_endpoint(file: UploadFile = File(...), lang_hint: str | None = Form(None), student=auth.Student):
    audio = await file.read()
    if len(audio) > 4_000_000:
        raise HTTPException(413, "audio_too_large")
    vocab = VOCAB_FILE.read_text(encoding="utf-8") if VOCAB_FILE.exists() else ""
    result = await stt.transcribe(audio, file.filename or "audio.webm", file.content_type or "audio/webm", lang_hint, vocab)
    auth.bump_usage(student["sub"], stt_calls=1)
    return result


# ---------------------------------------------------------------- tts (server fallback voice)
class TtsIn(BaseModel):
    text: str = Field(min_length=1, max_length=600)
    lang: str = Field(pattern="^(ar|en|fr)$")


@app.get("/tts/voices")
def tts_voices(student=auth.Student):
    return tts.availability()


@app.post("/tts")
async def tts_endpoint(body: TtsIn, student=auth.Student):
    if not tts.available(body.lang):
        raise HTTPException(404, "no_voice")
    wav, duration = await tts.synthesize(body.text, body.lang)
    return Response(wav, media_type="audio/wav", headers={"X-Duration": f"{duration:.3f}", "Cache-Control": "private, max-age=86400"})


# ---------------------------------------------------------------- admin
@app.post("/admin/codes")
def admin_codes(request: Request, n: int = 0, labels: UploadFile | None = File(None)):
    """Generate enrollment codes. Either ?n=400 or upload a one-column CSV of labels."""
    auth.require_admin(request)
    names: list[str] = []
    if labels is not None:
        text = labels.file.read().decode("utf-8-sig")
        names = [row[0].strip() for row in csv.reader(io.StringIO(text)) if row and row[0].strip()]
    count = n or len(names)
    if count <= 0:
        raise HTTPException(400, "need_n_or_csv")
    rows = auth.create_students(count, names or None)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["label", "code"])
    for r in rows:
        w.writerow([r["label"], r["code"]])
    return PlainTextResponse(out.getvalue(), media_type="text/csv")


@app.post("/admin/reload")
def admin_reload(request: Request):
    """Hot-swap the corpus after the pipeline commits new shards; evict cache for changed videos."""
    auth.require_admin(request)
    before = dict(corpus.shard_mtimes)
    corpus.load()
    changed = [vid for vid, m in corpus.shard_mtimes.items() if before.get(vid) != m] + [vid for vid in before if vid not in corpus.shard_mtimes]
    evicted = semantic_cache.evict_videos(changed)
    semantic_cache.load(corpus.version)
    return {"corpus_version": corpus.version, "chunks": corpus.size, "changed": changed, "cache_evicted": evicted}


@app.get("/admin/status")
def admin_status(request: Request):
    auth.require_admin(request)
    day_ago = int(time.time()) - 86400
    counts = {r["kind"]: r["n"] for r in query("SELECT kind, COUNT(*) AS n FROM events WHERE ts > ? GROUP BY kind", (day_ago,))}
    lat = query("SELECT kind, AVG(ms) AS avg_ms FROM events WHERE ts > ? AND ms IS NOT NULL GROUP BY kind", (day_ago,))
    return {
        "corpus": {"version": corpus.version, "chunks": corpus.size, "videos": len(corpus.videos)},
        "router": router.snapshot(),
        "stt": stt.budget(),
        "events_24h": counts,
        "avg_ms_24h": {r["kind"]: int(r["avg_ms"]) for r in lat},
        "cache_entries": query("SELECT COUNT(*) AS n FROM cache")[0]["n"],
        "students": query("SELECT COUNT(*) AS n, SUM(redeemed_at IS NOT NULL) AS redeemed FROM students")[0],
    }
