import { expect, test, waitForState, type Page } from "./state";

async function setup(page: Page, outcome = "completed", admin = true) {
  await page.goto("/ui/products?fixture=empty");
  const fixtures = await page.evaluate(async () => {
    const module = await import("/ui/src/dev-fixtures.ts");
    return { identity: module.fixtureIdentity, products: module.productsForFixture("products"), environment: module.environmentForFixture("products", "atlas-commerce", "prod")! };
  });
  const requests: { method: string; path: string; query: string; body: any }[] = [];
  const completed = new Set<string>();
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "private-recovery-csrf", is_administrator: admin, identity: { ...fixtures.identity, role: admin ? "admin" : "read_only" } } }));
  await page.route("**/v1/production-backup-authority/proposals?*", route => route.fulfill({ json: { status: "ok", proposals: [] } }));
  await page.route("**/backup-worker-bindings", route => route.fulfill({ json: { status: "accepted", result: { credentials: [] } } }));
  await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: fixtures.products } }));
  await page.route("**/v1/products/atlas-commerce/environments/prod", route => route.fulfill({ json: { status: "ok", trace_id: "lane", environment: fixtures.environment } }));
  await page.route("**/v1/products/atlas-commerce/environments/prod/config-status", route => route.fulfill({ json: { status: "ok", trace_id: "config", config_status: { production_backup_authorities: [] } } }));
  await page.route("**/v1/product-profiles/atlas-commerce", route => route.fulfill({ json: { status: "ok", trace_id: "profile", profile: { lanes: [] } } }));
  await page.route("**/promotion-status", route => route.fulfill({ status: 503, json: { error: { code: "fixture", message: "Outside this recovery test." } } }));
  await page.route("**/operational-readiness?*", route => route.fulfill({ status: 503, json: { error: { code: "fixture", message: "Outside this recovery test." } } }));
  await page.route("**/v1/admin/generic-web/promotion-recovery/**", async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const operation = path.replace(/\/(apply|dry-run)$/, "");
    const body = request.method() === "POST" ? request.postDataJSON() : null;
    requests.push({ method: request.method(), path, query: new URL(request.url()).search, body });
    if (body) expect(request.headers()["x-csrf-token"]).toBe("private-recovery-csrf");
    if (path.endsWith("/apply")) {
      if (outcome === "denied" || outcome === "changed") return route.fulfill({ status: outcome === "denied" ? 403 : 409, json: { trace_id: "recovery-refusal", error: { code: outcome === "denied" ? "authorization_denied" : "stale_recovery_digest", message: "Recovery stopped." } } });
      completed.add(operation);
      if (outcome === "uncertain") return route.abort("failed");
    }
    const applied = completed.has(operation);
    return route.fulfill({ json: {
      product: outcome === "wrong-product" && body ? "other-product" : "atlas-commerce", instance: "prod",
      recovery_reference: outcome === "wrong-reference" && body ? "another-operation" : "original-operation",
      reservation_state: applied ? "completed" : outcome === "expired-running" ? "running" : "reconcile_required", reservation_attempt: 1,
      checkpoint: "provider_dispatch_started", proposed_action: applied ? "replay_completed" : body ? outcome === "held" ? "hold_unknown" : "adopt_promotion" : "hold_unknown",
      recovery_digest: "a".repeat(64), ...(path.endsWith("/apply") ? { status: "accepted" } : {}),
    } });
  });
  await page.goto("/ui/products/atlas-commerce/environments/prod/actions");
  return { requests, panel: page.getByRole("region", { name: "Recover promotion", exact: true }) };
}

async function review(panel: ReturnType<Page["getByRole"]>) {
  await panel.getByLabel("Release decision").fill("original-decision");
  await panel.getByLabel("Recovery reason").fill("Reconcile the original accepted release.");
  await panel.getByRole("button", { name: "Review recovery", exact: true }).click();
}

test("admin reviews the original, invalidates edits, applies once and reads back separately", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const { panel, requests } = await setup(page);
  await review(panel);
  await expect(panel).toContainText("original-operation");
  await expect(panel).toContainText("reconcile_required");
  const apply = panel.getByRole("button", { name: "Apply reviewed recovery", exact: true });
  await expect(apply).toBeDisabled();
  await panel.getByRole("checkbox").check();
  await panel.getByLabel("Recovery reason").fill("A changed reason.");
  await expect(apply).toBeDisabled();
  await panel.getByRole("button", { name: "Review recovery", exact: true }).click();
  await panel.getByRole("checkbox").check();
  await panel.screenshot({ path: testInfo.outputPath("promotion-recovery-reviewed.png") });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await apply.click();
  await expect(panel.getByRole("status")).toBeVisible();
  expect(requests.filter(request => request.method === "GET")).toHaveLength(2);
  await expect(apply).toBeDisabled();
  await panel.getByRole("button", { name: "Read back original operation" }).click();
  await expect(panel.getByRole("status")).toBeVisible();
  await waitForState(() => requests.filter(request => request.method === "GET").length).toBe(3);
  const sent = requests.filter(request => request.path.endsWith("/apply"));
  expect(sent).toHaveLength(1);
  expect(sent[0].body).toEqual({ reason: "A changed reason.", recovery_reference: "original-operation", expected_recovery_digest: "a".repeat(64) });
  await expect(panel).not.toContainText("private-recovery-csrf");
  await page.reload();
  await expect(panel.getByRole("button", { name: "Apply reviewed recovery" })).toBeDisabled();
  await expect(panel.getByRole("button", { name: "Read back original operation" })).toBeEnabled();
});

for (const outcome of ["denied", "changed", "uncertain"]) {
  test(`recovery stops without retry after ${outcome}`, async ({ page }) => {
    const { panel, requests } = await setup(page, outcome);
    await review(panel); await panel.getByRole("checkbox").check();
    await panel.getByRole("button", { name: "Apply reviewed recovery" }).click();
    await expect(panel.getByRole("alert")).toBeVisible();
    await expect(panel.getByRole("button", { name: "Apply reviewed recovery" })).toBeDisabled();
    await page.reload();
    await expect(panel.getByRole("button", { name: "Review recovery", exact: true })).toBeDisabled();
    await panel.getByRole("button", { name: "Read back original operation" }).click();
    expect(requests.filter(request => request.path.endsWith("/apply"))).toHaveLength(1);
  });
}

for (const outcome of ["wrong-product", "wrong-reference", "held"]) {
  test(`unverified recovery cannot apply: ${outcome}`, async ({ page }) => {
    const { panel, requests } = await setup(page, outcome);
    await review(panel);
    if (outcome === "held") await expect(panel).toContainText("hold_unknown");
    else await expect(panel.getByRole("alert")).toBeVisible();
    await expect(panel.getByRole("button", { name: "Apply reviewed recovery" })).toBeDisabled();
    expect(requests.filter(request => request.path.endsWith("/apply"))).toHaveLength(0);
  });
}

test("non-admin has no recovery card or route calls", async ({ page }) => {
  const { panel, requests } = await setup(page, "completed", false);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(panel).toHaveCount(0);
  expect(requests).toHaveLength(0);
});

test("another selection cannot retry a previously attempted recovery", async ({ page }) => {
  const { panel, requests } = await setup(page, "uncertain");
  await review(panel); await panel.getByRole("checkbox").check();
  await panel.getByRole("button", { name: "Apply reviewed recovery" }).click();
  await expect(panel.getByRole("alert")).toBeVisible();
  await panel.getByRole("button", { name: "Choose another operation" }).click();
  await review(panel);
  await expect(panel.getByRole("status")).toBeVisible();
  await expect(panel.getByRole("button", { name: "Apply reviewed recovery" })).toBeDisabled();
  expect(requests.filter(request => request.path.endsWith("/apply"))).toHaveLength(1);
  await panel.getByRole("button", { name: "Choose another operation" }).click();
  await panel.getByLabel("Release decision").fill("another-decision");
  await panel.getByLabel("Release step").selectOption("rollback");
  await panel.getByLabel("Promotion attempt").selectOption("2");
  await panel.getByLabel("Recovery reason").fill("Inspect another original operation.");
  await panel.getByRole("button", { name: "Review recovery", exact: true }).click();
  await expect(panel.getByRole("checkbox")).toBeEnabled();
  const query = new URLSearchParams(requests.at(-1)!.query);
  expect(query.get("attempt")).toBe("2");
  expect(query.get("step")).toBe("rollback");
});

test("expired running reservation is actionable only when the route proves adoption", async ({ page }) => {
  const { panel } = await setup(page, "expired-running");
  await review(panel);
  await expect(panel).toContainText("running");
  await panel.getByRole("checkbox").check();
  await expect(panel.getByRole("button", { name: "Apply reviewed recovery" })).toBeEnabled();
});

test("unavailable session storage blocks recovery dispatch", async ({ page }) => {
  const { requests } = await setup(page);
  await page.addInitScript(() => Object.defineProperty(window, "sessionStorage", { get() { throw new DOMException("Unavailable", "SecurityError"); } }));
  await page.reload();
  const panel = page.getByRole("region", { name: "Recover promotion", exact: true });
  await expect(panel.getByRole("alert")).toBeVisible();
  await expect(panel.getByRole("button", { name: "Review recovery", exact: true })).toBeDisabled();
  expect(requests).toHaveLength(0);
});

test("unverified saved selection can choose another operation without clearing the original attempt", async ({ page }) => {
  const { panel, requests } = await setup(page, "uncertain");
  await review(panel); await panel.getByRole("checkbox").check();
  await panel.getByRole("button", { name: "Apply reviewed recovery" }).click();
  await expect(panel.getByRole("alert")).toBeVisible();
  await page.evaluate(() => {
    const key = Object.keys(sessionStorage).sort((left, right) => left.length - right.length).find(name => {
      try { return JSON.parse(sessionStorage.getItem(name) ?? "null")?.plan?.recovery_reference === "original-operation"; }
      catch { return false; }
    });
    if (!key) throw new Error("No saved review found.");
    sessionStorage.setItem(key, "invalid saved selection");
  });
  await page.reload();
  await expect(panel.getByRole("alert")).toBeVisible();
  await panel.getByRole("button", { name: "Choose another operation" }).click();
  await review(panel);
  await expect(panel.getByRole("button", { name: "Apply reviewed recovery" })).toBeDisabled();
  await expect(panel.getByRole("button", { name: "Read back original operation" })).toBeEnabled();
  expect(requests.filter(request => request.path.endsWith("/apply"))).toHaveLength(1);
  await panel.getByRole("button", { name: "Choose another operation" }).click();
  await page.reload();
  await expect(panel.getByLabel("Release decision")).toBeEnabled();
  await expect(panel.getByLabel("Release decision")).toHaveValue("");
});
