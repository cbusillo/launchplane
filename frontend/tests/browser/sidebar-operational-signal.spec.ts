import { expect, test } from "@playwright/test";

const cases = ["healthy", "failed", "incident", "stale", "unknown", "absent"] as const;

for (const state of cases) {
  test(`sidebar and product views agree for ${state} lanes`, async ({ page }, testInfo) => {
    await page.goto("/ui/products?fixture=products");
    const fixtures = await page.evaluate(async state => {
      const modulePath = "/ui/src/dev-fixtures.ts";
      const module = await import(modulePath);
      const product = structuredClone(module.productsForFixture("products")[0]);
      const lane = product.environments.find((lane: { environment: string }) => lane.environment === "testing");
      const now = Date.now();
      // Raw recorded trust reproduces the healthy lane hidden by the old sidebar.
      lane.trust_state = "recorded";
      lane.provenance.freshness_status = "verified";
      lane.provenance.stale_after = new Date(now + 60_000).toISOString();
      // Keep unrelated topology proof fresh; expired proof has its own browser cases.
      const freshen = (value: unknown) => {
        if (!value || typeof value !== "object") return;
        const record = value as Record<string, unknown>;
        if ("stale_after" in record) record.stale_after = lane.provenance.stale_after;
        Object.values(record).forEach(freshen);
      };
      freshen(lane.topology);
      lane.warnings = [];
      lane.topology.warnings = [];
      lane.topology.observed.tls_domains = [];
      lane.topology.observed.placement.runtime_identity_status = "match";
      lane.health_monitoring.checks = [structuredClone(lane.health_monitoring.checks[0])];
      const check = lane.health_monitoring.checks[0];
      check.probe_effective = true;
      check.status = "pass";
      check.trust_state = "verified";
      check.incident_status = "";
      check.provenance.stale_after = lane.provenance.stale_after;
      if (state === "failed") check.status = "fail";
      if (state === "incident") check.incident_status = "open";
      if (state === "stale") lane.provenance.stale_after = new Date(now - 1000).toISOString();
      if (state === "unknown") {
        lane.trust_state = "missing";
        lane.provenance.freshness_status = "missing";
        lane.health_monitoring.checks = [];
      }
      product.environments = state === "absent" ? [] : [lane];
      return { product, identity: module.fixtureIdentity, now };
    }, state);
    await page.clock.install({ time: new Date(fixtures.now) });
    const mutations: string[] = [];
    page.on("request", request => {
      if (request.url().includes("/v1/") && request.method() !== "GET") mutations.push(request.url());
    });
    await page.route("**/v1/**", route => route.fulfill({ status: 403, json: { status: "error", trace_id: "unused-read", error: { code: "authorization_denied", message: "Fixture read unavailable" } } }));
    await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "csrf", identity: fixtures.identity } }));
    await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: [fixtures.product] } }));
    await page.route(`**/v1/products/${fixtures.product.product}`, route => route.fulfill({ json: { status: "ok", trace_id: "product", product: fixtures.product } }));
    await page.goto("/ui/products");
    const railLink = page.locator(".rail-product-link").filter({ hasText: fixtures.product.display_name });
    await page.getByRole("list", { name: "Launchplane products" })
      .getByRole("link", { name: new RegExp(fixtures.product.display_name) }).click();
    await expect(page.getByRole("heading", { level: 1, name: fixtures.product.display_name })).toBeVisible();
    const dot = railLink.locator('[data-lane="testing"]');
    const signal = page.locator(".signal-tile").filter({ hasText: /^Testing/i });
    const tone = state === "healthy" ? "verified" : state === "failed" || state === "incident" ? "danger" : state === "stale" ? "warning" : "missing";
    await expect(signal).toHaveAttribute("data-tone", tone);
    await expect(dot).toHaveAttribute("data-tone", tone);
    const description = state === "absent" ? "Testing lane: absent (not recorded)" : `Testing operational status: ${tone}`;
    await expect(dot).toHaveAttribute("title", description);
    await expect(railLink.getByRole("img", { includeHidden: true })).toHaveAttribute("aria-label", `${description}; Production lane: absent (not recorded)`);
    const colorToken = tone === "verified" ? "--prod" : tone === "danger" ? "--danger" : tone === "warning" ? "--warning" : "--unknown";
    expect(await dot.evaluate((element, token) => {
      const expected = document.createElement("span");
      expected.style.backgroundColor = `var(${token})`;
      element.append(expected);
      const matches = getComputedStyle(element).backgroundColor === getComputedStyle(expected).backgroundColor;
      expected.remove();
      return matches;
    }, colorToken)).toBe(true);
    if (state === "healthy" || state === "absent" || state === "failed") {
      await page.screenshot({ path: `../tmp/browser-smoke/sidebar-${state}-${testInfo.project.name}.png`, fullPage: true });
    }
    expect(mutations).toEqual([]);
  });
}
