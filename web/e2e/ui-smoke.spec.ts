import { expect, test } from "@playwright/test";

/**
 * Whole-app UI smoke coverage: the student dashboard renders with no console errors, and the
 * separate /admin portal gates correctly for three real identities — anonymous, a signed-in
 * non-admin, and the one allowlisted admin — matching the server-side security tests in
 * tests/test_course_admin_api.py, but exercised through the actual rendered page instead of
 * the raw API. JWTs are minted directly against the real dev DB (see the session's own
 * `auth.issue_user_token` usage) and injected into localStorage under "tutor.session" —
 * scripting a real Google OAuth popup isn't practical or reliable in a headless run.
 */
const ADMIN_JWT = process.env.E2E_ADMIN_JWT;
const STUDENT_JWT = process.env.E2E_STUDENT_JWT;

function collectConsoleErrors(page: import("@playwright/test").Page): string[] {
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(msg.text());
  });
  page.on("pageerror", (err) => errors.push(err.message));
  return errors;
}

test("student dashboard loads with no console errors", async ({ page }) => {
  const errors = collectConsoleErrors(page);
  await page.goto("/");
  await expect(page.locator("text=Tutor").first()).toBeVisible({ timeout: 15_000 });
  await expect(page.getByPlaceholder(/Ask anything|اسأل عن|Pose une question/)).toBeVisible({ timeout: 15_000 });
  expect(errors, `unexpected console errors: ${errors.join("\n")}`).toEqual([]);
});

test("anonymous visitor hitting /admin gets a clear message, not a stuck loading screen", async ({ page }) => {
  await page.goto("/admin");
  // Either a sign-in prompt (if Google Sign-In is configured) or the "not configured" message
  // — never an indefinite spinner with no way forward.
  await expect(page.locator("body")).not.toHaveText(/^\s*$/);
  await page.waitForTimeout(1000); // let any async /me + admin-check settle
  const stuckLoading = await page.locator("text=Loading…").isVisible().catch(() => false);
  expect(stuckLoading, "an anonymous /admin visit should never be stuck on the loading state").toBe(false);
});

test.skip(!STUDENT_JWT, "requires E2E_STUDENT_JWT");
test("a signed-in non-admin is refused the admin dashboard", async ({ page }) => {
  await page.addInitScript((tok) => localStorage.setItem("tutor.session", tok), STUDENT_JWT!);
  const errors = collectConsoleErrors(page);
  await page.goto("/admin");
  await expect(page.locator("text=not authorized").or(page.locator("text=isn't authorized"))).toBeVisible({ timeout: 15_000 });
  expect(errors, `unexpected console errors: ${errors.join("\n")}`).toEqual([]);
});

test.skip(!ADMIN_JWT, "requires E2E_ADMIN_JWT");
test("the allowlisted admin reaches the real admin dashboard", async ({ page }) => {
  await page.addInitScript((tok) => localStorage.setItem("tutor.session", tok), ADMIN_JWT!);
  const errors = collectConsoleErrors(page);
  await page.goto("/admin");
  await expect(page.locator("h1").first()).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("heading", { name: "Courses" })).toBeVisible();
  await expect(page.getByRole("button", { name: "+ Create Course" })).toBeVisible();
  expect(errors, `unexpected console errors: ${errors.join("\n")}`).toEqual([]);
});
