import { defineConfig, devices } from "@playwright/test";

/**
 * DOM-level coverage for the live-caption pipeline: a scripted fake SpeechRecognition drives
 * real app code (stt.ts + Chat.tsx), and assertions read the actual rendered transcript —
 * closing the gap between the reconcile() unit tests and how it is actually wired into the page.
 *
 * getUserMedia/MediaRecorder are NOT stubbed: Chromium's fake-device flags below make them
 * real (silent synthetic audio), so recordAndUpload's real code path runs, including its real
 * network call to whatever core instance VITE_API_URL points at. Only the speech *recognizer*
 * is fake — there is no way to script real recognition results deterministically otherwise.
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  // These tests hit a real core instance and (via /me's `avatar: true`) a real third-party
  // LiveAvatar sandbox session per test, not mocked infra — both introduce genuine,
  // load-dependent latency. Under parallel workers that latency can occasionally cross the
  // production 8s "give up on silence" VAD timer in stt.ts mid-test, which is a real timer
  // firing correctly, not a bug in the code under test. Serializing avoids racing a real
  // production timeout against how busy the machine happens to be.
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL || "http://localhost:5173",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        permissions: ["microphone"],
        launchOptions: {
          args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"],
        },
      },
    },
  ],
  // Reuses the dev server already running in this environment; starts one otherwise. Real
  // requests still need a core instance up (see README note in e2e/README.md).
  webServer: {
    command: "npm run dev",
    url: "http://localhost:5173",
    reuseExistingServer: true,
    timeout: 30_000,
  },
});
