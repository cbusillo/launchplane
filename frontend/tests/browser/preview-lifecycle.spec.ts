import { expect, test, type Page } from "./state";

async function setup(page: Page, dropped: boolean | "denied" = false, role = "admin") {
  const requests: { body: Record<string, unknown>; key: string }[] = [];
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/v1/auth/session", route => route.fulfill({ json: {
    status: "ok", trace_id: "session", csrf_token: "csrf-fixture",
    identity: { provider: "github", login: "admin-fixture", github_id: 42, name: "Admin",
      email: "fixture@example.invalid", organizations: [], teams: [], role },
  } }));
  await page.route("**/v1/previews/lifecycle-sweep", async route => {
    expect(route.request().headers()["x-csrf-token"]).toBe("csrf-fixture");
    const body = route.request().postDataJSON();
    requests.push({ body, key: route.request().headers()["idempotency-key"] });
    if (dropped === "denied") {
      await route.fulfill({ status: 403, json: { error: "authorization_denied", message: "Fixture denial", trace_id: "denied-sweep" } });
      return;
    }
    if (dropped && requests.length === 1) { await route.abort("failed"); return; }
    await route.fulfill({ status: 202, json: {
      status: "accepted", trace_id: "sweep-trace", result: {
        status: "fail", apply: body.apply, profile_count: 2,
        profiles: [
          { product: "fixture-shop", context: "shop-preview", status: "blocked",
            error_message: "Held previews require original-intent recovery before cleanup apply.",
            held_reservations: [{ preview_id: "held-preview" }] },
          { product: "fixture-film", context: "film-preview", status: "report_only",
            orphaned_slugs: ["pr-9"], destroyed_slugs: [], failed_slugs: [], blocked_slugs: [], held_reservations: [],
            preview_lifecycle_plan_id: "film-plan", preview_lifecycle_cleanup_id: "film-cleanup" },
        ],
      },
    } });
  });
  return { requests, errors };
}

test("native portfolio report and explicit cleanup show held outcomes", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const fixture = await setup(page);
  await page.goto("/ui/engineering/preview-lifecycle");
  await expect(page.getByRole("heading", { name: "Preview cleanup", level: 1 })).toBeFocused();
  const panel = page.getByRole("region", { name: "Preview cleanup controls" });
  await expect(panel.getByRole("button", { name: "Apply cleanup", exact: true })).toBeDisabled();
  await page.screenshot({ path: testInfo.outputPath("preview-lifecycle-initial.png") });
  await panel.getByRole("button", { name: "Report", exact: true }).click();
  await expect(page.getByRole("region", { name: "Preview sweep result" }).getByRole("heading", { level: 2 })).toContainText("fail");
  await expect(panel.getByText("Held previews require original-intent recovery before cleanup apply.")).toBeVisible();
  await expect(panel.getByRole("button", { name: "Apply cleanup", exact: true })).toBeDisabled();
  await panel.getByRole("checkbox").check();
  await panel.getByRole("button", { name: "Apply cleanup", exact: true }).click();
  await expect(page.getByRole("region", { name: "Preview sweep result" }).getByRole("heading", { level: 2 })).toContainText("fail");
  await expect(panel.getByText("sweep-trace", { exact: true })).toBeVisible();
  expect(fixture.requests.map(request => request.body.apply)).toEqual([false, true]);
  expect(fixture.requests.every(request => !request.body.product && Boolean(request.key))).toBe(true);
  expect(fixture.requests[0].key).not.toBe(fixture.requests[1].key);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(await page.locator("[id]").evaluateAll(elements => new Set(elements.map(element => element.id)).size === elements.length)).toBe(true);
  expect(fixture.errors).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("preview-lifecycle-blocked.png"), fullPage: true });
});

for (const apply of [false, true]) {
  test(`dropped ${apply ? "cleanup" : "report"} response retains its request and key after reload`, async ({ page }) => {
    const fixture = await setup(page, true);
    await page.goto("/ui/engineering/preview-lifecycle");
    const panel = page.getByRole("region", { name: "Preview cleanup controls" });
    if (apply) await panel.getByRole("checkbox").check();
    await panel.getByRole("button", { name: apply ? "Apply cleanup" : "Report", exact: true }).click();
    await expect(panel.getByRole("button", { name: /Retry original (report|cleanup)/ })).toBeEnabled();
    await expect(panel.getByRole("button", { name: "Report", exact: true })).toHaveCount(0);
    await expect(panel.getByRole("button", { name: "Apply cleanup", exact: true })).toHaveCount(0);
    await page.evaluate(operationKey => {
      for (const key of Object.keys(sessionStorage)) {
        const state = JSON.parse(sessionStorage.getItem(key) || "null");
        if (state?.identity?.idempotencyKey !== operationKey) sessionStorage.removeItem(key);
      }
    }, fixture.requests[0].key);
    await page.reload();
    await panel.getByRole("button", { name: /Retry original (report|cleanup)/ }).click();
    await expect(panel.getByRole("region", { name: "Preview sweep result" }).getByRole("heading", { level: 2 })).toContainText("fail");
    expect(fixture.requests).toHaveLength(2);
    expect(fixture.requests[1]).toEqual(fixture.requests[0]);
    expect(fixture.errors).toEqual([]);
  });
}

test("an unrecognized retained fingerprint cannot dispatch a different sweep", async ({ page }) => {
  const fixture = await setup(page, true);
  await page.goto("/ui/engineering/preview-lifecycle");
  const panel = page.getByRole("region", { name: "Preview cleanup controls" });
  await panel.getByRole("button", { name: "Report", exact: true }).click();
  await expect(panel.getByRole("button", { name: "Retry original report" })).toBeEnabled();
  await page.evaluate(operationKey => {
    const key = Object.keys(sessionStorage).find(key =>
      JSON.parse(sessionStorage.getItem(key) || "null")?.identity?.idempotencyKey === operationKey)!;
    const state = JSON.parse(sessionStorage.getItem(key)!);
    state.identity.requestFingerprint = "unrecognized-fixture-fingerprint";
    sessionStorage.setItem(key, JSON.stringify(state));
  }, fixture.requests[0].key);
  await page.reload();
  await expect(panel.getByRole("alert")).toBeVisible();
  await expect(panel.getByRole("button", { name: "Retry original sweep" })).toBeDisabled();
  await expect(panel.getByRole("button", { name: "Report", exact: true })).toHaveCount(0);
  await expect(panel.getByRole("button", { name: "Apply cleanup", exact: true })).toHaveCount(0);
  expect(fixture.requests).toHaveLength(1);
});

test("read-only browser sessions have no sweep controls", async ({ page }) => {
  const fixture = await setup(page, false, "read_only");
  await page.goto("/ui/engineering/preview-lifecycle");
  await expect(page.getByRole("region", { name: "Preview cleanup controls" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Report", exact: true })).toHaveCount(0);
  expect(fixture.requests).toEqual([]);
});

test("a refused cleanup consumes its delete confirmation", async ({ page }) => {
  const fixture = await setup(page, "denied");
  await page.goto("/ui/engineering/preview-lifecycle");
  const panel = page.getByRole("region", { name: "Preview cleanup controls" });
  const confirmation = panel.getByRole("checkbox");
  await confirmation.check();
  await panel.getByRole("button", { name: "Apply cleanup", exact: true }).click();
  await expect(panel.locator('[role="status"][data-phase="failed"]')).toBeVisible();
  await expect(confirmation).not.toBeChecked();
  await expect(panel.getByRole("button", { name: "Apply cleanup", exact: true })).toBeDisabled();
  expect(fixture.requests).toHaveLength(1);
});


test("operation-key storage failure prevents a sweep", async ({ page }) => {
  const fixture = await setup(page);
  await page.addInitScript(() => {
    const setItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      let identity;
      try { identity = JSON.parse(value)?.identity; } catch { identity = null; }
      if (identity?.idempotencyKey) throw new DOMException("Operation storage full", "QuotaExceededError");
      return setItem.call(this, key, value);
    };
  });
  await page.goto("/ui/engineering/preview-lifecycle");
  const panel = page.getByRole("region", { name: "Preview cleanup controls" });
  await panel.getByRole("button", { name: "Report", exact: true }).click();
  await expect(panel.locator('[role="status"][data-phase="failed"]')).toBeVisible();
  expect(fixture.requests).toEqual([]);
});


test("fingerprint preparation locks both modes before another action can enter", async ({ page }) => {
  const fixture = await setup(page);
  await page.goto("/ui/engineering/preview-lifecycle");
  await page.evaluate(() => {
    const original = crypto.subtle.digest.bind(crypto.subtle);
    let release = () => {};
    const gate = new Promise<void>(resolve => { release = resolve; });
    Object.defineProperty(window, "releaseLifecycleDigest", { value: release, configurable: true });
    crypto.subtle.digest = async (algorithm, data) => { await gate; return original(algorithm, data); };
  });
  const panel = page.getByRole("region", { name: "Preview cleanup controls" });
  await panel.getByRole("checkbox").check();
  await panel.getByRole("button", { name: "Report", exact: true }).click();
  await expect(panel.getByRole("button", { name: "Apply cleanup", exact: true })).toBeDisabled();
  await expect(panel.getByRole("button", { name: "Report", exact: true })).toBeDisabled();
  expect(fixture.requests).toEqual([]);
  await page.evaluate(() => (window as unknown as { releaseLifecycleDigest: () => void }).releaseLifecycleDigest());
  await expect(panel.getByRole("region", { name: "Preview sweep result" }).getByRole("heading", { level: 2 })).toContainText("fail");
  expect(fixture.requests.map(request => request.body.apply)).toEqual([false]);
});


test("a retired page preparing a fingerprint cannot erase its successor's uncertain key", async ({ page }) => {
  const fixture = await setup(page, true);
  await page.goto("/ui/engineering/preview-lifecycle");
  await page.evaluate(() => {
    const original = crypto.subtle.digest.bind(crypto.subtle);
    let release = () => {};
    const gate = new Promise<void>(resolve => { release = resolve; });
    let first = true;
    let pending: Promise<ArrayBuffer>;
    Object.defineProperty(window, "releaseRetiredFingerprint", { value: async () => { release(); await pending; }, configurable: true });
    crypto.subtle.digest = (algorithm, data) => {
      if (!first) return original(algorithm, data);
      first = false;
      pending = (async () => { await gate; return original(algorithm, data); })();
      return pending;
    };
  });
  const panel = page.getByRole("region", { name: "Preview cleanup controls" });
  await panel.getByRole("button", { name: "Report", exact: true }).click();
  await page.getByRole("navigation", { name: "Engineering Ops views" }).getByRole("link", { name: "Overview", exact: true }).click();
  await page.getByRole("navigation", { name: "Engineering Ops views" }).getByRole("link", { name: "Preview cleanup", exact: true }).click();
  await panel.getByRole("checkbox").check();
  await panel.getByRole("button", { name: "Apply cleanup", exact: true }).click();
  await expect(panel.getByRole("button", { name: /Retry original (report|cleanup)/ })).toBeEnabled();
  await page.evaluate(() => (window as unknown as { releaseRetiredFingerprint: () => Promise<void> }).releaseRetiredFingerprint());
  await page.reload();
  await expect(panel.getByRole("button", { name: /Retry original (report|cleanup)/ })).toBeEnabled();
  await panel.getByRole("button", { name: /Retry original (report|cleanup)/ }).click();
  await expect(panel.getByRole("region", { name: "Preview sweep result" }).getByRole("heading", { level: 2 })).toContainText("fail");
  expect(fixture.requests).toHaveLength(2);
  expect(fixture.requests[1]).toEqual(fixture.requests[0]);
});
