import { expect, test } from "@playwright/test";

for (const detail of [false, true]) test(`an open ${detail ? "environment" : "workspace"} expires monitor verification`, async ({ page }) => {
  await page.goto("/ui/products?fixture=products");
  const { product, expiry } = await page.evaluate(async () => {
    const modulePath = "/ui/src/dev-fixtures.ts";
    const { productsForFixture } = await import(modulePath);
    const product = productsForFixture("products")[0];
    const lane = product.environments.find((environment: { environment: string }) => environment.environment === "testing");
    return { product: product.product, expiry: Date.parse(lane.provenance.stale_after) };
  });
  await page.clock.install({ time: new Date(expiry - 1000) });
  await page.goto(`/ui/products/${product}${detail ? "/environments/testing" : ""}?fixture=products`);
  const signal = detail
    ? page.locator(".condition-tile").filter({ hasText: /^Runtime identity/i })
    : page.locator(".signal-tile").filter({ hasText: /^Testing/i });
  await expect(signal).toHaveAttribute("data-tone", detail ? "pass" : "verified");
  const sidebarDot = page.locator('.rail-product-link[data-active="true"] [data-lane="testing"]');
  await expect(sidebarDot).toHaveAttribute("data-tone", "verified");
  await page.clock.runFor(2000);
  await expect(signal).toHaveAttribute("data-tone", "warning");
  if (!detail) await expect(signal).toContainText("Review warning");
  await expect(signal).toContainText("Stale");
  await expect(sidebarDot).toHaveAttribute("data-tone", "warning");
});

test("historical runtime match cannot show green without current monitor proof", async ({ page }) => {
  await page.goto("/ui/products?fixture=products");
  const { products, detail, fixtureIdentity, incidents } = await page.evaluate(async () => {
    const modulePath = "/ui/src/dev-fixtures.ts";
    const fixtures = await import(modulePath);
    const products = fixtures.productsForFixture("products");
    const detail = fixtures.environmentForFixture("products", products[0].product, "testing");
    detail.provenance.freshness_status = "recorded";
    detail.topology.observed.placement.trust_state = "recorded";
    detail.topology.observed.placement.provenance.freshness_status = "recorded";
    detail.health_monitoring.checks[0].runtime_identity_status = "missing";
    return { products, detail, fixtureIdentity: fixtures.fixtureIdentity,
      incidents: fixtures.incidentsForFixture("products", detail.product, "testing"),
    };
  });
  await page.clock.install({ time: new Date(detail.provenance.refreshed_at) });
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "csrf", identity: fixtureIdentity } }));
  await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products } }));
  await page.route(`**/v1/products/${detail.product}`, route => route.fulfill({ json: { status: "ok", trace_id: "product", product: products[0] } }));
  await page.route(`**/v1/products/${detail.product}/environments/testing`, route => route.fulfill({ json: { status: "ok", trace_id: "environment", environment: detail } }));
  await page.route(`**/v1/products/${detail.product}/environments/testing/public-ingress/incidents`, route => route.fulfill({ json: { status: "ok", trace_id: "incidents", incident_list: incidents } }));
  await page.goto(`/ui/products/${detail.product}/environments/testing`);
  const signal = page.locator(".condition-tile").filter({ hasText: /^Runtime identity/i });
  await expect(signal).toContainText("Match");
  await expect(signal).toContainText("Recorded");
  await expect(signal).toHaveAttribute("data-tone", "warning");
});
