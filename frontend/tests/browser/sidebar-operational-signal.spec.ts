import { setClockTime, expect, test } from "./state";
import { smokeArtifact } from "../../browser-smoke-paths.mjs";

const cases = ["healthy", "failed", "delivery-warning", "recovery-warning", "stale", "unknown", "absent"] as const;

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
      const freshen = (value: unknown) => {
        if (!value || typeof value !== "object") return;
        const record = value as Record<string, unknown>;
        if ("stale_after" in record) record.stale_after = new Date(now + 60_000).toISOString();
        Object.values(record).forEach(freshen);
      };
      freshen(lane);
      lane.provenance.freshness_status = "verified";
      lane.warnings = [];
      lane.topology.warnings = [];
      lane.health_monitoring.open_incidents = [];
      if (state === "delivery-warning" || state === "recovery-warning") lane.environment = "prod";
      if (state === "delivery-warning") {
        const incident = structuredClone(module.productsForFixture("products")[0].environments.find((value: { environment: string }) => value.environment === "prod").health_monitoring.open_incidents[0]);
        Object.assign(incident, { severity: "warning", check_kind: "provider", failure_layer: "provider", check_name: "production-delivery-example", failure_code: "production_delivery_failed" });
        lane.health_monitoring.open_incidents = [incident];
      }
      if (state === "recovery-warning") lane.health_monitoring.checks.push({ ...structuredClone(lane.health_monitoring.checks[0]), name: "Recovery path", advisory: true, status: "fail", summary: "Recovery route is unavailable" });
      const tone = state === "healthy" ? "verified" : state === "failed" ? "danger" : state === "unknown" || state === "absent" ? "missing" : state === "stale" ? "stale" : "warning";
      lane.lane_status = module.fixtureLaneStatus(tone);
      lane.lane_status.transitions = [];
      if (state === "delivery-warning") {
        lane.lane_status.current.warning_reasons = ["Production delivery failed; the current production release is healthy"];
        lane.lane_status.current.release_healthy = true;
      }
      product.environments = state === "absent" ? [] : [lane];
      const products = [product];
      if (state === "healthy") {
        for (const extraState of ["warning", "stale", "danger", "missing"] as const) {
          const extra = structuredClone(product);
          extra.product = `example-${extraState}`;
          extra.display_name = `${extraState[0].toUpperCase() + extraState.slice(1)} example`;
          extra.environments[0].lane_status = module.fixtureLaneStatus(extraState);
          extra.environments[0].lane_status.transitions = [];
          products.push(extra);
        }
      }
      const laneLabel = lane.environment === "prod" ? "Production" : "Testing";
      const detail = Object.assign(module.environmentForFixture("products", product.product, lane.environment), lane);
      return { detail, product, products, identity: module.fixtureIdentity, now, laneLabel, laneName: lane.environment, testingDescription: sidebarStatus(product.environments[0] ?? null, laneLabel).description };
    }, state);
    await setClockTime(page, fixtures.now);
    if (theme === "light") await page.getByRole("button", { name: "Switch to light theme" }).click();
    const mutations: string[] = [];
    page.on("request", request => {
      if (request.url().includes("/v1/") && request.method() !== "GET") mutations.push(request.url());
    });
    await page.route("**/v1/**", route => route.fulfill({ status: 403, json: { status: "error", trace_id: "unused-read", error: { code: "authorization_denied", message: "Fixture read unavailable" } } }));
    await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "csrf", identity: fixtures.identity } }));
    await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: fixtures.products } }));
    await page.route(`**/v1/products/${fixtures.product.product}`, route => route.fulfill({ json: { status: "ok", trace_id: "product", product: fixtures.product } }));
    await page.route(new RegExp(`/v1/products/${fixtures.product.product}/environments/${fixtures.laneName}(?:\\?.*)?$`), route => route.fulfill({ json: { status: "ok", trace_id: "environment", environment: fixtures.detail } }));
    await page.goto("/ui/products");
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    const railLink = page.locator(".rail-product-link").filter({ hasText: fixtures.product.display_name });
    await page.getByRole("list", { name: "Launchplane products" })
      .getByRole("link", { name: new RegExp(fixtures.product.display_name) }).click();
    await expect(page.getByRole("heading", { level: 1, name: fixtures.product.display_name })).toBeVisible();
    const dot = railLink.locator(`[data-lane="${fixtures.laneName}"]`);
    const signal = page.locator(".signal-tile").filter({ hasText: new RegExp(`^${fixtures.laneLabel}`, "i") });
    const healthy = state === "healthy";
    const tone = healthy ? "verified" : state === "failed" ? "danger" : state === "stale" || state === "delivery-warning" || state === "recovery-warning" ? "warning" : "missing";
    await expect(signal).toHaveAttribute("data-tone", tone);
    await expect(dot).toHaveAttribute("data-tone", state === "stale" ? "stale" : tone);
    await expect(dot).toHaveAttribute("title", fixtures.testingDescription);
    await expect(railLink.locator(".product-trust-dots")).toHaveAttribute("aria-label", new RegExp(fixtures.testingDescription.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    await expect(dot.locator("svg")).toBeAttached();
    if (state === "delivery-warning") {
      await expect(page.locator(".evidence-warning")).toHaveCount(0);
      await expect(page.getByRole("link", { name: "Inspect incident", exact: true })).toHaveAttribute("href", new RegExp(`/prod#incident-history$`));
    }
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
    if (state === "healthy" || state === "absent" || state === "failed" || state === "delivery-warning") {
      await page.screenshot({ path: smokeArtifact(`sidebar-${state}-${theme}-${testInfo.project.name}.png`), fullPage: false });
    }
    if (state === "healthy") {
      const signals = await page.locator('.rail-product-link [data-lane="testing"]').evaluateAll(icons =>
        icons.map(icon => `${getComputedStyle(icon).color};${icon.querySelector("svg")?.innerHTML}`));
      expect(new Set(signals).size).toBe(signals.length);
    }
    if (state === "recovery-warning") {
      await page.goto(`/ui/products/${fixtures.product.product}/environments/${fixtures.laneName}`);
      const monitoring = page.locator(".condition-tile").filter({ has: page.locator("span").getByText("Monitoring", { exact: true }) });
      await expect(monitoring).toHaveAttribute("data-tone", "warning");
    }
    expect(mutations).toEqual([]);
  });
}
}
