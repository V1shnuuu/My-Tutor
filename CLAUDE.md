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

## Commands

```
cd core && python -m app.cli doctor     # what works, what doesn't, and the fix
cd core && python -m app.cli voices     # download Piper voices (ar/en/fr)
python -m pytest                        # from the repo root
python eval/run_eval.py
python pipeline/add_videos.py <folder> --dry-run && python pipeline/ingest.py
```
