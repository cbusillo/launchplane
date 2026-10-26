import { expect, test, waitForState, type Page } from "./state";

async function setup(page: Page, outcome = "ok", admin = true) {
  const requests: { body: Record<string, any>; key: string }[] = [];
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/v1/auth/session", route => route.fulfill({ json: {
    status: "ok", trace_id: "session", csrf_token: "removal-csrf",
    identity: { provider: "github", login: "fixture", github_id: 42, name: "Admin", email: "fixture@example.invalid", organizations: [], teams: [], role: admin ? "admin" : "operator" },
  } }));
  await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: [] } }));
  await page.route("**/v1/admin/runtime-settings/removal", async route => {
    const body = route.request().postDataJSON();
    const key = route.request().headers()["idempotency-key"];
    expect(route.request().headers()["x-csrf-token"]).toBe("removal-csrf");
    requests.push({ body, key });
    const applies = requests.filter(request => request.body.mode === "apply");
    if (body.mode === "apply" && applies.length === 1 && outcome.startsWith("uncertain")) { await route.abort("failed"); return; }
    if (body.mode === "apply" && (outcome === "stale" || outcome === "uncertain-stale")) {
      await route.fulfill({ status: 409, json: { trace_id: "stale", error: { code: "runtime_setting_removal_unverified", message: "Refresh the exact review or read the original removal receipt." } } }); return;
    }
    const digest = "a".repeat(64);
    await route.fulfill({ json: { status: "ok", trace_id: body.mode, result: {
      product: body.product, scope: body.scope, context: body.context || "", instance: body.instance || "",
      removed_keys: outcome === "mismatch" ? ["UNREVIEWED_KEY"] : body.keys,
      surviving_keys: outcome === "retirement-only" ? [] : ["SURVIVING_KEY"],
      surviving_value_count: outcome === "retirement-only" ? 0 : 1,
      unprintable_survivor_count: 0, retirement_intent_count: 2, record_deleted: false,
      requires_compatible_readers_and_rollback: outcome === "retirement-only",
      plan_sha256: digest, review_id: `runtime-removal-${digest}`, applied: body.mode === "apply",
      ...(body.mode === "readback" ? { readback_matches: outcome !== "readback-mismatch" } : {}),
    } } });
  });
  return { requests, errors };
}

async function review(page: Page) {
  await page.getByLabel("Product", { exact: true }).fill("fixture-site");
  await page.getByLabel("Stored scope").selectOption("instance");
  await page.getByLabel("Context", { exact: true }).fill("fixture-context");
  await page.getByLabel("Instance", { exact: true }).fill("testing");
  await page.getByLabel("Setting names").fill("OBSOLETE_INPUT");
  await page.getByLabel("Removal reason").fill("Retire the qualified unused input.");
  await page.getByRole("button", { name: "Review removal", exact: true }).click();
}

async function confirm(page: Page) {
  await page.getByLabel(/I reviewed the exact keys/).check();
}

test("stored removal reviews exact scope, invalidates edits, applies and reads back", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const { requests, errors } = await setup(page);
  await page.goto("/ui/engineering/runtime-setting-removal");
  await expect(page.getByRole("heading", { name: "Remove stored settings", level: 1 })).toBeFocused();
  await expect(page.getByRole("button", { name: "Remove reviewed settings" })).toBeDisabled();
  await review(page);
  await confirm(page);
  await page.getByLabel("Removal reason").fill("Changed reason requires a fresh review.");
  await expect(page.getByRole("button", { name: "Remove reviewed settings" })).toBeDisabled();
  await page.getByRole("button", { name: "Review removal", exact: true }).click();
  await confirm(page);
  await page.screenshot({ path: testInfo.outputPath("runtime-removal-reviewed.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByRole("button", { name: "Remove reviewed settings" }).click();
  await expect(page.getByRole("status")).toContainText("Current stored record matches");
  const apply = requests.find(request => request.body.mode === "apply")!;
  expect(apply.body).toMatchObject({ product: "fixture-site", scope: "instance", context: "fixture-context", instance: "testing", keys: ["OBSOLETE_INPUT"], reason: "Changed reason requires a fresh review." });
  expect(apply.body.review_id).toBe(requests.findLast(request => request.body.mode === "readback")!.body.review_id);
  expect(apply.key).toBeTruthy();
  expect(errors).toEqual([]);
});

for (const outcome of ["uncertain", "uncertain-stale"]) {
  test(`original request and key survive reload after ${outcome}`, async ({ page }) => {
    const { requests } = await setup(page, outcome);
    await page.goto("/ui/engineering/runtime-setting-removal");
    await review(page); await confirm(page);
    await page.getByRole("button", { name: "Remove reviewed settings" }).click();
    await expect(page.getByRole("button", { name: "Retry original removal" })).toBeEnabled();
    await page.reload();
    await expect(page.getByLabel("Product", { exact: true })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Review removal", exact: true })).toBeDisabled();
    await page.getByRole("button", { name: "Retry original removal" }).click();
    if (outcome === "uncertain") await expect(page.getByRole("status")).toContainText("Current stored record matches");
    else {
      await waitForState(() => requests.filter(request => request.body.mode === "apply").length).toBe(2);
      await expect(page.getByLabel("Product", { exact: true })).toBeDisabled();
    }
    const applies = requests.filter(request => request.body.mode === "apply");
    expect(applies).toHaveLength(2);
    expect(applies[1]).toEqual(applies[0]);
  });
}

test("stale review, mismatched response and absent admin authority cannot proceed", async ({ page }) => {
  const { requests } = await setup(page, "stale");
  await page.goto("/ui/engineering/runtime-setting-removal");
  await review(page); await confirm(page);
  await page.getByRole("button", { name: "Remove reviewed settings" }).click();
  await expect(page.getByRole("button", { name: "Review removal", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Remove reviewed settings" })).toBeDisabled();
  await setup(page, "mismatch");
  await page.reload();
  await review(page);
  await expect(page.getByRole("button", { name: "Remove reviewed settings" })).toBeDisabled();
  await expect(page.getByRole("checkbox")).toHaveCount(0);
  await setup(page, "ok", false);
  await page.reload();
  await expect(page.getByRole("button", { name: "Review removal", exact: true })).toHaveCount(0);
  expect(requests.filter(request => request.body.mode === "apply")).toHaveLength(1);
});

test("readback mismatch stays visible and locked across reload", async ({ page }) => {
  await setup(page, "readback-mismatch");
  await page.goto("/ui/engineering/runtime-setting-removal");
  await review(page); await confirm(page);
  await page.getByRole("button", { name: "Remove reviewed settings" }).click();
  await expect(page.getByRole("status")).toContainText("Current readback is unverified");
  await page.reload();
  await expect(page.getByRole("button", { name: "Review removal", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "Check current record" }).click();
  await expect(page.getByRole("status")).toContainText("Current readback is unverified");
});

test("retirement-only record requires explicit reader and rollback qualification", async ({ page }) => {
  await setup(page, "retirement-only");
  await page.goto("/ui/engineering/runtime-setting-removal");
  await review(page);
  await expect(page.getByRole("alert")).toContainText("compatible readers to every consumer");
  await expect(page.getByRole("button", { name: "Remove reviewed settings" })).toBeDisabled();
});
