import { expect, test } from "@playwright/test";
import type { ClientReleaseRunView } from "../../src/generated/openapi.ts";

test("Preview-era note markers identify each PR only in the engineering view", async ({ page }, testInfo) => {
  await page.goto("/ui/owner-review?product=example-site&fixture=products");
  await expect(page.getByRole("heading", { name: "Review this release" })).toBeVisible();
  const response = await page.evaluate(async () => {
    const fixtures = await import("/ui/src/dev-fixtures.ts");
    return fixtures.releaseReviewForFixture("products");
  });
  response.review.checklist.items[0].preview_era_notes = true;
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", csrf_token: "fixture", identity: { login: "engineer", github_id: 9002, role: "admin", organizations: [], teams: [] } } }));
  await page.route("**/v1/release-review?*", route => route.fulfill({ json: { ...response, viewer_is_owner: false, can_override: true } }));
  await page.goto("/ui/owner-review?product=example-site");
  const marker = page.getByText("PR #42: preview-era test links replaced with testing-site instructions.", { exact: true });
  await expect(marker).toBeVisible();
  await expect(page.getByText("PR #45: preview-era test links replaced with testing-site instructions.", { exact: true })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("preview-era-admin.png"), fullPage: true });
  await page.unroute("**/v1/release-review?*");
  await page.route("**/v1/release-review?*", route => route.fulfill({ json: response }));
  await page.reload();
  await expect(page.getByText("Reviewing as the Client", { exact: true })).toBeVisible();
  await expect(marker).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("preview-era-client.png"), fullPage: true });
});

test("Failed releases distinguish automatic recovery from the rollback drill", async ({ page }, testInfo) => {
  await page.goto("/ui/owner-review?product=example-site&fixture=products");
  await expect(page.getByRole("heading", { name: "Review this release" })).toBeVisible();
  const response = await page.evaluate(async () => {
    const modulePath = "/ui/src/dev-fixtures.ts";
    const fixtures = await import(modulePath);
    return fixtures.releaseDecisionForFixture(fixtures.releaseReviewForFixture("products"), "accepted", "");
  });
  const run: ClientReleaseRunView = {
    decision_record_id: "fixture-release-decision", rollback_drill: true, state: "stopped",
    blocked_reason: "",
    steps: [
      { step: "promote-1", kind: "promote", status: "fail", operation_id: "failed-promotion" },
      { step: "failure-recovery-1", kind: "recovery", status: "pass", operation_id: "verified-recovery" },
      { step: "rollback-drill", kind: "rollback", status: "not_started", operation_id: "" },
    ],
  };
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", csrf_token: "fixture", identity: { login: "site-owner", github_id: 9001, role: "read_only", organizations: [], teams: [] } } }));
  await page.route("**/v1/release-review?*", route => route.fulfill({ json: { ...response, release_run: run } }));
  await page.goto("/ui/owner-review?product=example-site");
  const progress = page.getByRole("region", { name: "Release progress" });
  await expect(progress.getByRole("heading")).toHaveText("Release progress: Stopped");
  await expect(progress).toContainText("Put this version live: Failed");
  await expect(progress).toContainText("Automatic recovery: restore the previous passing version: Done");
  await expect(progress).toContainText("Rollback drill: return to the current version: Not started");
  await page.screenshot({ path: testInfo.outputPath("release-recovered.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

  run.state = "waiting";
  run.steps = [];
  run.blocked_reason = "Launchplane needs a verified previous passing version before this release can start.";
  await page.reload();
  await expect(progress.getByRole("status")).toHaveText(run.blocked_reason);
  await page.screenshot({ path: testInfo.outputPath("release-no-baseline.png"), fullPage: true });
});

test("Owner cannot accept undisclosed shared component changes", async ({ page }) => {
  await page.goto("/ui/owner-review?product=example-site&fixture=missing");
  await expect(page.getByText("Shared website components changed outside this repository's checklist. Admin review is required.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Accept release" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Record admin approval override" })).toHaveCount(0);
  await expect(page.getByText("Releases are on hold for Example site.")).toBeVisible();
});

test("An unavailable checklist names its reason code and trace ID", async ({ page }) => {
  await page.goto("/ui/owner-review?product=example-site&fixture=empty");
  await expect(page.getByText("Production has no recorded deployed version.", { exact: false })).toBeVisible();
  await expect(page.locator("code", { hasText: "production_identity_missing" })).toBeVisible();
  await expect(page.locator("code", { hasText: "fixture-release-review" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Accept release" })).toHaveCount(0);
});

test("Owner reviews the complete release and can request changes after accepting", async ({ page }, testInfo) => {
  const mutations: string[] = [];
  const errors: string[] = [];
  page.on("request", request => { if (request.method() !== "GET") mutations.push(request.url()); });
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/ui/owner-review?product=example-site&fixture=products");
  await expect(page.getByRole("heading", { name: "Review this release" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Open the testing site" })).toHaveAttribute("href", "https://testing.example.invalid/");
  const checks = page.locator(".release-review-checklist").first().locator(":scope > li");
  await expect(checks).toHaveCount(1);
  await expect(checks.getByText("On a phone, confirm the booking button is visible.", { exact: false })).toBeVisible();
  await expect(checks.locator(".release-review-changes > li")).toHaveCount(2);
  for (const repository of ["example/shared-addons", "example/disable-online"]) {
    const shared = page.getByRole("region", { name: `Shared website components from ${repository}`, exact: true });
    await expect(shared).toContainText("Sign in as a staff user and confirm your usual pages open.");
    await expect(shared).toContainText("Preserve staff sign-in");
  }
  const untested = page.locator(".release-review-untested");
  await expect(untested.getByText("2 changes need nothing from you")).toBeVisible();
  await expect(untested.getByText("Speed up CI")).toBeHidden();
  await expect(page.getByRole("button", { name: "Request changes" })).toBeDisabled();
  const versions = page.locator(".release-review-technical");
  await expect(versions.locator("dl")).toBeHidden();
  await expect(page.getByText("Accepting puts this version on the live site, Example site (www.example.invalid).")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("owner-initial.png"), fullPage: true });
  await page.getByRole("button", { name: "Accept release" }).click();
  await expect(page.getByRole("status")).toContainText("Launchplane is starting it");
  await expect(page.getByRole("region", { name: "Release progress" }).getByRole("listitem")).toHaveCount(5);
  await expect(page.getByRole("region", { name: "Latest release decision" })).toContainText("site-owner");
  await page.screenshot({ path: testInfo.outputPath("owner-accepted.png"), fullPage: true });
  const feedback = "The booking button needs a clearer label.\nKeep the contact link visible on a phone.";
  await page.getByRole("textbox").fill(feedback);
  await page.getByRole("button", { name: "Request changes" }).click();
  await expect(page.getByRole("region", { name: "Latest release decision" }).getByRole("blockquote")).toHaveText(feedback);
  await versions.getByText("Technical details", { exact: true }).click();
  await expect(versions).toContainText("Shared components from example/shared-addons");
  await expect(versions).toContainText("Shared components from example/disable-online");
  const sharedSources = await page.evaluate(async () => {
    const modulePath = "/ui/src/dev-fixtures.ts";
    const fixtures = await import(modulePath);
    return fixtures.releaseReviewForFixture("products").review.checklist?.shared_sources ?? [];
  });
  for (const source of sharedSources) {
    const range = versions.locator("dl > div").filter({ has: page.getByText(`Shared components from ${source.repository}`, { exact: true }) });
    await expect(range.locator("code")).toHaveText([source.production_commit, source.candidate_commit]);
  }
  await page.screenshot({ path: testInfo.outputPath("owner-changes-requested.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  expect(mutations).toEqual([]);
  expect(errors).toEqual([]);
});

test("Missing shared PR instructions disable Client acceptance", async ({ page }) => {
  await page.goto("/ui/owner-review?product=example-site&fixture=denied");
  await expect(page.getByRole("region", { name: "Shared website components from example/shared-addons", exact: true })).toContainText("#52 has no Client test notes.");
  await expect(page.getByRole("button", { name: "Accept release", exact: true })).toBeDisabled();
});

test("Operator records a separate reasoned override without Owner controls", async ({ page }, testInfo) => {
  const mutations: string[] = [];
  page.on("request", request => { if (request.method() !== "GET") mutations.push(request.url()); });
  await page.goto("/ui/owner-review?product=example-site&fixture=operator");
  await expect(page.getByRole("button", { name: "Accept release" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Request changes" })).toHaveCount(0);
  const override = page.getByRole("region", { name: "Admin Approval Override", exact: true });
  const record = override.getByRole("button");
  await expect(record).toBeDisabled();
  await page.screenshot({ path: testInfo.outputPath("operator-initial.png"), fullPage: true });
  await override.getByRole("textbox").fill("Reviewed the missing instructions with the Client.");
  await record.click();
  await expect(page.getByRole("status")).toBeVisible();
  const latest = page.getByRole("region", { name: "Latest release decision" });
  await expect(latest).toContainText("site-operator");
  await expect(latest.getByRole("blockquote")).toHaveText("Reviewed the missing instructions with the Client.");
  await page.screenshot({ path: testInfo.outputPath("operator-recorded.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  expect(mutations).toEqual([]);
});

test("A saved decision exposes publication failure and allows retry", async ({ page }) => {
  await page.goto("/ui/owner-review?product=example-site&fixture=error");
  const latest = page.getByRole("region", { name: "Latest release decision" });
  await expect(latest.getByRole("alert")).toBeVisible();
  await page.getByRole("button", { name: "Accept release", exact: true }).click();
  await expect(page.getByRole("status")).toBeVisible();
  await expect(latest.getByRole("alert")).toHaveCount(0);
});
