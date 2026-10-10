import { expect, test } from "@playwright/test";
import { READ_REFRESH_INTERVAL_MS } from "../../src/use-evidence-refresh";

function setDeadlines(value: unknown, deadline: number) {
  if (!value || typeof value !== "object") return;
  const record = value as Record<string, unknown>;
  if ("stale_after" in record) record.stale_after = new Date(deadline).toISOString();
  Object.values(record).forEach(child => setDeadlines(child, deadline));
}

for (const evidence of ["route", "tls"] as const) for (const environmentView of [false, true]) {
  test(`${evidence} proof expires in an open ${environmentView ? "environment" : "workspace"} during pending and failed reads`, async ({ page }, testInfo) => {
    await page.goto("/ui/products?fixture=products");
    const fixture = await page.evaluate(async () => {
      const modulePath = "/ui/src/dev-fixtures.ts";
      const module = await import(modulePath);
      const product = module.productsForFixture("products")[0];
      const detail = module.environmentForFixture("products", product.product, "testing");
      const incidents = module.incidentsForFixture("products", product.product, "testing");
      incidents.incidents = [];
      return { product, detail, incidents, identity: module.fixtureIdentity };
    });
    const start = Date.parse(fixture.detail.provenance.refreshed_at);
    await page.clock.install({ time: new Date(start - READ_REFRESH_INTERVAL_MS) });
    await page.clock.pauseAt(new Date(start));
    setDeadlines(fixture.product, start + 10 * READ_REFRESH_INTERVAL_MS);
    setDeadlines(fixture.detail, start + 10 * READ_REFRESH_INTERVAL_MS);
    const summary = fixture.product.environments.find(lane => lane.environment === "testing")!;
    for (const lane of [summary, fixture.detail]) {
      lane.warnings = [];
      lane.topology.warnings = [];
      for (const check of lane.health_monitoring.checks) check.incident_status = "";
      if (evidence === "route") setDeadlines(lane.topology.provider_recorded, start + 1000);
      else setDeadlines(lane.topology.observed.tls_domains, start + 1000);
    }
    let mode: "old" | "pending" | "fail" | "fresh" = "old";
    let releaseRead: (() => void) | undefined;
    const mutations: string[] = [];
    page.on("request", request => {
      if (request.url().includes("/v1/") && request.method() !== "GET") mutations.push(request.url());
    });
    await page.route("**/v1/**", route => route.fulfill({ status: 403, json: { error: { code: "authorization_denied" } } }));
    await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", csrf_token: "fixture", identity: fixture.identity } }));
    await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", products: [fixture.product] } }));
    const productUrl = `**/v1/products/${fixture.product.product}`;
    const detailUrl = `${productUrl}/environments/testing`;
    if (environmentView) await page.route(productUrl, route => route.fulfill({ json: { status: "ok", product: fixture.product } }));
    await page.route(environmentView ? detailUrl : productUrl, async route => {
      if (mode === "pending") await new Promise<void>(resolve => { releaseRead = resolve; });
      if (mode === "fail") {
        await route.fulfill({ status: 503, json: { status: "error", error: { code: "read_unavailable", message: "Topology evidence read unavailable" } } });
        return;
      }
      const data = structuredClone(environmentView ? fixture.detail : fixture.product);
      if (mode === "fresh") setDeadlines(data, start + 10 * READ_REFRESH_INTERVAL_MS);
      await route.fulfill({ json: { status: "ok", [environmentView ? "environment" : "product"]: data } });
    });
    await page.route(`${detailUrl}/public-ingress/incidents`, route => route.fulfill({ json: { status: "ok", incident_list: fixture.incidents } }));
    await page.goto(`/ui/products/${fixture.product.product}${environmentView ? "/environments/testing" : ""}`);
    const signal = environmentView
      ? evidence === "tls"
        ? page.locator(".condition-tile").filter({ hasText: /^TLS/i })
        : page.getByText("Route authority", { exact: true }).locator("..")
      : page.locator(".signal-tile").filter({ hasText: /^Testing/i });
    const assertState = async (expired: boolean) => {
      if (environmentView) await expect(page.getByLabel("Lane status")).toHaveAttribute("data-tone", expired ? "warning" : "verified");
      if (environmentView && evidence === "tls") await expect(page.locator(".tls-domain-list li").first()).toHaveAttribute("data-tone", expired ? "warning" : "pass");
      if (environmentView && evidence === "route") await expect(signal).toContainText(expired ? "Stale" : "Recorded");
      else await expect(signal).toHaveAttribute("data-tone", expired ? "warning" : environmentView ? "pass" : "verified");
    };
    await assertState(false);
    mode = "pending";
    await page.getByRole("button", { name: "Refresh current evidence" }).click();
    await expect.poll(() => Boolean(releaseRead)).toBe(true);
    await page.clock.runFor(2000);
    await assertState(true);
    if (environmentView) await expect(page.locator(".condition-tile").filter({ hasText: /^Runtime identity/i })).toHaveAttribute("data-tone", "pass");
    mode = "fail";
    releaseRead!();
    await expect(page.getByText("Topology evidence read unavailable", { exact: false }).first()).toBeVisible();
    await assertState(true);
    await page.screenshot({ path: `../tmp/browser-smoke/${evidence}-${environmentView ? "environment" : "workspace"}-expired-${testInfo.project.name}.png`, fullPage: true });
    mode = "pending";
    releaseRead = undefined;
    await page.evaluate(() => Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" }));
    await page.clock.runFor(READ_REFRESH_INTERVAL_MS);
    await page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
      document.dispatchEvent(new Event("visibilitychange"));
      window.dispatchEvent(new Event("focus"));
    });
    await expect.poll(() => Boolean(releaseRead)).toBe(true);
    await assertState(true);
    mode = "fresh";
    releaseRead!();
    await assertState(false);
    expect(mutations).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  });
}
