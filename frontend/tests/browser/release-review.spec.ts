import { expect, test } from "@playwright/test";
import type { ClientReleaseRunView } from "../../src/generated/openapi.ts";
import { RELEASE_PROGRESS_REFRESH_MS } from "../../src/OwnerReleaseReviewRoute";

test("Identical testing and production preserve the completed run without decision controls @responsive", async ({ page }, testInfo) => {
  await page.goto("/ui/owner-review?product=example-site&fixture=products");
  await expect(page.getByRole("heading", { name: "Review this release", exact: true })).toBeVisible();
  const response = await page.evaluate(async () => {
    const modulePath = "/ui/src/dev-fixtures.ts";
    const fixtures = await import(modulePath);
    return fixtures.releaseReviewForFixture("products");
  });
  response.review.checklist.production = { ...response.review.checklist.candidate };
  response.review.checklist.items = [];
  response.review.checklist.shared_sources = [];
  response.review = { ...response.review, required: false, approved: false, no_new_release: true, blockers: [] };
  response.release_run = {
    decision_record_id: "completed-release", rollback_drill: false, state: "passed", blocked_reason: "",
    steps: [{ step: "promote-1", kind: "promote", status: "pass", operation_id: "completed-promotion", failure: null }],
  };
  const writes: string[] = [];
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", csrf_token: "fixture", identity: { login: "site-owner", github_id: 9001, role: "read_only", organizations: [], teams: [] } } }));
  await page.route("**/v1/release-review?*", route => route.fulfill({ json: response }));
  await page.route("**/v1/release-review/decisions", route => {
    writes.push(route.request().method());
    return route.fulfill({ status: 409, json: {} });
  });
  await page.goto("/ui/owner-review?product=example-site");
  await expect(page.getByRole("heading", { name: "No new release to accept" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Release progress" })).toContainText("Release progress: Live");
  await expect(page.getByRole("button", { name: "Accept release" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Request changes", exact: true })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("no-new-release-client.png"), fullPage: true });
  response.viewer_is_owner = false;
  response.can_override = true;
  response.can_start_temporary_hotfix = true;
  await page.reload();
  for (const name of ["Record admin approval override", "Ship temporary hotfix now", "Ask site-owner to review now"]) {
    await expect(page.getByRole("button", { name, exact: true })).toHaveCount(0);
  }
  expect(writes).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("Preview-era note markers identify each PR only in the engineering view", async ({ page }, testInfo) => {
  await page.goto("/ui/owner-review?product=example-site&fixture=products");
  await expect(page.getByRole("heading", { name: "Review this release", exact: true })).toBeVisible();
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
  await expect(page.getByRole("heading", { name: "Review this release", exact: true })).toBeVisible();
  const response = await page.evaluate(async () => {
    const modulePath = "/ui/src/dev-fixtures.ts";
    const fixtures = await import(modulePath);
    return fixtures.releaseDecisionForFixture(fixtures.releaseReviewForFixture("products"), "accepted", "");
  });
  const run: ClientReleaseRunView = {
    decision_record_id: "fixture-release-decision", rollback_drill: true, state: "stopped",
    blocked_reason: "",
    steps: [
      { step: "promote-1", kind: "promote", status: "fail", operation_id: "failed-promotion", failure: null },
      { step: "failure-recovery-1", kind: "recovery", status: "pass", operation_id: "verified-recovery", failure: null },
      { step: "rollback-drill", kind: "rollback", status: "not_started", operation_id: "", failure: null },
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
  await expect(page.getByRole("heading", { name: "Review this release", exact: true })).toBeVisible();
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
  // The first line tells a non-Client who the Client is and what their decision does.
  const role = page.locator(".owner-review-card > .owner-review-state").first();
  await expect(role).toContainText("You are not the Client. site-owner is the Client");
  await expect(role).toContainText("admin override and does not start the release");
  const askNow = page.getByRole("region", { name: "Ask the Client now", exact: true });
  const ask = askNow.getByRole("button", { name: "Ask site-owner to review now" });
  await expect(ask).toBeDisabled();
  await askNow.getByRole("textbox").fill("Fixes a login security problem.");
  await ask.click();
  await expect(askNow.getByRole("status")).toContainText("Nothing has been deployed.");
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
  await expect(askNow).toHaveCount(0);
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

test("A failed release step shows the reason and its record and trace", async ({ page }, testInfo) => {
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", csrf_token: "fixture", identity: { login: "site-owner", github_id: 9001, role: "read_only", organizations: [], teams: [] } } }));
  await page.route("**/v1/release-review?*", async route => {
    const response = await page.evaluate(async () => {
      const modulePath = "/ui/src/dev-fixtures.ts";
      const fixtures = await import(modulePath);
      return fixtures.releaseReviewForFixture("products");
    });
    response.release_run = {
      decision_record_id: "decision-failed-release", rollback_drill: true, state: "stopped", blocked_reason: "",
      steps: [{step: "rollback-1", kind: "rollback", status: "fail", operation_id: "operation-rollback",
        failure: {code: "rollback_fail", reason: "Website sender verification failed.", record_id: "deployment-failed", trace_id: "client-release-trace", recorded_at: "2026-10-08T18:46:46Z"}}],
    };
    await route.fulfill({ json: response });
  });
  await page.goto("/ui/owner-review?product=example-site");
  const progress = page.getByRole("region", { name: "Release progress" });
  await expect(progress).toContainText("Website sender verification failed.");
  await expect(progress).toContainText("rollback_fail");
  await progress.getByText("Failure details", { exact: true }).click();
  for (const id of ["deployment-failed", "operation-rollback", "client-release-trace"]) {
    await expect(progress.locator("code", { hasText: id })).toBeVisible();
  }
  await progress.screenshot({path: testInfo.outputPath("failed-release-step.png")});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("An open review page follows the release it started", async ({ page }, testInfo) => {
  await page.goto("/ui/owner-review?product=example-site&fixture=products");
  await expect(page.getByRole("heading", { name: "Review this release" })).toBeVisible();
  const { review, accepted } = await page.evaluate(async () => {
    const modulePath = "/ui/src/dev-fixtures.ts";
    const fixtures = await import(modulePath);
    const review = fixtures.releaseReviewForFixture("products");
    return { review, accepted: fixtures.releaseDecisionForFixture(review, "accepted", "") };
  });
  type StepStatus = ClientReleaseRunView["steps"][number]["status"];
  const runWith = (state: ClientReleaseRunView["state"], backup: StepStatus, promote: StepStatus): ClientReleaseRunView => ({
    decision_record_id: "fixture-release-decision", rollback_drill: false, state, blocked_reason: "",
    steps: [
      { step: "backup-1", kind: "backup", status: backup, operation_id: "backup-operation", failure: null },
      { step: "promote-1", kind: "promote", status: promote, operation_id: "promote-operation", failure: null },
    ],
  });
  let current: object = review;
  let reads = 0;
  let failReads = false;
  const writes: string[] = [];
  await page.clock.install();
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", csrf_token: "fixture", identity: { login: "site-owner", github_id: 9001, role: "read_only", organizations: [], teams: [] } } }));
  await page.route("**/v1/release-review?*", route => {
    reads++;
    if (failReads) return route.fulfill({ status: 503, json: { status: "error", trace_id: "failed-read", error: { code: "read_unavailable", message: "Release read unavailable" } } });
    return route.fulfill({ json: current });
  });
  await page.route("**/v1/release-review/decisions", route => {
    writes.push(route.request().method());
    current = { ...accepted, release_run: runWith("waiting", "not_started", "not_started") };
    return route.fulfill({ json: current });
  });
  await page.goto("/ui/owner-review?product=example-site");
  await page.getByRole("button", { name: "Accept release" }).click();
  const progress = page.getByRole("region", { name: "Release progress" });
  await expect(progress.getByRole("heading")).toHaveText("Release progress: Starting");

  current = { ...accepted, release_run: runWith("running", "pass", "running") };
  await page.clock.runFor(RELEASE_PROGRESS_REFRESH_MS);
  await expect(progress.getByRole("heading")).toHaveText("Release progress: In progress");
  await expect(progress).toContainText("Verified backup: Done");
  await expect(progress).toContainText("Put this version live: Running");

  failReads = true;
  await page.clock.runFor(RELEASE_PROGRESS_REFRESH_MS);
  await expect(progress.getByRole("alert")).toContainText("Release progress could not be updated");
  await expect(progress).toContainText("Put this version live: Running");

  failReads = false;
  current = { ...accepted, release_run: runWith("passed", "pass", "pass") };
  await page.clock.runFor(RELEASE_PROGRESS_REFRESH_MS);
  await expect(progress.getByRole("heading")).toHaveText("Release progress: Live");
  await expect(progress.getByRole("alert")).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("release-progress-live.png"), fullPage: true });

  const settledReads = reads;
  await page.clock.runFor(3 * RELEASE_PROGRESS_REFRESH_MS);
  expect(reads).toBe(settledReads);
  expect(writes).toEqual(["POST"]);
});

test("A stopped release stops refreshing and a newer decision discards an older read", async ({ page }) => {
  await page.goto("/ui/owner-review?product=example-site&fixture=products");
  await expect(page.getByRole("heading", { name: "Review this release" })).toBeVisible();
  const { accepted, changesRequested } = await page.evaluate(async () => {
    const modulePath = "/ui/src/dev-fixtures.ts";
    const fixtures = await import(modulePath);
    const review = fixtures.releaseReviewForFixture("products");
    return {
      accepted: fixtures.releaseDecisionForFixture(review, "accepted", ""),
      changesRequested: fixtures.releaseDecisionForFixture(review, "changes_requested", "Keep the old label."),
    };
  });
  const run = (state: ClientReleaseRunView["state"], status: ClientReleaseRunView["steps"][number]["status"]): ClientReleaseRunView => ({
    decision_record_id: "fixture-release-decision", rollback_drill: false, state, blocked_reason: "",
    steps: [{ step: "promote-1", kind: "promote", status, operation_id: "promote-operation",
      failure: status === "fail" ? { code: "post_deploy_check_failed", reason: "The live site did not answer its checks.", record_id: "promotion-record", recorded_at: "2026-10-11T04:00:00Z", trace_id: "promotion-trace" } : null }],
  });
  const current = { ...accepted, release_run: run("running", "running") };
  let reads = 0;
  let holdRead: Promise<void> | undefined;
  let releaseHeldRead: (() => void) | undefined;
  await page.clock.install();
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", csrf_token: "fixture", identity: { login: "site-owner", github_id: 9001, role: "read_only", organizations: [], teams: [] } } }));
  await page.route("**/v1/release-review?*", async route => {
    reads++;
    if (holdRead) await holdRead;
    await route.fulfill({ json: current }).catch(() => undefined);
  });
  await page.route("**/v1/release-review/decisions", route => route.fulfill({ json: { ...changesRequested, release_run: run("stopped", "fail") } }));
  await page.goto("/ui/owner-review?product=example-site");
  const progress = page.getByRole("region", { name: "Release progress" });
  await expect(progress.getByRole("heading")).toHaveText("Release progress: In progress");

  holdRead = new Promise<void>(resolve => { releaseHeldRead = resolve; });
  const readsBeforeHold = reads;
  await page.clock.runFor(RELEASE_PROGRESS_REFRESH_MS);
  await expect.poll(() => reads).toBe(readsBeforeHold + 1);
  await page.getByRole("textbox").first().fill("Keep the old label.");
  await page.getByRole("button", { name: "Request changes" }).click();
  await expect(progress.getByRole("heading")).toHaveText("Release progress: Stopped");
  releaseHeldRead?.();
  holdRead = undefined;
  await page.clock.runFor(1000);
  await expect(progress.getByRole("heading")).toHaveText("Release progress: Stopped");
  await expect(progress).toContainText("The live site did not answer its checks.");

  const stoppedReads = reads;
  await page.clock.runFor(3 * RELEASE_PROGRESS_REFRESH_MS);
  expect(reads).toBe(stoppedReads);
});

test("Urgent invitation controls respect a live hotfix and an unchanged candidate", async ({ page }) => {
  await page.goto("/ui/owner-review?product=example-site&fixture=operator");
  await expect(page.getByRole("heading", { name: "Review this release", exact: true })).toBeVisible();
  const response = await page.evaluate(async () => {
    const fixtures = await import("/ui/src/dev-fixtures.ts");
    return fixtures.releaseReviewForFixture("operator");
  });
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", csrf_token: "fixture", identity: { login: "engineer", github_id: 9002, role: "admin", organizations: [], teams: [] } } }));
  await page.route("**/v1/release-review?*", route => route.fulfill({ json: response }));
  response.live_site_url = "https://live.example.invalid/";
  response.reviewing_temporary_hotfix = true;
  for (const director of [false, true]) {
    response.can_start_temporary_hotfix = director;
    await page.goto("/ui/owner-review?product=example-site");
    const role = page.locator(".owner-review-card > .owner-review-state").first();
    await expect(role).toContainText("urgent fix is already live");
    await expect(role).toContainText("Accept keeps it");
    await expect(role).toContainText("override is unavailable");
    await expect(page.getByRole("region", { name: "Admin Approval Override", exact: true })).toHaveCount(0);
    await expect(page.getByRole("region", { name: "Ask the Client now", exact: true })).toHaveCount(0);
    await expect(page.getByRole("link", { name: director ? "Open the testing site" : "Open the live site", exact: true })).toBeVisible();
    if (director) {
      await page.getByText("Technical details", { exact: true }).click();
      await expect(page.getByText("Proposed production version:", { exact: true })).toBeVisible();
      await expect(page.getByText("Live urgent fix:", { exact: true })).toHaveCount(0);
    }
  }
  response.reviewing_temporary_hotfix = false;
  await page.reload();
  await expect(page.locator(".owner-review-card > .owner-review-state").first()).toContainText("separate control below");
  response.review.checklist!.candidate = { ...response.review.checklist!.production };
  await page.reload();
  await expect(page.getByRole("region", { name: "Ask the Client now", exact: true })).toHaveCount(0);
  response.review.checklist!.candidate.shared_addons_digest = "shared-security-fix";
  await page.reload();
  await expect(page.getByRole("region", { name: "Ask the Client now", exact: true })).toBeVisible();
  response.review.checklist!.candidate = { ...response.review.checklist!.production, artifact_id: "rebuilt-security-image" };
  await page.reload();
  await expect(page.getByRole("region", { name: "Ask the Client now", exact: true })).toBeVisible();
});
