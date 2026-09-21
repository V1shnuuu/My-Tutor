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

**Confirmed the hard way, so don't re-attempt these as the fix**: neither a PO-token
provider (`bgutil-ytdlp-pot-provider`, still installed via `core/requirements.txt` plus the
`brainicism/bgutil-ytdlp-pot-provider` Docker container `deploy/setup.sh` runs on
`127.0.0.1:4416`) nor `YOUTUBE_COOKIES_FILE` cleared this on Oracle. Both were deployed and
tested live — `docker logs bgutil-provider` showed the token server healthy, the cookies file
was verified working from a normal residential IP — and yt-dlp still returned the identical
`"sign in to confirm"` error from the server. Forcing yt-dlp's player client
(`android`/`web`/`tv`, working around a separate real bug where this yt-dlp version's default
`visionos` client is broken) didn't help either. Getting the VM a **completely fresh** Oracle
ephemeral public IP didn't help either — the replacement was blocked identically, on the
*first* request, before this app had ever made one. That rules out "this one IP got flagged
from our own traffic" — it's Oracle's free-tier IP pool being challenged as a class, not a
per-IP reputation building up over time. Cookies and the PO-token provider are still wired in
(harmless, and may matter for less-severe blocks or other hosts), but treat them as
insufficient on their own for a host on a flagged range — don't spend time re-verifying they
"should" work.

The fix that actually clears it is `YOUTUBE_PROXY` (`core/app/config.py`, wired into both
`core/app/youtube.py` and `pipeline/ingest.py`): a residential-IP proxy
(`http://user:pass@host:port` or `socks5://...`) that routes yt-dlp's YouTube traffic through
a non-datacenter IP, which is the only thing that changes what YouTube sees enough to matter.
This is a deliberate, deployment-specific exception to rule #1 ("free and quota-free") — made
explicitly, at the deploying admin's discretion, because the alternative (a manual
download-and-upload flow) breaks the "paste a link" UX the admin dashboard is built around.
Don't treat this as license to add other paid dependencies elsewhere in the project.

`YOUTUBE_COOKIES_FILE`/`YOUTUBE_PROXY`/the pot-provider container all need
`EnvironmentFile=`/being reachable from the systemd unit (`deploy/tutor-backend.service`) to
actually reach `pipeline/ingest.py`'s plain `os.environ.get()` calls — `core/.env` is
otherwise only ever parsed by `config.py`'s pydantic `Settings`, which never exports it to the
real process environment.

Separately, now fixed but worth knowing the shape of: `deploy/setup.sh` used to
unconditionally overwrite `/etc/caddy/Caddyfile` from the repo's placeholder-domain template
on every run, even when a real domain (or, as deployed here, a `<dashes-ip>.sslip.io`
free-TLS hostname) was already live in it. It never restarted Caddy itself, so an
already-running Caddy kept serving correctly from memory right up until the next restart for
any reason (reboot, an unattended-upgrade, a future deploy) — at which point it would have
tried to get a cert for the literal placeholder string and taken the backend down. Live-hit
this exact sequence deploying the fixes above. `setup.sh` now only writes `/etc/caddy/Caddyfile`
when one doesn't already exist; if you ever hand-edit it outside that script, the same care
still applies before anything restarts Caddy.

## 10. Multiple courses can be published at once

The app used to hard-assume exactly one published course system-wide — `publish_course()`
auto-unpublished every other course, `published_course()` did `... LIMIT 1`, and the student
frontend had no course switcher at all. An admin running several differently-themed courses
(e.g. a physics unit and a separate film-sound unit) needs both live and independently
answerable, not one silently un-publishing the other.

Now: publishing a course only ever touches that course. `content.published_course()`,
`published_video_ids()`, and `student_course_tree()` each take an optional `course_id` —
given one, they scope to exactly that course (and only if it's actually published; a draft's
id never widens a scope). Given none, they fall back to "the one published course" only when
there's exactly one — the zero-friction path for any deployment that only ever runs a single
course, unchanged from before. With two or more published at once and no `course_id`, the
fallback is deliberately ambiguous (`None`/empty, never a guess) — `GET /courses` lists what's
live, the frontend's `CoursePicker` shows it once `getPublishedCourses()` returns more than
one, and the student's pick is threaded through `/course`, `/search`, and `/chat` as
`course_id` from then on (remembered per-browser via `localStorage`, not trusted without
re-checking it's still published).

**Retrieval itself needed no redesign.** `chat.py`/`corpus.py` only ever dealt in a
`set[str]` of allowed `video_id`s; the single-course assumption lived entirely in what
produced that set. If you touch grounding/retrieval scoping, keep it that way — `course_id`
stays a `content.py`/`main.py` concern, not something `corpus.search()` needs to know about.

**Deliberately not done here, flagged for whoever picks it up next**: `GET /videos` and
`/videos/{id}/{captions.vtt,transcript,quiz}` (`main.py`) take a bare `video_id`/`corpus_video_id`
with **no course-membership check at all** — a pre-existing gap predating multi-course, made
more exposed by it (a draft or unpublished course's video id can still be fetched directly by
anyone who has it). Fixing it needs distinguishing "static `pipeline/videos.yaml` videos"
(meant to stay always open) from "admin-course videos not currently in any published course"
(should be gated) — don't gate these endpoints by "must belong to a published course" without
that distinction, or you'll break the static-corpus-only deployment path.

**Also deliberately not done**: per-course admin ownership. `ADMIN_EMAILS` stays one global
allowlist — any admin can manage any course, same as before multi-course. `courses.created_by`
is an audit field only, never checked for authorization.

## Commands

```
cd core && python -m app.cli doctor     # what works, what doesn't, and the fix
cd core && python -m app.cli voices     # download Piper voices (ar/en/fr)
python -m pytest                        # from the repo root
python eval/run_eval.py
python pipeline/add_videos.py <folder> --dry-run && python pipeline/ingest.py
```
