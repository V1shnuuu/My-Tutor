"""Adaptive Tutor core API."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from . import auth, captions_translate, content, course_ingest, conversations, liveavatar, quiz, stt, syllabus_ai, tts, youtube
from .cache import semantic_cache
from .chat import run_chat
from .config import settings
from .corpus import corpus
from .db import connect, query, tx
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
    # Ingestion runs in daemon threads that die with the process, so any video still marked
    # "ingesting" at boot was interrupted, not in progress — and retry_ingest would refuse it
    # forever as "already ingesting". Mark it failed with the real reason so it's retryable.
    with tx() as c:
        c.execute(
            "UPDATE youtube_videos SET ingest_status = 'failed', ingest_error = ? WHERE ingest_status = 'ingesting'",
            ("interrupted by a server restart — retry to start again",),
        )
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
async def captions(video_id: str, lang: str | None = None):
    if lang:
        try:
            cues = await captions_translate.get_cues(video_id, lang)
        except captions_translate.TranslateError as e:
            status = {"no_transcript": 404, "captions_translate_bad_lang": 422, "captions_translate_busy": 429}.get(e.code, 502)
            raise HTTPException(status, e.code) from e
        return Response(captions_translate.cues_to_vtt(cues), media_type="text/vtt")
    path = settings.corpus_dir / "transcripts" / f"{video_id}.vtt"
    if not path.exists():
        raise HTTPException(404, "no_captions")
    return FileResponse(path, media_type="text/vtt")


@app.get("/videos/{video_id}/transcript")
async def transcript(video_id: str, lang: str | None = None):
    """Cue list for the in-app caption overlay (works with the YouTube IFrame too).

    `lang` ("en" or "ar") returns captions translated to that language regardless of what
    the video is actually spoken in — an admin-connected playlist can be in any language —
    via captions_translate.py. Omitted, this is the original untranslated transcript,
    unchanged from before that existed."""
    if lang:
        try:
            return await captions_translate.get_cues(video_id, lang)
        except captions_translate.TranslateError as e:
            status = {"no_transcript": 404, "captions_translate_bad_lang": 422, "captions_translate_busy": 429}.get(e.code, 502)
            raise HTTPException(status, e.code) from e
    path = settings.corpus_dir / "transcripts" / f"{video_id}.cues.json"
    if not path.exists():
        raise HTTPException(404, "no_transcript")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/videos/{video_id}/quiz")
async def video_quiz(video_id: str):
    """Practice questions generated from this video's own transcript chunks — see
    core/app/quiz.py for why every question is grounded in a specific excerpt rather than
    the model's general sense of the topic. Cached after first generation, so no auth is
    needed (matches /videos and /videos/{id}/transcript's own no-auth-required shape)."""
    try:
        return {"questions": await quiz.get_quiz(video_id)}
    except quiz.QuizError as e:
        status = {"no_transcript": 404, "quiz_unavailable": 503, "quiz_busy": 429}.get(e.code, 502)
        raise HTTPException(status, e.code) from e


@app.get("/search")
async def search(q: str = "", k: int = 10):
    """Cross-lecture search: every video's transcript, not just the currently-playing one —
    "find every mention of X across the whole course," for exam review. Pure retrieval, no
    LLM call — free, instant, and exact (a snippet is either there or it isn't), unlike chat
    which is one grounded answer to one question."""
    from .chat import citations_for
    from .embed import EmbeddingUnavailable, embed_queries

    q = q.strip()
    if not q:
        return {"results": []}
    k = max(1, min(k, 30))
    try:
        qvecs = await asyncio.to_thread(embed_queries, [q])
    except EmbeddingUnavailable as e:
        raise HTTPException(503, "encoder_unavailable") from e
    # Same scope as chat: the published course's lessons, or everything when none is published.
    hits = await asyncio.to_thread(corpus.search, qvecs, [q], k, content.published_video_ids())
    return {"results": citations_for(hits)}


@app.get("/course")
def get_published_course():
    """The admin-authored course (weeks → lessons → mapped video), or null if none is
    published yet — same no-auth-required shape as /videos, which this is meant to sit
    alongside rather than replace: a fresh install with no course published falls back to
    the static corpus/ videos the way it always has."""
    return content.student_course_tree()


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
async def chat(body: ChatIn, user=auth.OptionalUser, x_anon_id: str | None = Header(None, alias="X-Anon-Id")):
    history, conversation_id = body.history, None
    if user and body.conversation_id:
        conversations.owned_or_404(body.conversation_id, user["email"])
        conversation_id = body.conversation_id
        history = conversations.history_for_llm(conversation_id, user["email"])
    # A signed-in student is identified by their email; an anonymous one by a per-browser id
    # the client mints once and reuses (see web/src/lib/auth.ts's getAnonId) — falling back to
    # a single shared bucket only for callers that never send one (older cached bundles, curl).
    # Without this, every anonymous visitor would share one daily/minute budget with everyone
    # else who hasn't signed in, which is not a fair-use cap, it's an outage waiting to happen.
    student_id = user["email"] if user else (x_anon_id or ANON_ID)
    auth.check_fair_share(student_id)
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
    }


# ---------------------------------------------------------------- admin: course content
# Every route below requires auth.AdminUser — a signed-in Google account whose email is in
# ADMIN_EMAILS (core/app/auth.py), checked fresh on every request. Distinct from the
# x-admin-token routes above, which predate Google Sign-In and cover ops/CLI actions.
class CourseIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)


class TitleIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class ReorderIn(BaseModel):
    ids: list[str]


class VideoAssignIn(BaseModel):
    video_id: str | None = None


class PlaylistIn(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


class SyllabusWeekIn(BaseModel):
    title: str
    lessons: list[str]


class SyllabusBulkIn(BaseModel):
    weeks: list[SyllabusWeekIn]


class SyllabusTextIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


@app.get("/admin/course/is_admin")
def am_i_course_admin(user=auth.OptionalUser):
    """So the web app can decide whether to offer the Admin Dashboard at all, without
    guessing — the actual gate on every route below is still server-side per-request."""
    return {"is_admin": bool(user) and auth.is_course_admin(user["email"])}


@app.get("/admin/course/analytics")
def admin_course_analytics(admin=auth.AdminUser):
    """A course-admin-reachable view of the same numbers /admin/status already computes for
    the older x-admin-token ops routes — this project's only admin didn't have a login to
    that mechanism's usual caller (a curl command with a header), so the data existed but
    wasn't actually visible to them. Read-only, derived entirely from the existing `events`
    and `cache` tables — no new tracking, no new schema."""
    day_ago = int(time.time()) - 86400
    counts = {r["kind"]: r["n"] for r in query("SELECT kind, COUNT(*) AS n FROM events WHERE ts > ? GROUP BY kind", (day_ago,))}
    lat = query("SELECT kind, AVG(ms) AS avg_ms FROM events WHERE ts > ? AND ms IS NOT NULL GROUP BY kind", (day_ago,))
    # Every question resolves exactly one of these four ways — the denominator for the rates below.
    total = sum(counts.get(k, 0) for k in ("chat", "cache_hit", "floor", "gate_refuse"))
    return {
        "corpus": {"videos": len(corpus.videos), "chunks": corpus.size},
        "questions_24h": total,
        "cache_hit_rate_24h": round(counts.get("cache_hit", 0) / total, 3) if total else None,
        "refusal_rate_24h": round(counts.get("gate_refuse", 0) / total, 3) if total else None,
        "floor_rate_24h": round(counts.get("floor", 0) / total, 3) if total else None,
        "events_24h": counts,
        "avg_ms_24h": {r["kind"]: int(r["avg_ms"]) for r in lat},
        "cache_entries": query("SELECT COUNT(*) AS n FROM cache")[0]["n"],
        "queue_depth": router.queue_depth,
        "providers": [{"id": p["id"], "tier": p["tier"], "rpd_used": p["rpd_used"], "rpd": p["rpd"], "errors": p["errors"]} for p in router.snapshot()["providers"]],
        "stt": stt.budget(),
    }


@app.get("/admin/course/courses")
def admin_list_courses(admin=auth.AdminUser):
    return {"courses": content.list_courses()}


@app.post("/admin/course/courses")
def admin_create_course(body: CourseIn, admin=auth.AdminUser):
    return content.create_course(admin["email"], body.title, body.description)


@app.get("/admin/course/courses/{course_id}")
def admin_get_course(course_id: str, admin=auth.AdminUser):
    return content.admin_course_tree(course_id)


@app.patch("/admin/course/courses/{course_id}")
def admin_update_course(course_id: str, body: CourseIn, admin=auth.AdminUser):
    return content.update_course(course_id, body.title, body.description)


@app.delete("/admin/course/courses/{course_id}")
def admin_delete_course(course_id: str, admin=auth.AdminUser):
    content.delete_course(course_id)
    return {"ok": True}


@app.post("/admin/course/courses/{course_id}/publish")
def admin_publish_course(course_id: str, admin=auth.AdminUser):
    return content.publish_course(course_id, True)


@app.post("/admin/course/courses/{course_id}/unpublish")
def admin_unpublish_course(course_id: str, admin=auth.AdminUser):
    return content.publish_course(course_id, False)


@app.post("/admin/course/courses/{course_id}/weeks")
def admin_add_week(course_id: str, body: TitleIn, admin=auth.AdminUser):
    return content.add_week(course_id, body.title)


@app.post("/admin/course/courses/{course_id}/weeks/reorder")
def admin_reorder_weeks(course_id: str, body: ReorderIn, admin=auth.AdminUser):
    content.reorder_weeks(course_id, body.ids)
    return {"ok": True}


@app.patch("/admin/course/weeks/{week_id}")
def admin_rename_week(week_id: str, body: TitleIn, admin=auth.AdminUser):
    content.rename_week(week_id, body.title)
    return {"ok": True}


@app.delete("/admin/course/weeks/{week_id}")
def admin_delete_week(week_id: str, admin=auth.AdminUser):
    content.delete_week(week_id)
    return {"ok": True}


@app.post("/admin/course/weeks/{week_id}/lessons")
def admin_add_lesson(week_id: str, body: TitleIn, admin=auth.AdminUser):
    return content.add_lesson(week_id, body.title)


@app.post("/admin/course/weeks/{week_id}/lessons/reorder")
def admin_reorder_lessons(week_id: str, body: ReorderIn, admin=auth.AdminUser):
    content.reorder_lessons(week_id, body.ids)
    return {"ok": True}


@app.patch("/admin/course/lessons/{lesson_id}")
def admin_rename_lesson(lesson_id: str, body: TitleIn, admin=auth.AdminUser):
    content.rename_lesson(lesson_id, body.title)
    return {"ok": True}


@app.delete("/admin/course/lessons/{lesson_id}")
def admin_delete_lesson(lesson_id: str, admin=auth.AdminUser):
    content.delete_lesson(lesson_id)
    return {"ok": True}


@app.post("/admin/course/lessons/{lesson_id}/video")
def admin_assign_video(lesson_id: str, body: VideoAssignIn, admin=auth.AdminUser):
    content.assign_video(lesson_id, body.video_id)
    # A freshly-assigned video that has never been ingested anywhere starts transcription
    # right away rather than waiting for a separate "ingest now" click — assigning it to a
    # lesson is the moment it became something a student can ask the Tutor about.
    if body.video_id:
        rows = query("SELECT youtube_video_id, title, ingest_status FROM youtube_videos WHERE id = ?", (body.video_id,))
        if rows and rows[0]["ingest_status"] in ("pending", "failed"):
            course_ingest.ingest_video_in_background(body.video_id, rows[0]["youtube_video_id"], rows[0]["title"])
    return {"ok": True}


@app.post("/admin/course/videos/{video_id}/retry_ingest")
def admin_retry_ingest(video_id: str, admin=auth.AdminUser):
    """A failed ingestion used to be a dead end — nothing ever retried it, so a transient
    cause (a missing ffmpeg, a network blip mid-download) left the lesson permanently
    unanswerable. This is the explicit second chance; the admin UI shows the stored error
    next to the button so the retry is informed, not blind."""
    rows = query("SELECT youtube_video_id, title, ingest_status FROM youtube_videos WHERE id = ?", (video_id,))
    if not rows:
        raise HTTPException(404, "video_not_found")
    if rows[0]["ingest_status"] == "ingesting":
        raise HTTPException(409, "already_ingesting")
    content.set_video_ingest_status(video_id, "pending")
    course_ingest.ingest_video_in_background(video_id, rows[0]["youtube_video_id"], rows[0]["title"])
    return {"ok": True}


@app.post("/admin/course/syllabus/ai_parse")
async def admin_ai_parse_syllabus(body: SyllabusTextIn, admin=auth.AdminUser):
    """Fallback for pasted text the plain regex parser (syllabus.ts) can't make sense of —
    not tied to a course, since it's a pure text→structure transform the admin reviews
    before anything is saved, same as the regex path."""
    try:
        weeks = await syllabus_ai.ai_parse_syllabus(body.text)
    except syllabus_ai.SyllabusAIError as e:
        status = {"syllabus_ai_unavailable": 503, "syllabus_ai_busy": 429}.get(e.code, 502)
        raise HTTPException(status, e.code) from e
    return {"weeks": weeks}


@app.post("/admin/course/courses/{course_id}/syllabus/bulk")
def admin_bulk_syllabus(course_id: str, body: SyllabusBulkIn, admin=auth.AdminUser):
    """Creates every week/lesson from an already-client-parsed syllabus paste in one call,
    rather than one round trip per week/lesson. The admin reviews/edits the parse client-side
    before this ever fires — see web/src/lib/syllabus.ts."""
    content.get_course(course_id)
    created_weeks = []
    for w in body.weeks:
        week = content.add_week(course_id, w.title)
        lessons = [content.add_lesson(week["id"], t) for t in w.lessons]
        created_weeks.append({**week, "lessons": lessons})
    return {"weeks": created_weeks}


def _youtube_status(code: str) -> int:
    """Every youtube.YouTubeError code, mapped to the specific status it actually is — not a
    blanket catch-all, matching how core/app/stt.py distinguishes stt_budget (429) from
    stt_local_failed (503) rather than raising one generic "something went wrong" status."""
    return {
        "invalid_playlist_url": 422,       # the admin's input, not YouTube's fault
        "playlist_not_found": 404,
    }.get(code, 502)                       # anything else: yt-dlp genuinely couldn't read it


@app.post("/admin/course/courses/{course_id}/playlist")
async def admin_connect_playlist(course_id: str, body: PlaylistIn, admin=auth.AdminUser):
    content.get_course(course_id)
    try:
        playlist_id = youtube.extract_playlist_id(body.url)
        meta = await youtube.fetch_playlist(playlist_id)
        videos = await youtube.fetch_playlist_videos(playlist_id)
    except youtube.YouTubeError as e:
        raise HTTPException(_youtube_status(e.code), e.code) from e
    if not videos:
        raise HTTPException(422, "playlist_has_no_videos")
    playlist = content.set_playlist(course_id, meta)
    sync_result = content.sync_videos(playlist["id"], videos)
    return {"playlist": playlist, "sync": sync_result}


@app.post("/admin/course/courses/{course_id}/playlist/sync")
async def admin_sync_playlist(course_id: str, admin=auth.AdminUser):
    playlist = content.get_playlist(course_id)
    if not playlist:
        raise HTTPException(404, "no_playlist_connected")
    try:
        videos = await youtube.fetch_playlist_videos(playlist["youtube_playlist_id"])
    except youtube.YouTubeError as e:
        raise HTTPException(_youtube_status(e.code), e.code) from e
    sync_result = content.sync_videos(playlist["id"], videos)
    return {"sync": sync_result}


@app.get("/admin/course/courses/{course_id}/playlist/videos")
def admin_list_playlist_videos(course_id: str, admin=auth.AdminUser):
    """Every video in the connected playlist, assigned or not — what the per-lesson video
    picker offers, since re-assigning a video already used by another lesson must work too,
    not just picking from the leftover unassigned ones."""
    playlist = content.get_playlist(course_id)
    if not playlist:
        return {"videos": []}
    return {"videos": content.list_videos(playlist["id"])}
