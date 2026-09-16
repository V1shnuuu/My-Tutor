/**
 * Injected into the page via `page.addInitScript` — runs before any app script, which is
 * required: `stt.ts` reads `window.SpeechRecognition || window.webkitSpeechRecognition` into a
 * module-level constant the moment it is imported, so the stub must exist first.
 *
 * This is a controllable double, not a simulator of real recognition: `start()`/`stop()` do
 * nothing on their own. The test scripts every event by calling `__result`, `__end`, `__error`
 * on an instance pulled from `window.__fakeRecognizers`, which is exactly what lets a test
 * script the Chrome replay-already-final bug as a literal, repeatable event sequence.
 *
 * Written as a plain function (not a module) because `addInitScript` evaluates its source in
 * the page, with no bundler and no access to this file's own imports.
 */
export function installFakeSpeechRecognition() {
  class FakeSpeechRecognition {
    lang = "";
    interimResults = false;
    continuous = false;
    maxAlternatives = 1;
    onresult: ((e: unknown) => void) | null = null;
    onerror: ((e: unknown) => void) | null = null;
    onend: (() => void) | null = null;
    private started = false;

    constructor() {
      const w = window as unknown as { __fakeRecognizers: FakeSpeechRecognition[] };
      w.__fakeRecognizers ||= [];
      w.__fakeRecognizers.push(this);
    }

    start() { this.started = true; }
    // A real engine fires `end` after both stop() (graceful) and abort() (immediate) — the
    // fake matches that so cleanup code (stt.ts's cleanup()) exercises its real onend path.
    stop() { if (this.started) { this.started = false; this.onend?.(); } }
    abort() { if (this.started) { this.started = false; this.onend?.(); } }

    /** parts: [transcript, isFinal][], exactly one SpeechRecognitionEvent's whole result list —
     *  this is the "replay from index 0" shape reconcile() expects, not an incremental diff. */
    __result(parts: Array<[string, boolean]>) {
      const results = parts.map(([transcript, isFinal]) => ({ isFinal, 0: { transcript } }));
      this.onresult?.({ results, resultIndex: 0 });
    }
    /** Simulates Chrome's documented quirk: continuous recognition ends on its own after a
     *  long pause, with no matching onerror — indistinguishable from a deliberate stop(). */
    __end() { this.started = false; this.onend?.(); }
    __error(error: string) { this.onerror?.({ error }); }
  }

  const w = window as unknown as { SpeechRecognition: unknown; webkitSpeechRecognition: unknown };
  w.SpeechRecognition = FakeSpeechRecognition;
  w.webkitSpeechRecognition = FakeSpeechRecognition;
}

/** Simulates Firefox / a browser that never implemented the Web Speech API at all. Must also
 *  run via addInitScript, before stt.ts's module-level feature check. */
export function removeSpeechRecognition() {
  const w = window as unknown as { SpeechRecognition?: unknown; webkitSpeechRecognition?: unknown };
  delete w.SpeechRecognition;
  delete w.webkitSpeechRecognition;
}
