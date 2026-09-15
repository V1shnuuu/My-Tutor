# Adaptive Tutor

A video-grounded, language-adaptive tutor for ~400 college students. Students ask in
**Egyptian Arabic, English, or French** (typed or spoken); the tutor answers **only from the
course's lecture videos**, links every claim to a clickable timestamp, speaks the answer, and
lip-syncs a 2D avatar. Built to the four criteria in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md):
**fast → free → speaking avatar → no quota**.

```
web/        Vite + React SPA (Cloudflare Pages)       ← notebook UI, player, chat, voice, avatar
core/       FastAPI (Docker → Oracle Always-Free VM)  ← retrieval, LLM router, cache, auth, STT proxy
pipeline/   video → transcript → chunks → embeddings  ← GitHub Actions CPU / Kaggle GPU
corpus/     git-versioned index shards + transcripts  ← one shard per video
eval/       Q&A eval set per language + gate script   ← runs in CI, blocks bad deploys
infra/      docker-compose (core + Cloudflare Tunnel + Litestream backups to R2)
```

## Run locally (5 minutes)

```bash
# 1. core
python -m venv core/.venv && core/.venv/Scripts/pip install --extra-index-url https://download.pytorch.org/whl/cpu "torch>=2.4,<2.6" -r pipeline/requirements.txt
cp core/.env.example core/.env                 # add LLM keys (any subset); none = extractive answers only
python pipeline/ingest.py                      # indexes pipeline/videos.yaml (sample video included)
cd core && .venv/Scripts/python -m app.cli codes 3      # prints enrollment codes
.venv/Scripts/python -m uvicorn app.main:app --port 8000

# 2. web
cd web && npm i && npm run dev                 # http://localhost:5173 → paste a code
```

## Changing things with an AI

`CLAUDE.md` is read automatically by Claude Code sessions in this repo. For any other AI,
paste `docs/MASTER_PROMPT.md` and write the change you want at the bottom of it.

## Enrollment codes

```bash
cd core && python -m app.cli codes 400 --labels roster.csv   # one per student
cd core && python -m app.cli codes 1 --reusable              # one for you
```

Codes never expire. An ordinary one **binds to the first device that redeems it** — a second
browser gets `code_already_used`, which is what stops one code being passed around a cohort.

`--reusable` turns that off for a single code: it works on a phone, a laptop and a lecture-hall
machine, indefinitely. Use it for yourself and for demos. Don't hand it to students — everyone
redeeming it shares one student identity, so they share one daily budget and one history.

## Add the course's videos

Point `add_videos.py` at a folder of clips and it registers every one of them, copies them
where the player can reach them, and writes `pipeline/videos.yaml`. Then `ingest.py`
transcribes and indexes them, and they appear in the lesson column down the side of the app.

```bash
python pipeline/add_videos.py --dir "C:/Users/priya/Downloads/icon/uploads/uploads" --lang ar --week 1 --dry-run
python pipeline/add_videos.py --dir "C:/Users/priya/Downloads/icon/uploads/uploads" --lang ar --week 1
python pipeline/ingest.py
```

`--dry-run` first: it prints the id and title it would give each clip without touching
anything. `--lang` primes Whisper and `--week` groups the clips in the lesson column — run
it once per week's folder to get the grouping. Forward slashes work fine on Windows.

### The registry by hand

Edit `pipeline/videos.yaml` — one entry per lecture (`source: youtube` + `youtube_id`, or
`source: file` + `url`), choose `transcript: whisper | auto | file`, set `lang` — then run
`python pipeline/ingest.py` (or push: the `ingest` workflow does it and hot-reloads the core).
Put course jargon in `corpus/vocabulary.txt`; it primes Whisper and live STT.

*Bulk first ingest with GPU (free):* open a Kaggle notebook (GPU T4, 30 h/week), clone the
repo, `pip install -r pipeline/requirements.txt`, `WHISPER_MODEL=large-v3 WHISPER_DEVICE=cuda
python pipeline/ingest.py`, then commit `corpus/`. ~6 min per lecture hour.

*Transcript corrections:* edit `corpus/transcripts/<id>.vtt` text (keep timestamps), set that
video's `transcript: file` + `transcript_file: corpus/transcripts/<id>.vtt`, push.

## Deploy ($0/month)

| Piece | Where | How |
|---|---|---|
| Web | Cloudflare Pages | Connect the repo → root `web`, build `npm ci && npm run build`, output `dist`, env `VITE_API_URL=https://api.<domain>` |
| Core | Oracle Cloud Always Free (Ampere A1, 4 OCPU/24 GB) | `git clone`, `cp core/.env.example core/.env` (fill), `cp infra/.env.example infra/.env` (Tunnel token + R2 keys), `docker compose -f infra/docker-compose.yml up -d --build` |
| Tunnel | Cloudflare Zero Trust → Tunnels | Public hostname `api.<domain>` → `http://core:8000`; no inbound ports on the VM |
| Standby | Hugging Face Space (Docker, free CPU) | `infra/hf-space/Dockerfile` (same image, same env); point `VITE_API_URL` at it if the VM is down |
| Backups | Cloudflare R2 (10 GB free) | Litestream replicates `tutor.sqlite` every 10 s (`infra/litestream.yml`) |
| Monitoring | Grafana Cloud Free + UptimeRobot | `infra/monitoring.md` — scrape `GET /metrics`, ping `GET /healthz`, alert rules |

Set `ALLOWED_ORIGINS` in `core/.env` to the Pages URL. Generate codes once the core is up:
`curl -X POST "$CORE/admin/codes?n=400" -H "x-admin-token: $ADMIN_TOKEN" > codes.csv`
(or upload a one-column roster CSV as `labels`). Hand each student one code; it binds to
their first device.

## LLM providers

All free tiers, all through their OpenAI-compatible endpoints, limits in `core/providers.yaml`.
Set any subset of `GEMINI_API_KEY`, `CEREBRAS_API_KEY`, `GROQ_API_KEY`, `MISTRAL_API_KEY`,
`OPENROUTER_API_KEY`, `CLOUDFLARE_API_TOKEN`+`CLOUDFLARE_ACCOUNT_ID`. `GROQ_API_KEY` also
enables Whisper voice input (2 000 turns/day; the browser's speech recognition takes over after).
With no keys the tutor still works — it returns the exact lecture passages with timestamps.

## Quality gate

`python eval/run_eval.py` — retrieval recall@5, citation@1, gate recall, language detection,
guard false-blocks per language; CI fails the build below thresholds. Extend
`eval/qa_{ar,en,fr}.jsonl` with real course questions (gold = video id + timestamp window).

## Admin

`GET /admin/status` (header `x-admin-token`) — provider budgets, queue depth, cache size, 24 h
event counts and latencies. `POST /admin/reload` — hot-swap the corpus after ingest.
