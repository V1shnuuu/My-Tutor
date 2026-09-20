# My-Tutor — project rules

A video-grounded tutor for ~400 college students, answering in Egyptian Arabic, English
and French, citing the lecture timestamp behind every claim.

These rules exist because each one has already been broken once, at cost. Read them
before changing anything in `core/app/`, `providers.yaml`, or the voice path.

## 1. One key, and it is HeyGen's

`LIVEAVATAR_API_KEY` is the only credential this project needs, and only for the
streaming avatar. Everything else runs locally with no account:

| | what runs | where it is configured |
|---|---|---|
| brain | Qwen3-8B via Ollama | `LOCAL_LLM_*`, `providers.yaml: local-qwen` |
| ears | faster-whisper, ar/en/fr | `LOCAL_STT_*`, `core/app/stt.py` |
| voice | Piper, ar/en/fr | `core/app/tts.py`, `python -m app.cli voices` |
| face | HeyGen sandbox | `LIVEAVATAR_*` |

- **Never add a required key.** Cloud LLM keys in `.env` are optional overflow, reached
  only when the local server is down or saturated. An install with every one of them
  empty is supported and fully working — `tests/test_keyless_install.py` proves it, and
  that file is the thing to run when touching any of this.
- **No ElevenLabs.** No code reads `ELEVENLABS_API_KEY`. `eleven_multilingual_v2` in
  `LIVEAVATAR_TTS_MODEL` is a model name *HeyGen* accepts in their own field — it reads
  like an account requirement and is not one. Don't add an ElevenLabs client.
- **Never recommend a key as a fix** in `doctor`, docs, or errors. If the local lane is
  slow the answer is a bigger model on a GPU (`LOCAL_STT_MODEL=large-v3-turbo`,
  `LOCAL_STT_DEVICE=cuda`), not somebody's API.
- Free and quota-free is a product requirement at 400 students, not a preference.
- **Optional accounts follow the same rule.** `GOOGLE_CLIENT_ID` (saved chat history) and
  `ADMIN_EMAILS` (who can reach `/admin/course/*`) are each independently optional — the
  app runs with neither, students stay anonymous, and the admin dashboard simply has
  nothing to authorize. Admin identity is checked fresh against `ADMIN_EMAILS` on every
  request (`auth.is_course_admin`), never trusted from a request body or cached in a
  token. "Connect playlist" needs no key at all — `core/app/youtube.py` reads a pasted
  playlist URL with yt-dlp (already a dependency for lecture audio download), the same
  thing anyone with the link can already see on youtube.com.

## 2. All three languages, everywhere

Arabic is the primary language of this course, not an afterthought to English.

- ar/en/fr are equal in every lane. A change that works for English and degrades Arabic
  is a regression, not a partial win.
- `local-qwen` declares `langs: [ar, en, fr]`. STT pins `language=` for all three. Piper
  names a voice for all three. The avatar takes all three.
- **The avatar is not English-only.** `is_sandbox` drops the configured `voice_id`, but
  `_start_once` still sends `persona["language"] = lang`, so HeyGen speaks the answer's
  language in its own voice. A previous change gated Arabic and French away from the
  avatar on a misreading of the empty persona dict. Don't repeat it.
- Arabic needs two things the others don't: an Egyptian `initial_prompt` primer, or
  Whisper "corrects" إزاي to كيف; and mishkal tashkeel before Piper, or the MSA-trained
  voice mis-vowels undiacritised text.

## 3. Grounding is enforced in code, not in the prompt

- An answer with no `[C…]` marker is **replaced** with extractive lecture text and is
  **never cached**. This is the guarantee the product is sold on — do not relax it into
  a system-prompt instruction, which a model can ignore.
- Off-topic questions are refused at the dense-score gate, before any LLM call.
- The dense score is *also* the gate signal, so there is no honest degraded answer when
  the encoder is unavailable: fail fast and say so, never spin.

## 4. Qwen3 specifics

`reasoning_effort: none` is required in `extra_body`. Without it Qwen3 spends the whole
`max_tokens` budget inside `<think>` and streams back empty content — a silent failure
that looks like a broken model.

## 5. Evals

`eval/run_eval.py` gates on per-language baselines, not one global number, so a green
run is not the same as a good one. Each run prints `(below target: …)` where a baseline
is still under the real target. **Raise a baseline only with a measurement that earns
it** — never to make CI pass.

## 6. Secrets

`core/.env` is gitignored and stays that way. Never commit, echo, or paste a key. If one
reaches a chat or a log, say so plainly and tell the user to rotate it.

## 7. Every student, or no student — never a shared bucket

`auth.check_fair_share` existed for a long time without ever being called from `/chat` —
the daily/minute caps in `config.py` were configured, tested-for in the frontend
(`daily_cap`/`minute_cap` in `i18n.ts`), and completely unenforced. Anyone could hit the
LLM as fast as the network allowed. If you touch `/chat`'s auth/budget wiring in
`main.py`, keep the call to `auth.check_fair_share(student_id)` before any real work
starts, and keep `student_id` per-identity: a signed-in student's email, or an anonymous
browser's own id (`X-Anon-Id`, minted once client-side — see `lib/auth.ts`'s `getAnonId`).
Falling back to one shared id for every signed-out visitor means the first handful of
anonymous questions each day exhaust the cap for everyone else who hasn't signed in.

## 8. A provider's own outage is not the same failure as a rate limit

`router.py`'s cooldown after an error is sized to the failure, not one blanket duration.
A real 429 keeps a long (60s) cooldown — retrying sooner just burns another call against
an already-exhausted window. A 5xx or dropped connection gets a short one (5s): this is
the provider's own infrastructure having a bad moment (a bare 502 from Groq's Cloudflare
front door has actually happened, mid-demo, not hypothetically), and it recovers in
seconds on its own. Collapsing these into one long cooldown is what turned a single
transient blip into every student seeing "Busy" for the full window — worse the fewer
providers are actually configured (a deployment running on Groq alone has no fallback
to fall through to while it's in cooldown). If you touch this logic, keep the three
tiers distinct: 429 (exhausted, wait it out) vs 5xx/network (transient, retry soon) vs
other 4xx (a config problem — bad model name, bad request — that needs a human, not a
timer, but shouldn't lock the provider out as long as a real rate limit either).

## 9. A cloud IP is not a browser, and YouTube eventually notices

Both `core/app/youtube.py` (admin's "Connect" flow) and `pipeline/ingest.py`
(`yt_download_audio`/`yt_auto_captions`) call yt-dlp against YouTube directly from the
server's own IP. Cloud/datacenter IP ranges — Oracle's included — get flagged sooner or
later: every extraction starts failing with `"Sign in to confirm you're not a bot"`,
regardless of playlist vs. single video, regardless of which yt-dlp "player client" you
try (android/ios/tv/mweb — all tested, all blocked the same way once the IP is flagged).
There is no client-side workaround; it's the IP being challenged, not the request shape.

The durable fix is the `bgutil-ytdlp-pot-provider` plugin (`core/requirements.txt`) plus its
companion server, a `brainicism/bgutil-ytdlp-pot-provider` Docker container `deploy/setup.sh`
runs bound to `127.0.0.1:4416`. It mints yt-dlp a proof-of-origin token per request, which is
what actually satisfies the bot check — no code here calls it; yt-dlp auto-discovers the
plugin from `site-packages/yt_dlp_plugins/` the moment it's installed. Nothing expires on a
schedule and nobody has to export anything by hand, unlike the cookies fallback below. If
`ingestion failed` comes back with the same `"sign in to confirm"` message, check the
container is actually up first: `docker ps --filter name=bgutil-provider` and
`docker logs bgutil-provider`, before assuming the token approach itself has stopped working.

`YOUTUBE_COOKIES_FILE` (`core/app/config.py`) is the fallback if the provider container is
ever down and a video needs to go through right now: point it at a `cookies.txt` exported
from a real signed-in browser session (e.g. the "Get cookies.txt LOCALLY" extension), and
yt-dlp presents those cookies instead. No account password ever touches this app — cookies
are just proof-of-not-a-bot to YouTube, same as the token is. Both this and the pot-provider
container need `EnvironmentFile=`/being reachable from the systemd unit
(`deploy/tutor-backend.service`) to actually reach `pipeline/ingest.py`'s plain
`os.environ.get()` calls — `core/.env` is otherwise only ever parsed by `config.py`'s
pydantic `Settings`, which never exports it to the real process environment.

## Commands

```
cd core && python -m app.cli doctor     # what works, what doesn't, and the fix
cd core && python -m app.cli voices     # download Piper voices (ar/en/fr)
python -m pytest                        # from the repo root
python eval/run_eval.py
python pipeline/add_videos.py <folder> --dry-run && python pipeline/ingest.py
```
