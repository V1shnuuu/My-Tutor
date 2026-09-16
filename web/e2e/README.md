# e2e — live caption / STT DOM tests

Playwright tests that drive the real app in a real Chromium instance, with only the speech
*recognizer* replaced by a scripted double (`fake-speech-recognition.ts`). Everything else is
real: `getUserMedia`/`MediaRecorder` via Chromium's fake-device flags, the real `stt.ts`, the
real `Chat.tsx` rendering. They exist to close the gap the unit tests in
`../src/lib/transcript.test.ts` leave — those prove `reconcile()` is correct in isolation; these
prove it's wired into the page correctly.

## Prerequisites

A core instance must be running and reachable at whatever `VITE_API_URL` the frontend is built
with (see `.env.local`), and it must report `stt: true` from `/me` — otherwise `startListening`
takes the browser-only fallback lane instead of the one these tests target, and everything will
either fail confusingly or silently test the wrong code path.

```
cd core && python -m app.cli doctor      # confirm stt: available
cd core && uvicorn app.main:app          # if not already running
cd web  && npm run dev                    # if not already running
```

## Running

```
cd web
npx playwright install chromium   # once
npx playwright test               # runs e2e/*.spec.ts
```

`playwright.config.ts` runs these serially (`workers: 1`), deliberately — see the comment
there. They hit a live backend and (if `LIVEAVATAR_ENABLED`) a real third-party avatar session
per test; running them in parallel races real, load-dependent latency against the production
8-second silence-timeout in `stt.ts`; that is a real timer correctly firing under load, not a
bug in the code being tested. It'll manifest as `waitForRecognizer` timing out.

## What's covered here vs. elsewhere

- **`../src/lib/transcript.test.ts`** (`npm test`, no Playwright, no browser): the pure
  `reconcile()` logic — duplicate replay protection, whitespace, punctuation preservation,
  Arabic/French text. Run this for quick iteration on the reconciliation algorithm itself.
- **This directory**: the DOM wiring around that logic — does the real page actually render the
  committed/interim split correctly, does a recognizer restart preserve prior text, does the
  app degrade gracefully with no Web Speech API at all.
- **`MANUAL_QA.md`**: what neither can cover — a real microphone, a real voice, a real OS
  permission prompt, real background noise.
