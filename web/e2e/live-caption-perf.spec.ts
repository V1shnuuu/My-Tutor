/**
 * Perf sanity check for reconcile()'s O(n)-per-event / O(n²)-over-an-utterance behavior
 * (see src/lib/transcript.ts's docstring). This measures the real cost inside the browser —
 * React re-render included, not just the pure function — across enough events to stand in for
 * a genuinely long, uninterrupted utterance, without waiting the real wall-clock minutes out.
 *
 * A 2-minute utterance at a brisk speaking pace is roughly 150 committed sentence-level final
 * results (interim events arrive far more often per sentence but are cheap/short-lived by
 * comparison, so finals — which make the accumulated result list keep growing — are the
 * meaningful count for the O(n²) concern).
 */
import { expect, test } from "@playwright/test";
import { installFakeSpeechRecognition } from "./fake-speech-recognition";

const FINAL_RESULTS = 150;

test("reconciling ~150 accumulated final results (a long utterance) stays fast", async ({ page }) => {
  await page.addInitScript(installFakeSpeechRecognition);
  // See live-caption.spec.ts's openChatAndStartMic: must wait for /me before clicking, or the
  // mic falls through to the browser-only fallback lane instead of the one under test.
  const mePromise = page.waitForResponse((r) => r.url().endsWith("/me"));
  await page.goto("/");
  await mePromise;
  const mic = page.getByTestId("mic-button");
  await expect(mic).toBeVisible({ timeout: 15_000 });
  await mic.click();
  await page.waitForFunction(
    () => (window as unknown as { __fakeRecognizers?: unknown[] }).__fakeRecognizers?.length === 1,
    undefined,
    { timeout: 10_000 },
  );

  const { totalMs, lastEventMs } = await page.evaluate((n) => {
    const rec = (window as unknown as {
      __fakeRecognizers: { __result(p: [string, boolean][]): void }[];
    }).__fakeRecognizers[0];
    const parts: [string, boolean][] = [];
    let lastEventMs = 0;
    const t0 = performance.now();
    for (let i = 0; i < n; i++) {
      // Each call replays the WHOLE list so far, exactly like a real engine re-delivering
      // finalized indices — this is what makes result-list length the thing that matters.
      parts.push([`sentence number ${i} `, true]);
      const before = performance.now();
      rec.__result(parts.concat([["and continuing", false]]));
      lastEventMs = Math.max(lastEventMs, performance.now() - before);
    }
    return { totalMs: performance.now() - t0, lastEventMs };
  }, FINAL_RESULTS);

  console.log(`[perf] ${FINAL_RESULTS} accumulated results: total ${totalMs.toFixed(1)}ms, slowest single event ${lastEventMs.toFixed(1)}ms`);

  // Generous thresholds — this asserts "not pathological", not a tight budget. React's render
  // plus a few-thousand-character string rebuild per event should not be anywhere close to
  // these on any machine capable of running the tutor at all.
  expect(lastEventMs, "the last (largest) event must not itself be a visible stutter").toBeLessThan(50);
  expect(totalMs, "150 accumulated events total").toBeLessThan(2000);

  await expect(page.getByTestId("live-interim")).toHaveText("and continuing");
  await expect(page.getByTestId("live-committed")).toContainText("sentence number 149");
});
