import { expect, test } from "@playwright/test";
import { READ_REFRESH_INTERVAL_MS } from "../../src/use-evidence-refresh";

for (const theme of ["dark", "light"] as const) {
  test(`sidebar refreshes, distinguishes stale data and recovers in ${theme}`, { tag: "@layout" }, async ({ page }, testInfo) => {
    await page.goto("/ui/products?fixture=products");
    const fixtures = await page.evaluate(async () => {
      const modulePath = "/ui/src/dev-fixtures.ts";
      const module = await import(modulePath);
      const products = [structuredClone(module.productsForFixture("products")[0])];
      const now = Date.now();
      const freshen = (value: unknown) => {
        if (!value || typeof value !== "object") return;
        const record = value as Record<string, unknown>;
        if ("stale_after" in record) record.stale_after = new Date(now + 3600_000).toISOString();
        Object.values(record).forEach(freshen);
      };
      freshen(products);
      const product = products[0];
      product.display_name = "Freshness example";
      const lane = product.environments.find((lane: { environment: string }) => lane.environment === "testing")!;
      product.environments = [lane];
      lane.provenance.freshness_status = "verified";
      lane.warnings = [];
      lane.topology.warnings = [];
      lane.topology.observed.placement.trust_state = "verified";
      lane.topology.observed.placement.runtime_identity_status = "match";
      lane.health_monitoring.open_incidents = [];
      lane.health_monitoring.checks = [lane.health_monitoring.checks[0]];
      Object.assign(lane.health_monitoring.checks[0], { status: "pass", trust_state: "verified", probe_effective: true, incident_status: "" });
      return { products, identity: module.fixtureIdentity, now };
    });
    await page.clock.install({ time: new Date(fixtures.now) });
    await page.clock.pauseAt(new Date(fixtures.now + 1));
    let mode: "fresh" | "warning" | "alert" | "fail" | "delay" = "fresh";
    let reads = 0;
    let release: (() => void) | undefined;
    const mutations: string[] = [];
    page.on("request", request => {
      if (request.url().includes("/v1/") && request.method() !== "GET") mutations.push(request.url());
    });
    await page.route("**/v1/**", route => route.fulfill({ status: 403, json: { status: "error", trace_id: "unused", error: { code: "authorization_denied", message: "Unused read" } } }));
    await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "csrf", identity: fixtures.identity } }));
    await page.route("**/v1/products", async route => {
      reads++;
      if (mode === "delay") await new Promise<void>(resolve => { release = resolve; });
      if (mode === "fail") return route.fulfill({ status: 503, json: { status: "error", trace_id: "offline", error: { code: "read_unavailable", message: "Inventory unavailable" } } });
      const products = structuredClone(fixtures.products);
      if (mode === "warning") products[0].environments[0].topology.observed.tls_domains = [];
      if (mode === "alert") products[0].environments[0].health_monitoring.checks[0].status = "fail";
      return route.fulfill({ json: { status: "ok", trace_id: "products", products } });
    });
    await page.goto("/ui/products");
    if (theme === "light") await page.getByRole("button", { name: "Switch to light theme" }).click();
    const dots = page.locator(".rail-product-link [data-lane]");
    const testing = page.locator('.rail-product-link [data-lane="testing"]');
    await expect(testing).toHaveAttribute("data-tone", "verified");
    const screenshot = async (state: string) => {
      await page.clock.runFor(200);
      await page.screenshot({ path: `../tmp/browser-smoke/sidebar-${state}-${theme}-${testInfo.project.name}.png`, animations: "disabled" });
    };
    await screenshot("fresh");
    mode = "warning";
    const initialReads = reads;
    await page.clock.runFor(READ_REFRESH_INTERVAL_MS + 10);
    await expect.poll(() => reads).toBe(initialReads + 1);
    await expect(testing).toHaveAttribute("data-tone", "warning");
    if (testInfo.project.name === "desktop") await testing.hover();
    await expect(testing).toHaveAttribute("title", /DNS\/TLS/);
    const warningSymbol = await testing.locator("svg").innerHTML();
    await screenshot("warning");
    mode = "alert";
    await page.clock.runFor(READ_REFRESH_INTERVAL_MS + 10);
    await expect(testing).toHaveAttribute("data-tone", "danger");
    mode = "fail";
    await page.clock.runFor(READ_REFRESH_INTERVAL_MS + 10);
    await expect(page.locator(".rail-freshness")).toContainText("Stale data");
    for (const dot of await dots.all()) {
      await expect(dot).toHaveAttribute("data-tone", "stale");
      expect(await dot.locator("svg").innerHTML()).not.toBe(warningSymbol);
    }
    await screenshot("stale");
    mode = "fresh";
    await page.clock.runFor(READ_REFRESH_INTERVAL_MS + 10);
    await expect(testing).toHaveAttribute("data-tone", "verified");
    await expect(page.locator(".rail-freshness")).not.toContainText("Stale data");
    mode = "delay";
    await page.clock.runFor(READ_REFRESH_INTERVAL_MS + 10);
    await expect.poll(() => Boolean(release)).toBe(true);
    const pendingReads = reads;
    // A wall-clock correction cannot extend the inventory's freshness bound.
    await page.clock.setSystemTime(new Date(fixtures.now - 3600_000));
    await page.clock.runFor(2 * READ_REFRESH_INTERVAL_MS);
    expect(reads).toBe(pendingReads);
    await expect(testing).toHaveAttribute("data-tone", "stale");
    mode = "fresh";
    release!();
    await expect(testing).toHaveAttribute("data-tone", "verified");
    await page.goto(`/ui/products/${fixtures.products[0].product}/activity`);
    await expect(testing).toHaveAttribute("data-tone", "verified");
    const activityReads = reads;
    await page.clock.runFor(READ_REFRESH_INTERVAL_MS + 10);
    await expect.poll(() => reads).toBe(activityReads + 1);
    expect(mutations).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
}
