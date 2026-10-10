import { expect, test } from "@playwright/test";
import type { MergeTrainControllerStatusResponse } from "../../src/generated/openapi.ts";

test("landing progress uses total stored entries through refresh", async ({ page }, testInfo) => {
  await page.goto("/ui/engineering/merge-train?fixture=products");
  await expect(page.getByRole("heading", { name: "Merge train", exact: true })).toBeVisible();
  const fixtures = await page.evaluate(async () => {
    const path = "/ui/src/dev-fixtures.ts";
    const data = await import(path);
    return { identity: data.fixtureIdentity, targets: data.mergeTrainTargetsForFixture("products"),
      response: data.mergeTrainStatusForFixture("products", "example/control-plane", "main") as MergeTrainControllerStatusResponse };
  });
  const record = fixtures.response.controller_status.controller_records[0];
  Object.assign(record, { record_type: "batch_landing_plan", record_id: "completed-landing",
    status: "merged", policy_status: "current", stale_reason: "", pull_request_numbers: [7],
    merged_count: 1, planned_count: 0, blocked_count: 0, stale_count: 0, skipped_count: 0 });
  const mutations: string[] = [];
  const errors: string[] = [];
  page.on("request", request => { if (!["GET", "HEAD"].includes(request.method())) mutations.push(request.url()); });
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/v1/auth/session", route => route.fulfill({json: {status: "ok", identity: fixtures.identity, csrf_token: "fixture", trace_id: "counts-auth"}}));
  await page.route("**/v1/work-graph/merge-train/policy-targets", route => route.fulfill({json: fixtures.targets}));
  await page.route("**/v1/work-graph/merge-train/controller/status?**", route => route.fulfill({json: fixtures.response}));
  await page.goto("/ui/engineering/merge-train?repository=example%2Fcontrol-plane&base_branch=main");
  const row = page.locator(".engineering-controller-record").filter({has: page.getByText(record.record_id, {exact: true})});
  await expect(row).toContainText("1/1 merged");
  record.pull_request_numbers = [7, 8];
  record.merged_count = 2;
  await page.getByRole("button", {name: "Refresh status", exact: true}).click();
  await expect(row).toContainText("2/2 merged");
  await page.screenshot({path: testInfo.outputPath("completed-landing.png"), fullPage: true});
  Object.assign(record, {status: "blocked", pull_request_numbers: [7, 8, 9, 10, 11],
    merged_count: 1, planned_count: 1, blocked_count: 1, stale_count: 1, skipped_count: 1});
  await page.getByRole("button", {name: "Refresh status", exact: true}).click();
  await expect(row).toContainText("1/5 merged · 1 planned · 0 in progress · 1 blocked · 1 stale · 1 skipped");
  await page.screenshot({path: testInfo.outputPath("partial-landing.png"), fullPage: true});
  record.stale_count = 0;
  await page.getByRole("button", {name: "Refresh status", exact: true}).click();
  await expect(row).toContainText("1/5 merged · 1 planned · 1 in progress");
  Object.assign(record, {record_type: "stack_collapse_plan", status: "ready_for_train",
    pull_request_numbers: [7, 8, 9], merged_count: 2, planned_count: 0, blocked_count: 0, stale_count: 0, skipped_count: 0});
  await page.getByRole("button", {name: "Refresh status", exact: true}).click();
  await expect(row).toContainText("2 collapsed");
  await expect(row).not.toContainText("merged");
  record.record_type = "batch_candidate";
  record.status = "waiting";
  await page.getByRole("button", {name: "Refresh status", exact: true}).click();
  await expect(row).not.toContainText("merged");
  await expect(row).not.toContainText("collapsed");
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  expect(mutations).toEqual([]);
  expect(errors).toEqual([]);
});
