"""Adaptive Tutor core API."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import threading
import time
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from . import auth, conversations, liveavatar, stt, tts
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
    # Warm the local model. Ollama loads several GB on the first request and unloads again
    # after idling, so without this the first student of a session waits for the load on top
    # of their answer. Set OLLAMA_KEEP_ALIVE so it stays resident afterwards.
    if settings.local_llm_enabled:
        threading.Thread(target=_warm_local_llm, daemon=True).start()
    # Same reasoning for local STT: loading a CUDA model in particular is several seconds
    # (weights to VRAM, cuDNN/cuBLAS init) that the first student to speak would otherwise pay
    # on top of their question. Backgrounded like the LLM warmup, since it can be slow enough
    # to matter and must not delay the server accepting requests.
    if settings.local_stt_enabled:
        threading.Thread(target=_warm_local_stt, daemon=True).start()
    # Warm server voices so the first spoken answer does not pay the ONNX load.
    for lang, ok in tts.availability().items():
        if ok:
            try:
                tts._load(lang)
                tts.prepare_text("warmup", lang)
            except Exception as e:
                print("tts warmup failed:", lang, e)


def _warm_local_llm() -> None:
    """Load the model into the server's memory, off the startup path.

    One token is enough to force the load; the answer is thrown away. Failure is normal and
    silent-ish here — no Ollama running is a supported state, and `doctor` is where that
    gets reported properly.
    """
    try:
        httpx.post(
            f"{settings.local_llm_base_url}/chat/completions",
            json={"model": settings.local_llm_model, "max_tokens": 1,
                  "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer local"},
            timeout=180.0,
        )
    except Exception as e:
        print("local llm warmup skipped:", type(e).__name__, e)


def _warm_local_stt() -> None:
    """Load faster-whisper into memory, off the startup path. No GPU / faster-whisper missing
    is a supported state (falls back to Groq or the browser's own recognizer), so a failure
    here is logged, not raised."""
    try:
        stt._local_whisper()
    except Exception as e:
        print("local stt warmup skipped:", type(e).__name__, e)


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


# Usage logging for anyone not signed in. Signing in is optional — it buys saved history,
# not access — so the tutor still answers without it and a keyless install still works.
ANON_ID = "anon"


# ---------------------------------------------------------------- auth
class GoogleSignInIn(BaseModel):
    credential: str = Field(min_length=16, max_length=8192)  # the ID token from Google


@app.get("/auth/config")
def auth_config():
    """What the sign-in button needs. The client id is public by design; with it empty the
    web app hides sign-in and stays in anonymous/browser-local mode."""
    return {"google_client_id": settings.google_client_id, "enabled": auth.google_enabled()}


@app.post("/auth/google")
def auth_google(body: GoogleSignInIn):
    profile = auth.verify_google_id_token(body.credential)
    user = auth.upsert_user(profile["email"], profile["name"], profile["picture"])
    return {"token": auth.issue_user_token(user["email"]), "user": user}


@app.get("/me")
def me(user=auth.OptionalUser):
    profile = None
    if user:
        rows = query("SELECT email, name, picture FROM users WHERE email = ?", (user["email"],))
        profile = dict(rows[0]) if rows else {"email": user["email"], "name": "", "picture": ""}
    return {
        "stt": stt.budget()["available"],
        "tts": tts.availability(),
        # Whether to stream the HeyGen avatar instead of the local 2D face. The server owns
        # this: it holds the key, and a build-time flag in the web app was a second place to
        # configure it that silently disagreed with core/.env.
        "avatar": bool(settings.liveavatar_enabled and settings.liveavatar_api_key),
        "auth": {"google_client_id": settings.google_client_id, "enabled": auth.google_enabled()},
        "user": profile,
    }


# ---------------------------------------------------------------- conversations (signed in only)
class ConversationIn(BaseModel):
    title: str = Field(default="", max_length=200)


@app.get("/conversations")
def list_conversations(user=auth.CurrentUser):
    return {"conversations": conversations.list_for(user["email"])}


@app.post("/conversations")
def new_conversation(body: ConversationIn, user=auth.CurrentUser):
    return conversations.create(user["email"], body.title)


@app.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str, user=auth.CurrentUser):
    meta = conversations.owned_or_404(conversation_id, user["email"])
    return {**meta, "messages": conversations.messages(conversation_id, user["email"])}


@app.patch("/conversations/{conversation_id}")
def patch_conversation(conversation_id: str, body: ConversationIn, user=auth.CurrentUser):
    return conversations.rename(conversation_id, user["email"], body.title)


@app.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, user=auth.CurrentUser):
    conversations.delete(conversation_id, user["email"])
    return {"ok": True}


@app.delete("/conversations")
def delete_all_conversations(user=auth.CurrentUser):
    return {"deleted": conversations.delete_all(user["email"])}


# ---------------------------------------------------------------- corpus
@app.get("/videos")
def videos():
    return {"corpus_version": corpus.version, "videos": [v.__dict__ for v in corpus.videos.values()]}


@app.get("/videos/{video_id}/captions.vtt")
def captions(video_id: str):
    path = settings.corpus_dir / "transcripts" / f"{video_id}.vtt"
    if not path.exists():
        raise HTTPException(404, "no_captions")
    return FileResponse(path, media_type="text/vtt")


@app.get("/videos/{video_id}/transcript")
def transcript(video_id: str):
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
    # True when the client will speak this answer, which wants a much shorter register
    # than the same answer being read. Defaults to the reading length.
    spoken: bool = False
    # Signed-in only. Given one, the turn is appended to that conversation and its prior
    # turns — read from the database, not from `history` — are what the model sees.
    conversation_id: str | None = None


async def _persisted(gen, conversation_id: str, message: str, lang_hint: str | None):
    """Pass the SSE stream through untouched while recording the turn.

    The user message is written up front so a disconnect mid-answer still leaves the question
    in the history; the assistant row is written from the `done` event, which carries the
    final (possibly citation-corrected) answer rather than the raw token stream.
    """
    conversations.add_message(conversation_id, "user", message, lang_hint)
    lang, citations = lang_hint, []
    try:
        async for chunk in gen:
            for line in chunk.splitlines():
                if not line.startswith("data: "):
                    continue
                try:
                    ev = json.loads(line[6:])
                except ValueError:
                    continue
                if ev.get("type") == "meta":
                    lang = ev.get("lang", lang)
                elif ev.get("type") == "citations":
                    citations = ev.get("items", [])
                elif ev.get("type") == "done":
                    conversations.add_message(
                        conversation_id, "assistant", ev.get("answer", ""),
                        lang, citations, ev.get("source"),
                    )
            yield chunk
    except asyncio.CancelledError:
        # The student closed the tab mid-answer. Their question is already saved; there is no
        # assistant row to write, and re-raising keeps the disconnect semantics intact.
        raise


@app.post("/chat")
async def chat(body: ChatIn, user=auth.OptionalUser):
    history, conversation_id = body.history, None
    if user and body.conversation_id:
        conversations.owned_or_404(body.conversation_id, user["email"])
        conversation_id = body.conversation_id
        history = conversations.history_for_llm(conversation_id, user["email"])
    student_id = user["email"] if user else ANON_ID
    gen = run_chat(student_id, body.message, history, body.prev_lang, {}, body.spoken)
    if conversation_id:
        gen = _persisted(gen, conversation_id, body.message, body.prev_lang)
    return StreamingResponse(gen, media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- stt
@app.get("/stt/budget")
def stt_budget():
    return stt.budget()


@app.post("/stt")
async def stt_endpoint(file: UploadFile = File(...), lang_hint: str | None = Form(None)):
    audio = await file.read()
    if len(audio) > 4_000_000:
        raise HTTPException(413, "audio_too_large")
    vocab = VOCAB_FILE.read_text(encoding="utf-8") if VOCAB_FILE.exists() else ""
    result = await stt.transcribe(audio, file.filename or "audio.webm", file.content_type or "audio/webm", lang_hint, vocab)
    auth.bump_usage(ANON_ID, stt_calls=1)
    return result


# ---------------------------------------------------------------- tts (server fallback voice)
class TtsIn(BaseModel):
    text: str = Field(min_length=1, max_length=600)
    lang: str = Field(pattern="^(ar|en|fr)$")


@app.get("/tts/voices")
def tts_voices():
    return tts.availability()


@app.post("/tts")
async def tts_endpoint(body: TtsIn):
    if not tts.available(body.lang):
        raise HTTPException(404, "no_voice")
    wav, duration = await tts.synthesize(body.text, body.lang)
    return Response(wav, media_type="audio/wav", headers={"X-Duration": f"{duration:.3f}", "Cache-Control": "private, max-age=86400"})


# ---------------------------------------------------------------- live avatar (dev-only)
class LiveAvatarSessionIn(BaseModel):
    lang: str = Field(pattern="^(ar|en|fr)$")


@app.get("/avatar/live/available")
def live_avatar_available():
    return {"enabled": settings.liveavatar_enabled and bool(settings.liveavatar_api_key)}


@app.post("/avatar/live/session")
async def live_avatar_session(body: LiveAvatarSessionIn):
    try:
        return await liveavatar.start_session(body.lang)
    except liveavatar.LiveAvatarError as e:
        raise HTTPException(502, str(e)) from e


@app.post("/avatar/live/session/{session_id}/stop")
async def live_avatar_stop(session_id: str):
    await liveavatar.stop_session(session_id)
    return {"ok": True}


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
