import { expect, test, type Page } from "./state";

async function setup(page: Page, dropped = false, role = "admin") {
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
  await expect(page.getByRole("heading", { name: "Report result: fail" })).toBeVisible();
  await expect(panel.getByText("Held previews require original-intent recovery before cleanup apply.")).toBeVisible();
  await expect(panel.getByRole("button", { name: "Apply cleanup", exact: true })).toBeDisabled();
  await panel.getByLabel("Delete orphaned previews across all preview-enabled products.").check();
  await panel.getByRole("button", { name: "Apply cleanup", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Cleanup result: fail" })).toBeVisible();
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
    if (apply) await panel.getByLabel("Delete orphaned previews across all preview-enabled products.").check();
    await panel.getByRole("button", { name: apply ? "Apply cleanup" : "Report", exact: true }).click();
    await expect(panel.getByRole("button", { name: "Retry original sweep" })).toBeEnabled();
    await expect(panel.getByRole("button", { name: "Report", exact: true })).toHaveCount(0);
    await expect(panel.getByRole("button", { name: "Apply cleanup", exact: true })).toHaveCount(0);
    await page.reload();
    await panel.getByRole("button", { name: "Retry original sweep" }).click();
    await expect(panel.getByRole("heading", { name: `${apply ? "Cleanup" : "Report"} result: fail` })).toBeVisible();
    expect(fixture.requests).toHaveLength(2);
    expect(fixture.requests[1]).toEqual(fixture.requests[0]);
    expect(fixture.errors).toEqual([]);
  });
}

for (const corruption of ["changed", "missing"] as const) {
  test(`${corruption} retained request cannot dispatch a different sweep`, async ({ page }) => {
    const fixture = await setup(page, true);
    await page.goto("/ui/engineering/preview-lifecycle");
    const panel = page.getByRole("region", { name: "Preview cleanup controls" });
    await panel.getByRole("button", { name: "Report", exact: true }).click();
    await expect(panel.getByRole("button", { name: "Retry original sweep" })).toBeEnabled();
    await page.evaluate(mode => {
      const key = Object.keys(sessionStorage).find(key => key.startsWith("launchplane:preview-lifecycle-draft:"))!;
      if (mode === "missing") sessionStorage.removeItem(key);
      else {
        const body = JSON.parse(sessionStorage.getItem(key)!);
        body.apply = true;
        sessionStorage.setItem(key, JSON.stringify(body));
      }
    }, corruption);
    await page.reload();
    if (corruption === "missing") {
      await expect(panel.getByRole("button", { name: "Retry original sweep" })).toBeDisabled();
      await expect(panel.getByRole("alert")).toContainText("exact original request is unavailable");
    } else {
      await panel.getByRole("button", { name: "Retry original sweep" }).click();
      await expect(panel.getByText("Preview sweep uncertain", { exact: true })).toBeVisible();
    }
    await expect(panel.getByRole("button", { name: "Report", exact: true })).toHaveCount(0);
    expect(fixture.requests).toHaveLength(1);
  });
}

test("read-only browser sessions have no sweep controls", async ({ page }) => {
  const fixture = await setup(page, false, "read_only");
  await page.goto("/ui/engineering/preview-lifecycle");
  await expect(page.getByText("Preview cleanup requires a signed-in administrator outside demonstration mode.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Report", exact: true })).toHaveCount(0);
  expect(fixture.requests).toEqual([]);
});


test("operation-key storage failure prevents a sweep even when payload storage works", async ({ page }) => {
  const fixture = await setup(page);
  await page.addInitScript(() => {
    const setItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (key.startsWith("launchplane.browser-operation.v2.")) throw new DOMException("Operation storage full", "QuotaExceededError");
      return setItem.call(this, key, value);
    };
  });
  await page.goto("/ui/engineering/preview-lifecycle");
  const panel = page.getByRole("region", { name: "Preview cleanup controls" });
  await panel.getByRole("button", { name: "Report", exact: true }).click();
  await expect(panel.getByText("Preview sweep failed", { exact: true })).toBeVisible();
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
  await panel.getByLabel("Delete orphaned previews across all preview-enabled products.").check();
  await panel.getByRole("button", { name: "Report", exact: true }).click();
  await expect(panel.getByRole("button", { name: "Apply cleanup", exact: true })).toBeDisabled();
  await expect(panel.getByRole("button", { name: "Report", exact: true })).toBeDisabled();
  expect(fixture.requests).toEqual([]);
  await page.evaluate(() => (window as unknown as { releaseLifecycleDigest: () => void }).releaseLifecycleDigest());
  await expect(panel.getByRole("heading", { name: "Report result: fail" })).toBeVisible();
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
  await panel.getByLabel("Delete orphaned previews across all preview-enabled products.").check();
  await panel.getByRole("button", { name: "Apply cleanup", exact: true }).click();
  await expect(panel.getByRole("button", { name: "Retry original sweep" })).toBeEnabled();
  await page.evaluate(() => (window as unknown as { releaseRetiredFingerprint: () => Promise<void> }).releaseRetiredFingerprint());
  await page.reload();
  await expect(panel.getByRole("button", { name: "Retry original sweep" })).toBeEnabled();
  await panel.getByRole("button", { name: "Retry original sweep" }).click();
  await expect(panel.getByRole("heading", { name: "Cleanup result: fail" })).toBeVisible();
  expect(fixture.requests).toHaveLength(2);
  expect(fixture.requests[1]).toEqual(fixture.requests[0]);
});
