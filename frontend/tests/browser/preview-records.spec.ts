import { expect, test, type Page } from "@playwright/test";
import type { PreviewGenerationRecord, PreviewRecord, ProductReconcileRequestView } from "../../src/generated/openapi.ts";

async function setup(page: Page) {
  await page.goto("/ui/products?fixture=products");
  const fixtures = await page.evaluate(async () => {
    const modulePath = "/ui/src/dev-fixtures.ts";
    const module = await import(modulePath) as typeof import("../../src/dev-fixtures");
    return { product: module.productsForFixture("products")[0], identity: module.fixtureIdentity };
  });
  const product = fixtures.product;
  const timestamp = new Date().toISOString();
  const preview = (number: number, state: PreviewRecord["state"]): PreviewRecord => ({
    schema_version: 1, preview_id: `preview-pr-${number}`, context: product.preview.context,
    anchor_repo: product.repository.split("/").at(-1)!, anchor_pr_number: number,
    anchor_pr_url: `https://github.com/example/site/pull/${number}`, preview_label: `pr-${number}`,
    canonical_url: `https://pr-${number}.example.invalid`, state, created_at: timestamp,
    updated_at: timestamp, eligible_at: timestamp, paused_at: "", destroy_after: "", destroyed_at: "",
    destroy_reason: "", active_generation_id: "", serving_generation_id: "", latest_generation_id: "",
    latest_manifest_fingerprint: "",
  });
  const previews = [preview(28, "pending"), preview(45, "active"), preview(149, "teardown_pending")];
  const generation = (record: PreviewRecord, sequence: number, state: PreviewGenerationRecord["state"]): PreviewGenerationRecord => ({
    schema_version: 1, generation_id: `${record.preview_id}-gen-${sequence}`, preview_id: record.preview_id,
    sequence, state, requested_reason: "test", requested_at: timestamp, started_at: timestamp,
    ready_at: state === "ready" ? timestamp : "", finished_at: timestamp, superseded_at: "", failed_at: "", expires_at: "",
    resolved_manifest_fingerprint: "fixture-manifest", artifact_id: `build-${sequence}`, baseline_release_tuple_id: "",
    source_map: [], anchor_summary: { repo: record.anchor_repo, pr_number: record.anchor_pr_number,
      head_sha: (sequence === 1 ? "a" : "b").repeat(40), pr_url: record.anchor_pr_url }, companion_summaries: [],
    deploy_status: "pass", verify_status: state === "ready" ? "pass" : "fail",
    overall_health_status: state === "ready" ? "pass" : "fail", failure_stage: "",
    failure_summary: "RAW PROVIDER LOG MUST NOT BE DISPLAYED", runtime_identity: null,
  });
  const serving = generation(previews[1], 1, "ready");
  const latest = generation(previews[1], 2, "failed");
  latest.runtime_identity = { schema_version: 1, product: product.product, context: product.preview.context,
    instance: "pr-45", environment_kind: "preview", deployment_record_id: "recorded-deploy",
    artifact_id: latest.artifact_id, source_git_ref: latest.anchor_summary.head_sha, image_reference: "",
    release_tuple_id: "", preview_id: latest.preview_id, preview_generation_id: latest.generation_id, deployed_at: timestamp };
  previews[1].serving_generation_id = serving.generation_id;
  previews[1].latest_generation_id = latest.generation_id;
  const requests: ProductReconcileRequestView[] = [28, 149].map(number => ({
    target_key: `${product.product}:preview:${number}`, target_kind: "preview", pull_request_number: number,
    state: number === 28 ? "done" : "failed", requested_at: timestamp, updated_at: timestamp,
    request_count: 1, attempt: 7, last_delivery_id: "", last_error: number === 149 ? "Matching target evidence is required." : "",
    last_plan: { action: "destroy", reason: number === 28 ? "preview_destroy_retry_limit" : "pull_request_not_open",
      held: number === 28, preview_result_status: "blocked" },
  }));
  Object.assign(product.preview, { records_status: "available", records_truncated: false, trust_state: "missing",
    active_count: previews.length, records: previews.map(record => ({ preview_id: record.preview_id,
      change_number: record.anchor_pr_number, change_url: record.anchor_pr_url,
      recorded_state: record.state, updated_at: record.updated_at })) });
  let mode: "ready" | "fail" | "deny" = "ready";
  let delayFirst = false;
  let release: (() => void) | undefined;
  const mutations: string[] = [];
  const pageErrors: string[] = [];
  page.on("pageerror", error => pageErrors.push(error.message));
  page.on("request", request => {
    if (request.url().includes("/v1/") && request.method() !== "GET") mutations.push(request.url());
  });
  await page.route("**/v1/**", route => route.fulfill({ status: 403, json: { status: "error",
    error: { code: "authorization_denied", message: "Fixture read unavailable" }, trace_id: "unused-read" } }));
  await page.route("**/v1/auth/session", route => route.fulfill({ json: {
    status: "ok", trace_id: "session", csrf_token: "csrf", identity: fixtures.identity,
  } }));
  await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: [product] } }));
  await page.route(`**/v1/products/${product.product}`, route => route.fulfill({ json: { status: "ok", trace_id: "product", product } }));
  await page.route(`**/v1/product-profiles/${product.product}/reconcile-requests`, route => route.fulfill({ json: {
    status: "ok", trace_id: "reconcile", product: product.product, requests,
  } }));
  await page.route("**/v1/previews/*/history", async route => {
    const record = previews.find(value => route.request().url().includes(`/${value.preview_id}/`))!;
    if (delayFirst && record.anchor_pr_number === 28) await new Promise<void>(resolve => { release = resolve; });
    if (mode !== "ready") return route.fulfill({ status: mode === "deny" ? 403 : 503, json: {
      status: "error", error: { code: mode === "deny" ? "authorization_denied" : "read_unavailable", message: "History read unavailable" }, trace_id: "history-failure",
    } });
    await route.fulfill({ json: { status: "ok", trace_id: "history", preview: record,
      generations: record.anchor_pr_number === 45 ? [latest, serving] : [] } }).catch(() => {});
  });
  return { product, latest, mutations, pageErrors, mode: (value: typeof mode) => { mode = value; },
    delay: () => { delayFirst = true; }, release: () => release?.(), delayed: () => Boolean(release) };
}

test("recorded previews show pending, held cleanup and distinct serving/latest proof", async ({ page }, testInfo) => {
  const fixture = await setup(page);
  await page.goto(`/ui/products/${fixture.product.product}`);
  const inventory = page.getByRole("region", { name: "Inspect individual previews" });
  await expect(inventory.getByRole("button", { name: /^Change #28/ })).toBeVisible();
  await inventory.getByRole("button", { name: /^Change #28/ }).click();
  await expect(inventory).toContainText("Retries held");
  await expect(inventory).toContainText("preview_destroy_retry_limit");
  await expect(inventory).toContainText("Generation evidence is missing");
  await expect(inventory).toContainText("Provider presence unknown");
  await inventory.getByRole("button", { name: /^Change #45/ }).click();
  const latest = inventory.getByRole("region", { name: "Latest recorded generation" });
  const serving = inventory.getByRole("region", { name: "Recorded serving generation" });
  await expect(latest).toContainText("2 · failed");
  await expect(latest).toContainText(fixture.latest.anchor_summary.head_sha);
  await expect(latest).toContainText("A declaration is not observed runtime identity verification");
  await expect(serving).toContainText("1 · ready");
  await expect(serving).not.toContainText(fixture.latest.anchor_summary.head_sha);
  await expect(inventory).not.toContainText("RAW PROVIDER LOG");
  await latest.scrollIntoViewIfNeeded();
  await page.screenshot({ path: `../tmp/browser-smoke/preview-records-generations-${testInfo.project.name}.png` });
  fixture.mode("fail");
  await inventory.getByRole("button", { name: "Refresh preview evidence" }).click();
  await expect(inventory).toContainText("Showing the last recorded response");
  await expect(latest).toContainText("2 · failed");
  fixture.mode("ready");
  fixture.latest.state = "ready";
  fixture.latest.overall_health_status = "pass";
  fixture.latest.verify_status = "pass";
  await inventory.getByRole("button", { name: "Refresh preview evidence" }).click();
  await expect(latest).toContainText("2 · ready");
  await inventory.getByRole("button", { name: /^Change #149/ }).click();
  await expect(inventory).toContainText("Cleanup reconciliation");
  await expect(inventory).toContainText("Recorded outcome: blocked");
  await expect(inventory).toContainText("Matching target evidence is required");
  await expect(inventory.getByRole("link", { name: "Inspect lifecycle activity" })).toHaveAttribute("href", `/ui/products/${fixture.product.product}/activity`);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByRole("heading", { name: "Inspect individual previews" }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: `../tmp/browser-smoke/preview-records-blocked-${testInfo.project.name}.png`, fullPage: true });
  expect(fixture.mutations).toEqual([]);
  expect(fixture.pageErrors).toEqual([]);
});

test("history denial removes previously cached details without widening authority", async ({ page }) => {
  const fixture = await setup(page);
  await page.goto(`/ui/products/${fixture.product.product}`);
  await page.getByRole("button", { name: /^Change #45/ }).click();
  await expect(page.getByRole("region", { name: "Latest recorded generation" })).toContainText("2 · failed");
  fixture.mode("deny");
  await page.getByRole("button", { name: "Refresh preview evidence" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Read denied: preview.read" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Latest recorded generation" })).toHaveCount(0);
  expect(fixture.mutations).toEqual([]);
  expect(fixture.pageErrors).toEqual([]);
});

test("held cleanup remains visible while history is delayed or unavailable", async ({ page }) => {
  const fixture = await setup(page);
  fixture.delay();
  await page.goto(`/ui/products/${fixture.product.product}`);
  await page.getByRole("button", { name: /^Change #28/ }).click();
  await expect.poll(fixture.delayed).toBe(true);
  const evidence = page.locator("#selected-preview-evidence");
  try {
    await expect(evidence).toContainText("Retries held");
  } finally {
    fixture.mode("fail");
    fixture.release();
  }
  await expect(evidence).toContainText("History read unavailable");
  await expect(evidence).toContainText("Retries held");
  await expect(evidence).not.toContainText("No matching reconciliation was returned");
  expect(fixture.mutations).toEqual([]);
  expect(fixture.pageErrors).toEqual([]);
});

test("a late history response cannot replace a newly selected preview", async ({ page }) => {
  const fixture = await setup(page);
  fixture.delay();
  await page.goto(`/ui/products/${fixture.product.product}`);
  await page.getByRole("button", { name: /^Change #28/ }).click();
  await expect.poll(fixture.delayed).toBe(true);
  await page.getByRole("button", { name: /^Change #45/ }).click();
  fixture.release();
  await expect(page.getByRole("region", { name: "Latest recorded generation" })).toContainText("2 · failed");
  await expect(page.locator("#selected-preview-evidence")).toContainText("preview-pr-45");
  await expect(page.locator("#selected-preview-evidence")).not.toContainText("preview-pr-28");
  expect(fixture.mutations).toEqual([]);
  expect(fixture.pageErrors).toEqual([]);
});

for (const status of ["authorization_denied", "unsupported", "available"] as const) {
  test(`individual inventory ${status} never invents provider absence`, async ({ page }) => {
    const fixture = await setup(page);
    Object.assign(fixture.product.preview, { records_status: status, records: [], records_truncated: false });
    await page.goto(`/ui/products/${fixture.product.product}`);
    const inventory = page.getByRole("region", { name: "Inspect individual previews" });
    await expect(inventory).toContainText(status === "authorization_denied" ? "existing preview.read grant"
      : status === "unsupported" ? "unavailable in this response" : "not verified provider absence");
    await expect(inventory.getByRole("button")).toHaveCount(0);
    expect(fixture.mutations).toEqual([]);
    expect(fixture.pageErrors).toEqual([]);
  });
}
