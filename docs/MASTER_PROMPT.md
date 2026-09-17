# Master prompt

Paste everything between the rules into a fresh AI session, then write what you want changed
at the bottom. It carries the context that otherwise takes ten messages to re-establish, and
the rules that this project has already paid to learn.

Claude Code sessions in this repo read `CLAUDE.md` automatically and need none of this.

---

You are working on **My-Tutor**, a video-grounded tutor for ~400 college students. Students
ask questions in Egyptian Arabic, English or French; it answers **only** from the course
lecture videos and cites the timestamp behind every claim, with speech in and out and a
talking avatar.

Repo: https://github.com/V1shnuuu/My-Tutor

## Architecture

```
core/        FastAPI backend — retrieval, LLM router, STT, TTS, avatar sessions, SQLite
  app/chat.py               the answer path: gate → retrieve → generate → ground-check → cache
  app/corpus.py             dense (multilingual-e5-base, 768-dim) + BM25, fused with RRF
  app/router.py             picks an LLM lane by headroom, tier, language, priority
  app/stt.py                faster-whisper local, Groq Whisper if a key exists
  app/tts.py                Piper per language, + mishkal tashkeel for Arabic
  app/liveavatar.py         HeyGen LiveAvatar sessions over LiveKit
  app/auth.py               Google ID-token verify, ADMIN_EMAILS gate, per-student fair-share
  app/content.py            courses/weeks/lessons CRUD for the Admin Dashboard
  app/youtube.py            playlist reads via yt-dlp — no API key
  app/course_ingest.py      background transcribe→chunk→embed when an admin assigns a video
  app/syllabus_ai.py        local-LLM fallback parse for a pasted syllabus that isn't Week/N-list shaped
  app/captions_translate.py captions always in en/ar, whatever the video's real spoken language is
  providers.yaml            the LLM lanes; local-qwen (qwen3:8b) is priority 1
web/         React + Vite SPA, notebook/ruled-paper theme, streams answers over SSE
  src/components/AdminDashboard.tsx   separate portal at /admin — course/syllabus authoring
  src/lib/course.ts                   course-tree API client + courseToVideos() reshaping
pipeline/    add_videos.py (folder → videos.yaml) and ingest.py (transcribe → index)
corpus/      committed transcripts + index shards
eval/        per-language eval gate, runs in CI
deploy/      systemd unit + Caddyfile + bootstrap script for an Oracle Cloud Always Free VM
tests/       pytest, 150+ tests, run from the repo root
```

## Rules that are not negotiable

1. **One key, and it is HeyGen's.** `LIVEAVATAR_API_KEY` is the only credential, and only for
   the avatar. Brain (Qwen3-8B via Ollama), ears (faster-whisper) and voice (Piper) all run
   locally with no account. Never add a required key; never suggest a key as the fix for a
   slow local lane. No ElevenLabs — `eleven_multilingual_v2` is a HeyGen-side field name, not
   an account. Free and quota-free is a product requirement at 400 students. This extends to
   every optional account: `GOOGLE_CLIENT_ID` (saved history), `ADMIN_EMAILS` (who can reach
   `/admin/course/*`) — the app runs with none of them. "Connect playlist" needs no key either:
   `app/youtube.py` reads a pasted URL with yt-dlp, already a dependency for lecture audio.
2. **Arabic, English and French are equal.** Arabic is the course's primary language. A change
   that works in English and degrades Arabic is a regression. The avatar takes all three:
   sandbox drops the configured `voice_id` but still sends `persona["language"]`.
3. **Grounding is enforced in code, not in the prompt.** An answer with no `[C…]` citation is
   replaced with extractive lecture text and never cached. Off-topic questions are refused at
   the dense-score gate before any LLM call. Do not relax either into a system-prompt
   instruction a model can ignore.
4. **`reasoning_effort: none`** is required for Qwen3 or it spends the whole token budget
   inside `<think>` and streams back nothing.
5. **Eval baselines are raised by measurement, never to make CI pass.**
6. **Secrets:** `core/.env` is gitignored and stays that way. Never commit or echo a key.
7. **Every student, or no student — never a shared bucket.** `/chat` must call
   `auth.check_fair_share(student_id)` before doing any real work. `student_id` is the
   signed-in email, or an anonymous browser's own `X-Anon-Id` (minted once client-side) —
   never one constant shared by every signed-out visitor. This broke once (the check
   existed but nothing called it) and is exactly the kind of regression that hides quietly.
8. **Admin identity is checked fresh, every request, server-side.** `/admin/course/*` trusts
   nothing the client claims. A forged `role`/`is_admin` field in a request body changes
   nothing — authorization comes only from the caller's verified email against
   `ADMIN_EMAILS`. This is a different, newer mechanism than the older `x-admin-token`
   ops routes (`/admin/status`, `/admin/reload`) — don't conflate the two.

## How to verify a change

```bash
python -m pytest                     # from the repo root — must stay green
cd web && npx tsc --noEmit && npm run build
cd core && python -m app.cli doctor  # what works, what doesn't, and the fix for each
python eval/run_eval.py              # per-language gate; green ≠ good, read the notes
```

Run them before claiming anything works. If you cannot run something, say so plainly rather
than implying you did.

## Things that have already bitten, so check them first

- The frontend must not gate features on build-time `VITE_*` flags that duplicate server
  state — `.env.*` is gitignored, so they are absent on a fresh clone and disagree silently.
  The server tells the client what is on, via `/me`.
- Startup warms the encoder, the voices and the local LLM. Ollama unloads an idle model, so
  `OLLAMA_KEEP_ALIVE` matters more than any code change for perceived speed.
- On a CPU, `qwen3:8b` runs at a few tokens a second. Model size is the answer, not tuning.
- There is no enrollment-code login anymore — students are anonymous by default; signing in
  with Google is optional and buys saved history, not access. Don't reintroduce a code gate.
- Adding a SQLite column needs an `ALTER TABLE` migration in `db.py`; `CREATE TABLE IF NOT
  EXISTS` never reaches an existing database.
- A course-authored video's `Video.id` on the frontend must be the **corpus video id**
  (`yt-<youtubeId>`), not the lesson's own row id — every backend lookup (transcripts,
  captions, citation jumps) is keyed by the corpus id. Using the lesson id there silently
  breaks captions and citation jumps for any published course; it happened once already
  (see `courseToVideos` in `web/src/lib/course.ts`).
- A Google OAuth client must be type **Web application**, not Desktop — only Web application
  clients have "Authorized JavaScript origins," and Google's own error for the wrong type
  ("no registered origin", 401 invalid_client) doesn't say that outright.
- An OAuth consent screen left in "Testing" mode only lets its listed test users sign in at
  all — everyone else hits Google's own "Access blocked" page before ever reaching this app.

## How to answer me

Read the relevant code before proposing anything — do not guess at what a file does. Make the
change, run the checks above, and tell me what you actually ran and what it printed. If
something cannot be verified in your environment, name it as unverified instead of implying
it passed. If my request would break one of the rules above, say so in a sentence and propose
the nearest thing that does not.

---

## THE CHANGE I WANT

Take this project from "well-built but not proven" to actually serving real students 24/7.
Concretely, that means every item below is true, not just plausible:

**1. It is actually deployed and reachable 24/7.**
   - The backend runs on a real always-on host (an Oracle Cloud Always Free Ampere A1 VM is
     the intended target — see `deploy/setup.sh`), behind HTTPS on a real domain, not
     `localhost`. `systemctl status tutor-backend` shows it running; killing the process and
     waiting a few seconds shows it come back on its own (Restart=on-failure).
   - The frontend is deployed (Vercel or Cloudflare Pages) and its `VITE_API_URL` points at
     that real backend domain, not a dev server.
   - `ALLOWED_ORIGINS` on the server and the deployed frontend's actual origin match exactly.

**2. Any Gmail account can actually sign in.**
   - The Google OAuth consent screen is out of "Testing" mode (published, or otherwise no
     longer limited to a hand-picked test-user list) — verify this by having someone whose
     email was never added anywhere sign in successfully.
   - The OAuth client's Authorized JavaScript origins list the deployed frontend's real
     origin, not just `localhost:5173`.
   - `ADMIN_EMAILS` still contains exactly one address. Prove a second real Google account
     gets a 403 from every `/admin/course/*` route while still being able to use the tutor
     itself with no restriction.

**3. The LLM/STT lane works without your GPU in the loop.**
   - On the deployed (GPU-less) server, `LOCAL_LLM_ENABLED=false` and a free-tier cloud key
     (Groq to start) is set and actually answering real chat requests — not just present in
     `.env`, verified by asking a real question against the deployed URL and confirming the
     answer came from that provider (check `/admin/status`'s provider snapshot or the
     `provider` field on the `done` SSE event).
   - Confirm the retrieval/embedding lane (`sentence-transformers`) actually starts on that
     ARM box — this is the one dependency most likely to have a rough edge on aarch64, so
     don't assume it from the x86 dev machine.
   - If Groq's free quota runs out mid-day, the router falls through cleanly (a second free
     key, or the extractive floor) rather than the chat silently going dark.

**4. It survives real concurrent load, not just a quick local burst.**
   - Load-test the *deployed* URL (not localhost) at a scale closer to the real ~400-student
     ceiling: model a realistic burst (e.g. 50-100 concurrent distinct anonymous identities
     asking real questions within a short window) and confirm no 5xx, no crashed process, and
     that the daily/minute fair-share caps still hold per-identity under that concurrency.
   - Deliberately kill Ollama/disconnect the LLM mid-request during a load test and confirm
     the app degrades to the extractive floor rather than hanging or 500ing.

**5. Clean up what this session's audit found but didn't fully resolve.**
   - ~~`docs/ARCHITECTURE.md` is still describing the old design~~ **Done**: rewritten to
     describe what's actually built (HeyGen avatar not 3D three.js, no edge layer, no
     Docker/Tunnel, Google Sign-In + anonymous not enrollment/SSO, systemd+Caddy not
     Litestream/R2) and to say plainly where the real gaps still are.
   - ~~The `/admin/codes` enrollment-code endpoint and its CLI (`app.cli codes`) are
     vestigial — no frontend path uses them anymore.~~ **Done**: removed, along with
     `auth.create_students`/`redeem`/`new_code`, the `students` table, and its
     now-pointless migration — nothing redeemed a code through any live route, so it was
     dead weight pretending to be a feature.
   - ~~`web`'s production JS bundle is a single ~900KB chunk.~~ **Done**: `AdminDashboard`
     is now a dynamic `import()` behind `React.lazy` on the `/admin` route — verified in
     the build output as its own ~9KB chunk, separate from the ~890KB main bundle students
     actually download. The remaining ~890KB is a separate, larger question (likely
     LiveKit for the avatar) not addressed here.
   - `check_fair_share`'s daily-cap check still has a real (documented, accepted-for-now) race
     under true concurrency — decide, with a measurement from step 4's load test, whether it's
     actually worth closing with an atomic reserve-and-refund-on-refusal, or whether the
     current "cheap, approximate" version holds up fine at real scale.

**How I'll know this is actually done, not just claimed:** for each of the five sections
above, show me the real command you ran or the real request you made against the *deployed*
URL, and its real output — not "this should work now." If something can't be verified from
where you're running (e.g., you can't create the Oracle account or flip the OAuth publishing
status yourself), say exactly that and hand back the precise action and the exact information
you need from me to confirm it once I've done it.
