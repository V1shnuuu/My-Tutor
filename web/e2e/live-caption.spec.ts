/**
 * DOM-level tests for the live-caption pipeline (see fake-speech-recognition.ts for how the
 * recognizer is stubbed). These exercise the real app — real getUserMedia via Chromium's
 * fake-device flags, real MediaRecorder, real stt.ts, real Chat.tsx rendering — with only the
 * speech *recognizer* replaced, since there is no other way to script deterministic recognition
 * events. They close the gap the reconcile() unit tests (src/lib/transcript.test.ts) leave:
 * those prove the pure function; these prove it is wired into the page correctly.
 *
 * Needs a running core instance behind the frontend (VITE_API_URL / .env.local) — /me must
 * report stt: true or the app takes the browser-only fallback lane instead of the one these
 * tests target. See README.md in this directory.
 */
import { expect, test, type Page } from "@playwright/test";
import { installFakeSpeechRecognition, removeSpeechRecognition } from "./fake-speech-recognition";

async function openChatAndStartMic(page: Page) {
  // App.tsx's bootstrap effect fetches /me asynchronously and only then learns whether the
  // server STT lane is available (sttServer) — the composer renders immediately regardless, so
  // clicking the mic before that state actually commits silently takes the browser-only
  // fallback lane instead of the one under test (a real startup race — see the report).
  // Waiting for the /me *response* is necessary but not sufficient: React still has to parse
  // the body, call setSttServer, and re-render before the button's own onClick closes over the
  // new value. Rather than guess a fixed delay for that, retry the click until it actually
  // lands — clicking again while not yet listening is a harmless no-op-then-retry either way.
  const mePromise = page.waitForResponse((r) => r.url().endsWith("/me"));
  await page.goto("/");
  await mePromise;
  const mic = page.getByTestId("mic-button");
  await expect(mic).toBeVisible({ timeout: 15_000 });
  await expect(async () => {
    await mic.click();
    await expect(mic).toHaveAttribute("data-listening", "true", { timeout: 500 });
  }).toPass({ timeout: 10_000 });
}

/** The recognizer instance recordAndUpload's armLiveCaption() creates is asynchronous relative
 *  to the click (it waits on getUserMedia + MediaRecorder construction first), so wait for it
 *  rather than assuming it exists the instant the button flips to listening. */
async function waitForRecognizer(page: Page, count = 1) {
  await page.waitForFunction(
    (n) => (window as unknown as { __fakeRecognizers?: unknown[] }).__fakeRecognizers?.length === n,
    count,
    { timeout: 10_000 },
  );
}

test.describe("live transcript preview", () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(installFakeSpeechRecognition);
  });

  test("progressive interim results render live, word by word", async ({ page }) => {
    await openChatAndStartMic(page);
    await waitForRecognizer(page);

    const committed = page.getByTestId("live-committed");
    const interim = page.getByTestId("live-interim");
    const step = async (text: string) => page.evaluate(
      (t) => (window as unknown as { __fakeRecognizers: { __result(p: [string, boolean][]): void }[] })
        .__fakeRecognizers[0].__result([[t, false]]),
      text,
    );

    await step("Hello");
    await expect(interim).toHaveText("Hello");
    await step("Hello my");
    await expect(interim).toHaveText("Hello my");
    await step("Hello my name");
    await expect(interim).toHaveText("Hello my name");
    await step("Hello my name is Vishnu");
    await expect(interim).toHaveText("Hello my name is Vishnu");
    await expect(committed).toHaveText("");   // nothing finalized yet — still all interim
  });

  test("a replayed already-final result does not duplicate the committed text", async ({ page }) => {
    // The exact bug this whole effort exists to close: Chrome re-delivering a resultIndex it
    // already finalized. Firing the identical final event twice must not double the words.
    await openChatAndStartMic(page);
    await waitForRecognizer(page);

    const fire = (parts: [string, boolean][]) => page.evaluate(
      (p) => (window as unknown as { __fakeRecognizers: { __result(p: [string, boolean][]): void }[] })
        .__fakeRecognizers[0].__result(p),
      parts,
    );

    await fire([["Hello my name", true]]);
    await expect(page.getByTestId("live-committed")).toHaveText("Hello my name");
    await fire([["Hello my name", true]]);   // replay of the same final result
    await expect(page.getByTestId("live-committed")).toHaveText("Hello my name");
    await fire([["Hello my name", true]]);   // and again
    await expect(page.getByTestId("live-committed")).toHaveText("Hello my name");
  });

  test("recognizer silently ending mid-utterance restarts and preserves prior text", async ({ page }) => {
    // Chrome's documented quirk: continuous recognition can end on its own after a pause, with
    // no onerror. Before the fix this froze the caption; armLiveCaption() now restarts it.
    await openChatAndStartMic(page);
    await waitForRecognizer(page);

    await page.evaluate(() => (window as unknown as {
      __fakeRecognizers: { __result(p: [string, boolean][]): void; __end(): void }[];
    }).__fakeRecognizers[0].__result([["Hello my name", true]]));
    await expect(page.getByTestId("live-committed")).toHaveText("Hello my name");

    // The engine ends itself without any error — not a stop() call from our own code.
    await page.evaluate(() => (window as unknown as {
      __fakeRecognizers: { __end(): void }[];
    }).__fakeRecognizers[0].__end());

    // armLiveCaption() must have started a second recognizer instance.
    await waitForRecognizer(page, 2);

    // The caption must not have frozen or reset to empty in between.
    await expect(page.getByTestId("live-committed")).toHaveText("Hello my name");

    await page.evaluate(() => (window as unknown as {
      __fakeRecognizers: { __result(p: [string, boolean][]): void }[];
    }).__fakeRecognizers[1].__result([[" is Vishnu", true]]));

    // Carried text plus the new instance's result, not just the new instance's alone.
    await expect(page.getByTestId("live-committed")).toHaveText("Hello my name is Vishnu");
  });

  test("an unrelated recognition error does not clear the transcript or crash the page", async ({ page }) => {
    const pageErrors: Error[] = [];
    page.on("pageerror", (e) => pageErrors.push(e));

    await openChatAndStartMic(page);
    await waitForRecognizer(page);

    await page.evaluate(() => (window as unknown as {
      __fakeRecognizers: { __result(p: [string, boolean][]): void }[];
    }).__fakeRecognizers[0].__result([["Testing", false]]));
    await expect(page.getByTestId("live-interim")).toHaveText("Testing");

    await page.evaluate(() => (window as unknown as {
      __fakeRecognizers: { __error(e: string): void }[];
    }).__fakeRecognizers[0].__error("network"));

    await expect(page.getByTestId("live-interim")).toHaveText("Testing");
    expect(pageErrors).toHaveLength(0);
  });
});

test.describe("Web Speech API unavailable (Firefox-equivalent)", () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(removeSpeechRecognition);
  });

  test("mic still works and degrades gracefully with no live caption", async ({ page }) => {
    const pageErrors: Error[] = [];
    page.on("pageerror", (e) => pageErrors.push(e));

    // openChatAndStartMic retries the click until data-listening is actually true, so its
    // return already proves the server lane was reached with no Web Speech API present — the
    // one thing worth asserting further is that nothing crashed getting there.
    await openChatAndStartMic(page);

    // No recognizer exists to ever call onInterim, so transcript.text stays empty and
    // Chat.tsx's own empty-state branch renders — the plain "Listening…" placeholder, not the
    // committed/interim spans (those only exist once there is something to show).
    const liveBox = page.getByTestId("live-transcript");
    await expect(liveBox).toBeVisible();
    await expect(liveBox.getByTestId("live-committed")).toHaveCount(0);
    await expect(liveBox).toContainText("Listening");
    expect(pageErrors).toHaveLength(0);

    // Stopping must also not throw with no recognizer ever having existed.
    await page.getByTestId("mic-button").click();
    await expect(pageErrors).toHaveLength(0);
  });
});
