# Manual QA — live caption / STT

Things automated tests can't cover: a real microphone, a real voice, a real OS permission
prompt, real background noise. Run this once per release that touches `stt.ts` or `Chat.tsx`'s
transcript rendering. Needs `npm run dev` and a running core instance with `LOCAL_STT_ENABLED`
(or `GROQ_API_KEY`) so `/me` reports `stt: true` — otherwise the mic falls back to the
browser-only lane and you're testing different code than these steps describe.

For each: steps, then what you should see. If it doesn't match, note the actual behavior and
file it — don't just try again.

## 1. Real background noise / accent variation

- Click the mic somewhere with normal ambient noise (a fan, faint traffic, another
  conversation in the room) and ask a real question in your own accent.
- **Expect**: the live caption keeps up with your words; noise doesn't cause it to freeze,
  restart repeatedly (watch the console with `VITE_DEBUG_STT=1` — you'd see repeated `live
  caption (re)started` lines), or show a wall of garbled text unrelated to what you said.
- The **committed (solid) text does not have to be perfectly accurate** — that's the caption,
  not the answer. What matters: the final answer that comes back should still correctly reflect
  your real question, since it comes from server-side Whisper, not from what the caption showed.

## 2. Pause 10+ seconds mid-sentence, then resume

- Click the mic, say "What is the difference between", then go silent for 10+ seconds, then
  continue "...a stack and a queue?".
- **Expect**: the mic does NOT cut off and submit "What is the difference between" as a
  question on its own after the pause — Arabic and English both get ~1.1-1.3s of *trailing*
  silence tolerance (see `hang` in `stt.ts`), but a mid-sentence pause with the mic still
  actively held open should not trigger that; it only fires once the whole utterance has holds
  no further speech. If your pause is long enough with the mic still open and you resume before
  the ~30s hard cap, the full sentence should be transcribed and answered as one question.
- Also watch the caption during the pause: it should sit still showing what was said so far,
  not clear itself.

## 3. Deny mic permission mid-session (not just at start)

- Start a normal voice question, let it work once successfully.
- Click the mic again to start a second one, then **revoke** microphone permission from the
  browser's site settings while it's actively listening (not before clicking).
- **Expect**: a clear error surfaces (the app's mic-denied toast/message) rather than a frozen
  "Listening…" state or a silent hang. The composer should return to a normal, usable state
  (typing a question by hand must still work) — it must not get stuck disabled.

## 4. Switch between Arabic and English mid-sentence

- Set the UI language to English, then click the mic and say an English sentence that switches
  to Arabic partway through (e.g., "Can you explain لية إحنا بنستخدم the recursion here؟").
- **Expect**: this is a known, accepted limitation, not a bug to chase — see finding §1.2 in the
  audit. The Web Speech caption is locked to whichever language was configured when the mic
  started (English, in this case), so the Arabic portion of the *live preview* may show as
  garbled or nonsense English-phoneme text. That is cosmetic only.
- What actually matters: the **final answer** (after the mic stops) should still be correct,
  because the real transcription comes from server-side Whisper with language auto-detection,
  not from the Web Speech caption. Confirm the final answer correctly reflects the mixed-language
  question — if the *final* answer is wrong, that's a real regression; if only the live caption
  looked odd mid-sentence, that's expected.

## 5. Very long uninterrupted speech (2+ minutes)

- Click the mic and talk continuously for 2+ minutes without a long pause (reading from an
  article works well for this).
- **Expect**: no visible stutter or lag in the caption keeping up with your speech, no dropped
  words, no browser tab slowdown. The caption box should auto-scroll to keep the most recent
  words in view rather than growing the page layout.
- This is the scenario the reconciliation performance note (audit §4) covers: the underlying
  computation does get more expensive as the transcript grows, but it's been measured up to a
  20-minute-equivalent synthetic transcript at a few milliseconds per update — you should not
  be able to perceive it. If you *do* notice real lag here, that's a genuine finding worth
  reporting precisely (how long you spoke before it appeared), since it would contradict the
  measured data and be worth re-investigating.
