import { expect, test } from "@playwright/test";
import type {
  GitHubHumanIdentityResponse,
  MergeTrainControllerStatusResponse,
  MergeTrainPolicyTargetsResponse,
} from "../../src/generated/openapi.ts";

test("policy history stays visible without blocking current controller status", async ({ page }, testInfo) => {
  await page.goto("/ui/engineering/merge-train?fixture=products");
  await expect(page.getByRole("heading", { name: "Merge train", exact: true })).toBeVisible();
  // Reuse the browser-owned fixtures in their browser context; they read window.
  const fixtureData = await page.evaluate(async () => {
    const path = "/ui/src/dev-fixtures.ts";
    const fixtures = await import(path);
    return {
      identity: fixtures.fixtureIdentity as GitHubHumanIdentityResponse,
      targets: fixtures.mergeTrainTargetsForFixture("products") as MergeTrainPolicyTargetsResponse,
      response: fixtures.mergeTrainStatusForFixture("products", "example/control-plane", "main") as MergeTrainControllerStatusResponse,
    };
  });
  const response = fixtureData.response;
  const status = response.controller_status;
  const original = status.controller_records[0];
  const history = {
    ...original,
    record_id: "historical-candidate",
    status: "passed",
    historical: true,
    policy_status: "stale" as const,
    policy_sha256: "old-policy",
    stale_reason: "Recorded under an earlier policy revision.",
  };
  status.controller_records = [history];
  status.admission.controller_action = "idle";
  status.admission.controller_candidate_record_id = "";
  status.admission.controller_reason = "No current controller work.";
  status.admission.detail = "No current controller work.";
  status.controller_diagnostics = null;
  status.latest_run = null;
  status.latest_dry_run = null;
  const state = status.controller_state!;
  state.status = "idle";
  state.active_record_id = "";
  state.active_phase = "";
  state.active_action = "";
  state.reconciliation_status = "clean";

  const mutations: string[] = [];
  const errors: string[] = [];
  page.on("request", (request) => {
    if (!["GET", "HEAD"].includes(request.method())) mutations.push(request.url());
  });
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/v1/auth/session", (route) => route.fulfill({
    json: { status: "ok", identity: fixtureData.identity, csrf_token: "fixture", trace_id: "test-auth" },
  }));
  await page.route("**/v1/work-graph/merge-train/policy-targets", (route) => route.fulfill({
    json: fixtureData.targets,
  }));
  await page.route("**/v1/work-graph/merge-train/controller/status?**", (route) => route.fulfill({ json: response }));

  await page.goto("/ui/engineering/merge-train?repository=example%2Fcontrol-plane&base_branch=main");
  await expect(page.getByRole("heading", { name: "Merge train", exact: true })).toBeVisible();
  const controller = page.locator(".engineering-metric").filter({
    has: page.getByText("Controller", { exact: true }),
  });
  await expect(controller).toHaveAttribute("data-tone", "pass");
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(page.getByText("historical policy", { exact: true })).toBeVisible();
  await expect(page.getByText(history.record_id, { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("historical-controller.png"), fullPage: true });

  // Policy filtering can advertise idle before unfinished old work is retired.
  history.historical = false;
  history.status = "planned";
  await page.getByRole("button", { name: "Refresh status", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(controller).toHaveAttribute("data-tone", "blocked");
  await expect(page.getByText("historical policy", { exact: true })).toHaveCount(0);
  history.historical = true;
  history.status = "passed";

  state.reconciliation_status = "adopted";
  await page.getByRole("button", { name: "Refresh status", exact: true }).click();
  await expect(controller).toHaveAttribute("data-tone", "pending");
  await expect(page.locator(".engineering-status-chip").getByText("Adopted", { exact: true })).toBeVisible();
  state.reconciliation_status = "clean";

  // A currently referenced stale record still requires attention, even if its
  // stored status looks terminal. The service's reference owns applicability.
  state.status = "running";
  state.active_record_id = history.record_id;
  await page.getByRole("button", { name: "Refresh status", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(controller).toHaveAttribute("data-tone", "blocked");
  await expect(page.getByText("historical policy", { exact: true })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("active-stale-controller.png"), fullPage: true });

  state.active_record_id = "";
  state.status = "reconcile_required";
  state.reconciliation_status = "required";
  state.reconciliation_detail = "A current provider outcome requires reconciliation.";
  await page.getByRole("button", { name: "Refresh status", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(state.reconciliation_detail);
  await expect(page.getByText(history.record_id, { exact: true })).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(overflow).toBe(false);
  expect(mutations).toEqual([]);
  expect(errors).toEqual([]);
});
