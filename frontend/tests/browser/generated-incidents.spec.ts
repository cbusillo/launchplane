import { expect, test } from "@playwright/test";

test("canonical generated incidents render once and disappear after resolution", async ({ page }, testInfo) => {
  await page.goto("/ui/products?fixture=products");
  const fixtures = await page.evaluate(async () => {
    const modulePath = "/ui/src/dev-fixtures.ts";
    const module = await import(modulePath);
    const products = module.productsForFixture("products");
    const product = products[0];
    const detail = module.environmentForFixture("products", product.product, "testing");
    const template = product.environments.find((lane: { environment: string }) => lane.environment === "prod").health_monitoring.open_incidents[0];
    const cases = [
      ["tls-alias.example.test", "tls", "tls_expired", "critical", "acknowledged"],
      ["monitor-cadence:public_http:public-ingress", "provider", "monitor_run_missed", "critical", "silenced"],
      ["launchplane-deploy-fence", "provider", "deploy_fence_held", "warning", "active"],
    ];
    const incidents = cases.map(([check_name, check_kind, failure_code, severity, notification_state], index) => ({
      ...template, incident_id: `generated-${index}`, check_name, check_kind, failure_code, severity, notification_state,
      product: product.product, environment: "testing", context: detail.context,
      summary: `Canonical ${failure_code} evidence`,
    }));
    product.environments = [product.environments.find((lane: { environment: string }) => lane.environment === "testing")];
    product.environments[0].health_monitoring.open_incidents = incidents;
    detail.health_monitoring.open_incidents = incidents;
    return { products: [product], detail, incidents, identity: module.fixtureIdentity };
  });
  let resolved = false;
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", identity: fixtures.identity, csrf_token: "csrf" } }));
  await page.route("**/v1/products", route => {
    const products = structuredClone(fixtures.products);
    if (resolved) products[0].environments[0].health_monitoring.open_incidents = [];
    return route.fulfill({ json: { status: "ok", trace_id: "products", products } });
  });
  await page.route(`**/v1/products/${fixtures.detail.product}`, route => route.fulfill({ json: { status: "ok", trace_id: "product", product: fixtures.products[0] } }));
  await page.route(`**/v1/products/${fixtures.detail.product}/environments/testing`, route => route.fulfill({ json: { status: "ok", trace_id: "lane", environment: fixtures.detail } }));
  await page.route("**/public-ingress/incidents", route => route.fulfill({ json: {
    status: "ok", trace_id: "incidents", incident_list: {
      product: fixtures.detail.product, environment: "testing", incidents: fixtures.incidents,
      provenance: fixtures.detail.provenance, trust_state: "recorded",
    },
  } }));
  await page.route("**/public-ingress/incidents/*", route => route.fulfill({ json: {
    status: "ok", trace_id: "detail", incident: {
      incident: fixtures.incidents[0], observations: [], events: [], reminders: [],
      notification_attempts: [], outbox_deliveries: [], provenance: fixtures.detail.provenance,
    },
  } }));
  await page.goto("/ui/products");
  const overview = page.getByRole("region", { name: "Active public ingress incidents" });
  await expect(overview.getByRole("heading", { name: `${fixtures.incidents.length} open incidents` })).toBeVisible();
  await expect(overview.locator("li")).toHaveCount(fixtures.incidents.length);
  for (const incident of fixtures.incidents) {
    const row = overview.locator("li").filter({ hasText: incident.summary });
    await expect(row).toHaveAttribute("data-severity", incident.severity);
    await expect(row.getByRole("link", { name: "Inspect incident" })).toHaveAttribute("href", /environments\/testing#incident-history$/);
  }
  await expect(overview).toContainText("Acknowledged");
  await expect(overview).toContainText("Silenced");
  await page.screenshot({ path: testInfo.outputPath("generated-incidents.png"), fullPage: true });
  await overview.getByRole("link", { name: "Inspect incident" }).first().click();
  await expect(page.getByRole("region", { name: "Public ingress incidents", exact: true })).toBeVisible();
  await expect(page.locator(".incident-list-item")).toHaveCount(fixtures.incidents.length);
  resolved = true;
  await page.goto("/ui/products");
  await expect(overview.getByRole("heading", { name: "No open incidents recorded" })).toBeVisible();
  await expect(overview.locator("li")).toHaveCount(0);
});
