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
    const applies = requests.filter(request => request.body.mode === "apply" && request.body.context === body.context);
    if (body.mode === "apply" && body.context !== "second-context" && applies.length === 1 && outcome.startsWith("uncertain")) { await route.abort("failed"); return; }
    if (body.mode === "apply" && body.context !== "second-context" && (outcome === "stale" || outcome === "uncertain-stale" || outcome.startsWith("uncertain-pending") || outcome === "uncertain-denied")) {
      await route.fulfill({ status: outcome === "uncertain-denied" ? 403 : 409, json: { trace_id: "stale", error: { code: outcome === "uncertain-denied" ? "authorization_denied" : outcome.startsWith("uncertain-pending") ? "runtime_setting_removal_unverified" : "runtime_setting_removal_stale", message: "Refresh the exact review or read the original removal receipt." } } }); return;
    }
    const digest = "a".repeat(64);
    await route.fulfill({ json: { status: "ok", trace_id: `${body.mode}-${requests.length}`, result: {
      product: body.product, scope: body.scope, context: body.context || "", instance: body.instance || "",
      removed_keys: outcome === "mismatch" || (outcome === "malformed-apply" && body.mode === "apply" && applies.length === 1) ? ["UNREVIEWED_KEY"] : body.keys,
      surviving_keys: outcome === "retirement-only" ? [] : ["SURVIVING_KEY"],
      surviving_value_count: outcome === "retirement-only" ? 0 : 1,
      unprintable_survivor_count: 0, retirement_intent_count: 2, record_deleted: false,
      requires_compatible_readers_and_rollback: outcome === "retirement-only",
      instance_plain_setting_effects: body.keys.map((key: string) => ({ key, source_after: "context", effect: "changed" })),
      plan_sha256: digest, review_id: `runtime-removal-${digest}`, applied: body.mode === "apply",
      ...(body.mode === "readback" ? { readback_matches: !outcome.endsWith("mismatch") } : {}),
    } } });
  });
  return { requests, errors };
}

async function selectRecord(page: Page, context = "fixture-context", product = "fixture-site") {
  await page.getByLabel("Product", { exact: true }).fill(product);
  await page.getByLabel("Stored scope").selectOption("instance");
  await page.getByLabel("Context", { exact: true }).fill(context);
  await page.getByLabel("Instance", { exact: true }).fill("testing");
  await page.getByRole("button", { name: "Select record", exact: true }).click();
}

async function review(page: Page, context = "fixture-context") {
  await selectRecord(page, context);
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
  await expect(page.getByRole("button", { name: "Remove reviewed settings" })).toHaveCount(0);
  await review(page);
  await expect(page.getByLabel("Context fallback comparison")).toContainText("OBSOLETE_INPUT");
  await confirm(page);
  await page.getByLabel("Removal reason").fill("Changed reason requires a fresh review.");
  await expect(page.getByRole("button", { name: "Remove reviewed settings" })).toBeDisabled();
  await page.getByRole("button", { name: "Review removal", exact: true }).click();
  await confirm(page);
  await page.screenshot({ path: testInfo.outputPath("runtime-removal-reviewed.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByRole("button", { name: "Remove reviewed settings" }).click();
  await expect(page.getByRole("status")).toContainText("Current stored record matches");
  const previousCheck = await page.getByLabel("Current record check reference").textContent();
  let finishReadback!: () => void;
  const pendingReadback = new Promise<void>(resolve => { finishReadback = resolve; });
  await page.route("**/v1/admin/runtime-settings/removal", async route => {
    await pendingReadback;
    await route.fallback();
  });
  const freshResponse = page.waitForResponse(response => response.url().endsWith("/v1/admin/runtime-settings/removal") && response.request().postDataJSON().mode === "readback");
  await page.getByRole("button", { name: "Check current record" }).click();
  try {
    await expect(page.getByRole("button", { name: "Check current record" })).toBeDisabled();
    await expect(page.getByLabel("Current record check reference")).toHaveCount(0);
    await expect(page.getByText("Current stored record matches", { exact: false })).toHaveCount(0);
  } finally { finishReadback(); }
  await waitForState(() => requests.filter(request => request.body.mode === "readback").length).toBe(2);
  await expect(page.getByRole("status")).toContainText("Current stored record matches");
  const freshTrace = (await (await freshResponse).json()).trace_id;
  expect(freshTrace).not.toBe(previousCheck);
  await expect(page.getByLabel("Current record check reference")).toHaveText(freshTrace);
  expect(requests.filter(request => request.body.mode === "apply")).toHaveLength(1);
  const apply = requests.find(request => request.body.mode === "apply")!;
  expect(apply.body).toMatchObject({ product: "fixture-site", scope: "instance", context: "fixture-context", instance: "testing", keys: ["OBSOLETE_INPUT"], reason: "Changed reason requires a fresh review." });
  expect(apply.body.review_id).toBe(requests.findLast(request => request.body.mode === "readback")!.body.review_id);
  expect(apply.key).toBeTruthy();
  expect(errors).toEqual([]);
  await page.route("**/v1/admin/runtime-settings/removal", route => route.fulfill({ status: 503, json: {
    trace_id: "readback-unavailable", error: { code: "service_unavailable", message: "Current record cannot be checked." },
  } }));
  await page.getByRole("button", { name: "Check current record" }).click();
  await expect(page.getByText("Current record cannot be checked.", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Current record check reference")).toHaveCount(0);
  await expect(page.getByText("Current stored record matches", { exact: false })).toHaveCount(0);
});

for (const outcome of ["uncertain", "uncertain-stale", "uncertain-pending"]) {
  test(`original request and key survive reload after ${outcome}`, async ({ page }) => {
    const { requests } = await setup(page, outcome);
    await page.goto("/ui/engineering/runtime-setting-removal");
    await review(page); await confirm(page);
    await page.getByRole("button", { name: "Remove reviewed settings" }).click();
    await expect(page.getByRole("button", { name: "Retry original removal" })).toBeEnabled();
    await page.reload();
    await expect(page.getByRole("textbox", { name: "Setting names", exact: true })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Review removal", exact: true })).toBeDisabled();
    await page.getByRole("button", { name: "Retry original removal" }).click();
    if (outcome === "uncertain") await expect(page.getByRole("status")).toContainText("Current stored record matches");
    else {
      await waitForState(() => requests.filter(request => request.body.mode === "apply").length).toBe(2);
      if (outcome === "uncertain-stale") {
        await expect(page.getByRole("textbox", { name: "Setting names", exact: true })).toBeEnabled();
        await expect(page.getByRole("button", { name: "Review removal", exact: true })).toBeEnabled();
      } else {
        await expect(page.getByRole("textbox", { name: "Setting names", exact: true })).toBeDisabled();
        await page.getByRole("button", { name: "Check current record" }).click();
        await expect(page.getByRole("textbox", { name: "Setting names", exact: true })).toBeDisabled();
      }
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
  const mismatch = await setup(page, "mismatch");
  await page.reload();
  await review(page);
  await expect(page.getByRole("button", { name: "Remove reviewed settings" })).toBeDisabled();
  await expect(page.getByText(/The removal response does not match the exact request/)).toBeVisible();
  await expect(page.getByRole("checkbox")).toHaveCount(0);
  const nonadmin = await setup(page, "ok", false);
  await page.reload();
  await expect(page.getByRole("alert")).toContainText("Sign in as an administrator");
  expect(requests.filter(request => request.body.mode === "apply")).toHaveLength(1);
  expect(mismatch.requests.filter(request => request.body.mode === "apply")).toHaveLength(0);
  expect(nonadmin.requests).toHaveLength(0);
});

test("committed removal with later drift can review current state", async ({ page }) => {
  await setup(page, "readback-mismatch");
  await page.goto("/ui/engineering/runtime-setting-removal");
  await review(page); await confirm(page);
  await page.getByRole("button", { name: "Remove reviewed settings" }).click();
  await expect(page.getByRole("status")).toContainText("Current stored record does not match");
  await expect(page.getByRole("button", { name: "Review removal", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Check current record" }).click();
  await expect(page.getByRole("status")).toContainText("Current stored record does not match");
  await page.getByRole("button", { name: "Review removal", exact: true }).click();
  await expect(page.getByRole("checkbox")).toHaveCount(1);
});

test("retirement-only review discloses reader and rollback qualification before confirmation", async ({ page }) => {
  await setup(page, "retirement-only");
  await page.goto("/ui/engineering/runtime-setting-removal");
  await review(page);
  await expect(page.getByRole("alert")).toContainText("compatible readers to every consumer");
});


test("unavailable recovery storage explains the block without sending removal", async ({ page }) => {
  const { requests } = await setup(page);
  await page.addInitScript(() => Object.defineProperty(window, "sessionStorage", { get() { throw new DOMException("Unavailable", "SecurityError"); } }));
  await page.goto("/ui/engineering/runtime-setting-removal");
  await selectRecord(page);
  await expect(page.getByRole("alert")).toContainText("Recovery storage is unavailable");
  expect(requests).toHaveLength(0);
});


for (const outcome of ["uncertain-pending", "uncertain-denied", "uncertain-pending-mismatch"]) {
  test(`uncertain record remains guarded while other records work: ${outcome}`, async ({ page }) => {
    const { requests } = await setup(page, outcome);
    await page.goto("/ui/engineering/runtime-setting-removal");
    await review(page); await confirm(page);
    await page.getByRole("button", { name: "Remove reviewed settings" }).click();
    await expect(page.getByRole("button", { name: "Retry original removal" })).toBeEnabled();
    await page.getByRole("button", { name: "Check current record" }).click();
    await expect(page.getByText(/This does not confirm the original Apply/)).toBeVisible();
    await expect(page.getByText(outcome.endsWith("mismatch") ? /Current record does not match/ : /Current record matches/)).toBeVisible();
    await expect(page.getByText("Removal committed.", { exact: false })).toHaveCount(0);
    await review(page, "second-context"); await confirm(page);
    await page.getByRole("button", { name: "Remove reviewed settings" }).click();
    await expect(page.getByRole("status")).toContainText("Removal committed");
    await selectRecord(page, "fixture-context", "new-product-selection");
    await expect(page.getByLabel("Setting names")).toBeDisabled();
    await expect(page.getByRole("button", { name: "Review removal", exact: true })).toBeDisabled();
    await page.reload();
    await page.getByRole("button", { name: "Retry original removal" }).click();
    await waitForState(() => requests.filter(request => request.body.mode === "apply" && request.body.context === "fixture-context").length).toBe(2);
    const attempts = requests.filter(request => request.body.mode === "apply" && request.body.context === "fixture-context");
    expect(attempts[1]).toEqual(attempts[0]);
    expect(requests.find(request => request.body.mode === "apply" && request.body.context === "second-context")!.key).not.toBe(attempts[0].key);
    await expect(page.getByLabel("Setting names")).toBeDisabled();
  });
}

test("malformed successful apply remains uncertain and resolves through a same-key receipt", async ({ page }) => {
  const { requests } = await setup(page, "malformed-apply");
  await page.goto("/ui/engineering/runtime-setting-removal");
  await review(page); await confirm(page);
  await page.getByRole("button", { name: "Remove reviewed settings" }).click();
  await expect(page.getByRole("button", { name: "Retry original removal" })).toBeEnabled();
  await page.getByRole("button", { name: "Retry original removal" }).click();
  await expect(page.getByRole("status")).toContainText("Removal committed");
  const attempts = requests.filter(request => request.body.mode === "apply");
  expect(attempts[1]).toEqual(attempts[0]);
});
