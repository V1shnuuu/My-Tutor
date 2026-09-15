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
  app/chat.py       the answer path: gate → retrieve → generate → ground-check → cache
  app/corpus.py     dense (multilingual-e5-base, 768-dim) + BM25, fused with RRF
  app/router.py     picks an LLM lane by headroom, tier, language, priority
  app/stt.py        faster-whisper local, Groq Whisper if a key exists
  app/tts.py        Piper per language, + mishkal tashkeel for Arabic
  app/liveavatar.py HeyGen LiveAvatar sessions over LiveKit
  providers.yaml    the LLM lanes; local-qwen (qwen3:8b) is priority 1
web/         React + Vite SPA, notebook/ruled-paper theme, streams answers over SSE
pipeline/    add_videos.py (folder → videos.yaml) and ingest.py (transcribe → index)
corpus/      committed transcripts + index shards
eval/        per-language eval gate, runs in CI
tests/       pytest, 56 tests, run from the repo root
```

## Rules that are not negotiable

1. **One key, and it is HeyGen's.** `LIVEAVATAR_API_KEY` is the only credential, and only for
   the avatar. Brain (Qwen3-8B via Ollama), ears (faster-whisper) and voice (Piper) all run
   locally with no account. Never add a required key; never suggest a key as the fix for a
   slow local lane. No ElevenLabs — `eleven_multilingual_v2` is a HeyGen-side field name, not
   an account. Free and quota-free is a product requirement at 400 students.
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
- Enrollment codes never expire, but bind to the first device. `codes 1 --reusable` opts out.
- Adding a SQLite column needs an `ALTER TABLE` migration in `db.py`; `CREATE TABLE IF NOT
  EXISTS` never reaches an existing database.

## How to answer me

Read the relevant code before proposing anything — do not guess at what a file does. Make the
change, run the checks above, and tell me what you actually ran and what it printed. If
something cannot be verified in your environment, name it as unverified instead of implying
it passed. If my request would break one of the rules above, say so in a sentence and propose
the nearest thing that does not.

---

## THE CHANGE I WANT

<!-- Write it here. Be concrete about the behaviour you want, not the code you think it needs. -->
