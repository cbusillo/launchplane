import { expect, test } from "@playwright/test";

const cases = ["healthy", "failed", "incident", "stale", "unknown", "absent"] as const;

for (const theme of ["dark", "light"] as const) {

for (const state of cases) {
  test(`sidebar and product views agree for ${state} lanes in ${theme} theme`, async ({ page }, testInfo) => {
    await page.goto("/ui/products?fixture=products");
    const fixtures = await page.evaluate(async state => {
      const modulePath = "/ui/src/dev-fixtures.ts";
      const module = await import(modulePath);
      const { sidebarStatus } = await import("/ui/src/sidebar-status.ts");
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
      const products = [product];
      if (state === "healthy") {
        for (const extraState of ["warning", "failing", "unknown"]) {
          const extra = structuredClone(product);
          extra.product = `example-${extraState}`;
          extra.display_name = `${extraState[0].toUpperCase() + extraState.slice(1)} example`;
          const extraLane = extra.environments[0];
          if (extraState === "warning") extraLane.provenance.stale_after = new Date(now - 1000).toISOString();
          if (extraState === "failing") {
            extraLane.health_monitoring.checks[0].status = "fail";
            extraLane.health_monitoring.checks[0].failure_code = "dns_failure";
          }
          if (extraState === "unknown") {
            extraLane.trust_state = "missing";
            extraLane.provenance.freshness_status = "missing";
            extraLane.health_monitoring.checks = [];
          }
          products.push(extra);
        }
      }
      return { product, products, identity: module.fixtureIdentity, now, testingDescription: sidebarStatus(product.environments[0] ?? null, "Testing").description };
    }, state);
    await page.clock.install({ time: new Date(fixtures.now) });
    if (theme === "light") await page.getByRole("button", { name: "Switch to light theme" }).click();
    const mutations: string[] = [];
    page.on("request", request => {
      if (request.url().includes("/v1/") && request.method() !== "GET") mutations.push(request.url());
    });
    await page.route("**/v1/**", route => route.fulfill({ status: 403, json: { status: "error", trace_id: "unused-read", error: { code: "authorization_denied", message: "Fixture read unavailable" } } }));
    await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "csrf", identity: fixtures.identity } }));
    await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: fixtures.products } }));
    await page.route(`**/v1/products/${fixtures.product.product}`, route => route.fulfill({ json: { status: "ok", trace_id: "product", product: fixtures.product } }));
    await page.goto("/ui/products");
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    const railLink = page.locator(".rail-product-link").filter({ hasText: fixtures.product.display_name });
    await page.getByRole("list", { name: "Launchplane products" })
      .getByRole("link", { name: new RegExp(fixtures.product.display_name) }).click();
    await expect(page.getByRole("heading", { level: 1, name: fixtures.product.display_name })).toBeVisible();
    const dot = railLink.locator('[data-lane="testing"]');
    const signal = page.locator(".signal-tile").filter({ hasText: /^Testing/i });
    const tone = state === "healthy" ? "verified" : state === "failed" || state === "incident" ? "danger" : state === "stale" ? "warning" : "missing";
    await expect(signal).toHaveAttribute("data-tone", tone);
    await expect(dot).toHaveAttribute("data-tone", tone);
    await expect(dot).toHaveAttribute("title", fixtures.testingDescription);
    await expect(railLink.locator(".product-trust-dots")).toHaveAttribute("aria-label", new RegExp(fixtures.testingDescription.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    await expect(dot.locator("svg")).toBeAttached();
    // State symbols must remain perceptible against the actual rail in both themes.
    const contrast = await dot.evaluate(element => {
      const rgb = (value: string) => value.match(/[\d.]+/g)!.slice(0, 3).map(Number);
      const luminance = (color: number[]) => color.map(value => {
        const channel = value / 255;
        return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
      }).reduce((total, value, index) => total + value * [0.2126, 0.7152, 0.0722][index], 0);
      const fg = luminance(rgb(getComputedStyle(element).color));
      let background = element.parentElement!;
      while (getComputedStyle(background).backgroundColor === "rgba(0, 0, 0, 0)") {
        background = background.parentElement!;
      }
      const bg = luminance(rgb(getComputedStyle(background).backgroundColor));
      return (Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05);
    });
    expect(contrast).toBeGreaterThanOrEqual(3);
    if (state === "healthy" || state === "absent" || state === "failed") {
      await page.screenshot({ path: `../tmp/browser-smoke/sidebar-${state}-${theme}-${testInfo.project.name}.png`, fullPage: false });
    }
    if (state === "healthy") {
      const colors = await page.locator('.rail-product-link [data-lane="testing"]').evaluateAll(icons =>
        icons.map(icon => getComputedStyle(icon).color));
      expect(new Set(colors).size).toBe(colors.length);
    }
    expect(mutations).toEqual([]);
  });
}
}
