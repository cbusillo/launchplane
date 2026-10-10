import { expect, test } from "@playwright/test";
import { READ_REFRESH_INTERVAL_MS } from "../../src/use-evidence-refresh";

for (const environmentView of [false, true]) {
  test(`open ${environmentView ? "environment" : "workspace"} reads recover without reusing expired proof`, async ({ page }, testInfo) => {
    await page.goto("/ui/products?fixture=products");
    const fixtures = await page.evaluate(async () => {
      const modulePath = "/ui/src/dev-fixtures.ts";
      const module = await import(modulePath);
      const products = module.productsForFixture("products");
      const product = products[0];
      const detail = module.environmentForFixture("products", product.product, "testing");
      return { products, product, detail, identity: module.fixtureIdentity,
        incidents: module.incidentsForFixture("products", product.product, "testing") };
    });
    const start = Date.parse(fixtures.detail.provenance.stale_after) - 1000;
    await page.clock.install({ time: new Date(start) });
    let responseMode: "old" | "delay" | "fail" | "unverified" | "fresh" = "old";
    let reads = 0;
    let inventoryReads = 0;
    let delayInventory = false;
    let releaseInventory: (() => void) | undefined;
    let releaseRead: (() => void) | undefined;
    const mutations: string[] = [];
    page.on("request", request => {
      if (request.url().includes("/v1/") && request.method() !== "GET") mutations.push(request.url());
    });
    const currentProof = <T,>(data: T): T => {
      const copy = structuredClone(data);
      const now = new Date(start + 20 * READ_REFRESH_INTERVAL_MS).toISOString();
      const refresh = (value: unknown) => {
        if (!value || typeof value !== "object") return;
        const record = value as Record<string, unknown>;
        if ("stale_after" in record) {
          record.stale_after = now;
          record.refreshed_at = new Date(start + 3 * READ_REFRESH_INTERVAL_MS).toISOString();
        }
        for (const child of Object.values(record)) refresh(child);
      };
      refresh(copy);
      return copy;
    };
    await page.route("**/v1/**", route => route.fulfill({ status: 403, json: { status: "error", trace_id: "unused-read", error: { code: "authorization_denied", message: "Fixture read unavailable" } } }));
    await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "csrf", identity: fixtures.identity } }));
    await page.route("**/v1/products", async route => {
      inventoryReads++;
      const products = responseMode === "fresh" ? currentProof(fixtures.products) : structuredClone(fixtures.products);
      if (delayInventory) {
        products[0].display_name = "Obsolete delayed inventory";
        await new Promise<void>(resolve => { releaseInventory = resolve; });
      }
      return route.fulfill({ json: { status: "ok", trace_id: "products", products } });
    });
    const productUrl = `**/v1/products/${fixtures.product.product}`;
    const detailUrl = `${productUrl}/environments/testing`;
    const targetUrl = environmentView ? detailUrl : productUrl;
    if (environmentView) await page.route(productUrl, route => route.fulfill({ json: { status: "ok", trace_id: "product", product: fixtures.product } }));
    await page.route(targetUrl, async route => {
      reads++;
      if (responseMode === "delay") await new Promise<void>(resolve => { releaseRead = resolve; });
      if (responseMode === "fail") {
        await route.fulfill({ status: 503, json: { status: "error", error: { code: "read_unavailable", message: "Evidence read unavailable" }, trace_id: "failed-refresh" } });
        return;
      }
      let data = environmentView ? fixtures.detail : fixtures.product;
      if (responseMode === "unverified") {
        data = currentProof(data);
        const lane = environmentView ? data : data.environments.find((value: { environment: string }) => value.environment === "testing");
        lane.provenance.freshness_status = "recorded";
        lane.trust_state = "recorded";
        lane.health_monitoring.checks[0].runtime_identity_status = "missing";
        lane.health_monitoring.checks[0].trust_state = "recorded";
        lane.topology.observed.placement.provenance.freshness_status = "recorded";
        lane.topology.observed.placement.trust_state = "recorded";
      }
      await route.fulfill({ json: { status: "ok", trace_id: "read",
        [environmentView ? "environment" : "product"]: responseMode === "fresh" ? currentProof(data) : data } });
    });
    await page.route(`${detailUrl}/public-ingress/incidents`, route => route.fulfill({ json: { status: "ok", trace_id: "incidents", incident_list: fixtures.incidents } }));
    await page.goto(`/ui/products/${fixtures.product.product}${environmentView ? "/environments/testing" : ""}`);
    const signal = page.locator(environmentView ? ".condition-tile" : ".signal-tile")
      .filter({ hasText: environmentView ? /^Runtime identity/i : /^Testing/i });
    await expect(signal).toHaveAttribute("data-tone", environmentView ? "pass" : "verified");
    await page.clock.runFor(2000);
    await expect(signal).toContainText("Stale");
    await expect(signal).toHaveAttribute("data-tone", "warning");
    const initialReads = reads;
    responseMode = "delay";
    await page.clock.runFor(READ_REFRESH_INTERVAL_MS);
    await expect.poll(() => Boolean(releaseRead)).toBe(true);
    expect(reads).toBe(initialReads + 1);
    await page.clock.runFor(2 * READ_REFRESH_INTERVAL_MS);
    expect(reads).toBe(initialReads + 1);
    await expect(signal).toHaveAttribute("data-tone", "warning");
    responseMode = "fail";
    releaseRead!();
    await expect(page.getByText("Evidence read unavailable", { exact: false }).first()).toBeVisible();
    await expect(signal).toContainText("Stale");
    await page.screenshot({ path: `../tmp/browser-smoke/open-${environmentView ? "environment" : "workspace"}-failed-${testInfo.project.name}.png`, fullPage: true });
    // Immediate failures with the same status must keep retrying even if React
    // batches loading and error into one render.
    await page.clock.runFor(READ_REFRESH_INTERVAL_MS);
    await expect.poll(() => reads).toBe(initialReads + 2);
    await expect(signal).toContainText("Stale");
    responseMode = "unverified";
    await page.clock.runFor(READ_REFRESH_INTERVAL_MS);
    await expect.poll(() => reads).toBe(initialReads + 3);
    await expect(signal).toHaveAttribute("data-tone", "warning");
    await expect(signal).toContainText("Recorded");
    responseMode = "fresh";
    await page.evaluate(() => Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" }));
    await page.clock.runFor(READ_REFRESH_INTERVAL_MS);
    expect(reads).toBe(initialReads + 3);
    await page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
      document.dispatchEvent(new Event("visibilitychange"));
      window.dispatchEvent(new Event("focus"));
      window.dispatchEvent(new Event("focus"));
    });
    await expect.poll(() => reads).toBe(initialReads + 4);
    await expect(signal).toHaveAttribute("data-tone", environmentView ? "pass" : "verified");
    await expect(signal).not.toContainText("Stale");
    // Inventory has its own read lifecycle, including the rail's lane evidence.
    await page.clock.runFor(READ_REFRESH_INTERVAL_MS);
    await expect(page.locator('.rail-product-link[data-active="true"] [data-lane="testing"]')).toHaveAttribute("data-tone", "verified");
    await page.screenshot({ path: `../tmp/browser-smoke/open-${environmentView ? "environment" : "workspace"}-recovered-${testInfo.project.name}.png`, fullPage: true });
    delayInventory = true;
    await page.clock.runFor(READ_REFRESH_INTERVAL_MS);
    await expect.poll(() => Boolean(releaseInventory)).toBe(true);
    const manualRefresh = page.getByRole("button", { name: "Refresh current evidence" });
    await expect(manualRefresh).toBeEnabled();
    delayInventory = false;
    const delayedInventoryReads = inventoryReads;
    await manualRefresh.click();
    await expect.poll(() => inventoryReads).toBe(delayedInventoryReads + 1);
    releaseInventory!();
    await expect(manualRefresh.locator("svg")).not.toHaveClass(/spin/);
    await expect(page.locator(".rail-products")).not.toContainText("Obsolete delayed inventory");
    responseMode = "delay";
    releaseRead = undefined;
    await page.clock.runFor(READ_REFRESH_INTERVAL_MS);
    await expect.poll(() => Boolean(releaseRead)).toBe(true);
    const observedReads = reads;
    await page.getByRole("link", { name: "Engineering Ops" }).click();
    await expect(page.getByRole("heading", { name: "Platform delivery systems", exact: true })).toBeVisible();
    const observedInventoryReads = inventoryReads;
    responseMode = "fresh";
    releaseRead!();
    await page.clock.runFor(2 * READ_REFRESH_INTERVAL_MS);
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    expect(reads).toBe(observedReads);
    expect(inventoryReads).toBe(observedInventoryReads);
    expect(mutations).toEqual([]);
  });
}

for (const view of ["actions", "runtime-settings", "managed-secrets"]) {
  test(`automatic detail reads preserve ${view} state`, async ({ page }) => {
    await page.goto("/ui/products?fixture=products");
    const fixtures = await page.evaluate(async () => {
      const modulePath = "/ui/src/dev-fixtures.ts";
      const module = await import(modulePath);
      const products = module.productsForFixture("products");
      const product = products[0];
      const detail = module.environmentForFixture("products", product.product, "testing");
      const config = module.configStatusForFixture("products", product.product, "testing");
      const action = detail.available_actions.find((item: { authz_action: string }) => item.authz_action);
      return { products, product, detail, config, identity: module.fixtureIdentity,
        readiness: module.operationalReadinessForFixture("products", detail, action) };
    });
    await page.clock.install({ time: new Date(fixtures.detail.provenance.refreshed_at) });
    let detailReads = 0;
    let configReads = 0;
    let readinessReads = 0;
    let expectedArtifact = "";
    const mutations: string[] = [];
    page.on("request", request => {
      if (request.url().includes("/v1/") && request.method() !== "GET") mutations.push(request.url());
    });
    await page.route("**/v1/**", route => route.fulfill({ status: 403, json: { error: { code: "authorization_denied", message: "Unused fixture read" } } }));
    await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", csrf_token: "csrf", identity: fixtures.identity } }));
    await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", products: fixtures.products } }));
    const detailUrl = `**/v1/products/${fixtures.product.product}/environments/testing`;
    await page.route(detailUrl, route => {
      detailReads++;
      return route.fulfill({ json: { status: "ok", environment: fixtures.detail } });
    });
    await page.route(`${detailUrl}/config-status`, route => {
      configReads++;
      return route.fulfill({ json: { status: "ok", config_status: fixtures.config } });
    });
    await page.route("**/operational-readiness?*", route => {
      readinessReads++;
      const query = new URL(route.request().url()).searchParams;
      const requestedAction = query.get("action");
      expectedArtifact = query.get("expected_current_artifact_id") ?? "";
      return route.fulfill({ json: { status: "ok", readiness: { ...fixtures.readiness,
        action: { ...fixtures.readiness.action, requested_action: requestedAction } } } });
    });
    await page.goto(`/ui/products/${fixtures.product.product}/environments/testing/${view}`);
    if (view === "actions") {
      await expect(page.getByLabel("Inspect exact action")).toBeVisible();
      await expect(page.getByRole("button", { name: "Refresh readiness" })).toBeEnabled();
    } else {
      const field = page.locator('.product-config-field').first();
      await field.getByRole("checkbox").check();
      await field.getByLabel(view === "runtime-settings" ? "New value" : "Write-only value").fill("local-fixture-draft");
      await page.getByLabel(/Change reason/).fill("Preserve my draft");
    }
    const observedDetailReads = detailReads;
    const observedConfigReads = configReads;
    const observedReadinessReads = readinessReads;
    const selectedAction = view === "actions" ? await page.getByLabel("Inspect exact action").inputValue() : "";
    // Wall-clock corrections must not stop the monotonic refresh timer.
    await page.clock.setSystemTime(new Date(Date.parse(fixtures.detail.provenance.refreshed_at) - 3600_000));
    await page.clock.runFor(READ_REFRESH_INTERVAL_MS);
    await expect.poll(() => detailReads).toBe(observedDetailReads + 1);
    await expect(page.getByText("Refreshing evidence", { exact: true })).not.toBeVisible();
    expect(configReads).toBe(observedConfigReads);
    expect(readinessReads).toBe(observedReadinessReads);
    if (view === "actions") {
      await expect(page.getByLabel("Inspect exact action")).toHaveValue(selectedAction);
      await page.getByRole("button", { name: "Refresh current evidence" }).click();
      await expect.poll(() => readinessReads).toBeGreaterThan(observedReadinessReads);
      await expect(page.getByLabel("Inspect exact action")).toHaveValue(selectedAction);
      const afterManualReadinessReads = readinessReads;
      fixtures.detail.target.expected_runtime_identity.artifact_id = "fixture-next-expected-artifact";
      await page.clock.runFor(READ_REFRESH_INTERVAL_MS);
      await expect.poll(() => readinessReads).toBe(afterManualReadinessReads + 1);
      expect(expectedArtifact).toBe("fixture-next-expected-artifact");
    } else {
      await expect(page.locator('.product-config-field').first()
        .getByLabel(view === "runtime-settings" ? "New value" : "Write-only value")).toHaveValue("local-fixture-draft");
      await expect(page.getByLabel(/Change reason/)).toHaveValue("Preserve my draft");
    }
    expect(mutations).toEqual([]);
  });
}
