import { expect, test } from "@playwright/test";

test("declared no website stays reviewable and a failed private check still turns red", async ({ page }) => {
  await page.goto("/ui/products?fixture=products");
  const { products, detail, fixtureIdentity, incidents } = await page.evaluate(async () => {
    const modulePath = "/ui/src/dev-fixtures.ts";
    const fixtures = await import(modulePath);
    const products = fixtures.productsForFixture("products");
    const detail = fixtures.environmentForFixture("products", products[0].product, "testing");
    const lane = products[0].environments.find((lane: {environment: string}) => lane.environment === "testing");
    products[0].warnings = [];
    for (const environment of products[0].environments) {
      environment.warnings = [];
      environment.topology.warnings = [];
    }
    for (const environment of [lane, detail]) {
      environment.warnings = [];
      environment.base_url = "";
      environment.health_url = "";
      environment.topology.desired.base_url = "";
      environment.topology.desired.health_url = "";
      environment.topology.desired.domains = [];
      environment.topology.desired.public_website = "none";
      environment.topology.warnings = [{ code: "public_website_not_applicable", severity: "info", scope: "authority", detail: "No public website declared; public ingress is not applicable.", domain_name: "" }];
      environment.topology.observed.tls_domains = [];
      environment.topology.observed.ingress.probe_effective = false;
      Object.assign(environment.public_ingress, { monitoring_intent: "private", incident_eligible: false, status: "not_expected", summary: "Private monitoring is authoritative." });
      environment.health_monitoring.monitoring_intent = "private";
      environment.health_monitoring.public_incident_eligible = false;
      environment.health_monitoring.checks = [environment.health_monitoring.checks[0]];
      Object.assign(environment.health_monitoring.checks[0], { name: "private-runtime", kind: "private_http", enabled: true, probe_effective: true, status: "pass", incident_status: "", summary: "Discord connection and event processing verified." });
    }
    return { products, detail, fixtureIdentity: fixtures.fixtureIdentity,
      incidents: fixtures.incidentsForFixture("products", detail.product, "testing") };
  });
  await page.clock.install({ time: new Date(detail.provenance.refreshed_at) });
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "csrf", identity: fixtureIdentity } }));
  await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products } }));
  await page.route(`**/v1/product-profiles/${detail.product}`, route => route.fulfill({ status: 404, json: { status: "error", trace_id: "profile", error: { code: "not_found", message: "No profile in this browser fixture" } } }));
  await page.route(`**/v1/products/${detail.product}`, route => route.fulfill({ json: { status: "ok", trace_id: "product", product: products[0] } }));
  await page.route(`**/v1/products/${detail.product}/environments/testing`, route => route.fulfill({ json: { status: "ok", trace_id: "environment", environment: detail } }));
  await page.route(`**/v1/products/${detail.product}/environments/testing/public-ingress/incidents`, route => route.fulfill({ json: { status: "ok", trace_id: "incidents", incident_list: incidents } }));
  await page.goto(`/ui/products/${detail.product}`);
  const signal = page.locator(".signal-tile").filter({ hasText: /^Testing/i });
  await expect(signal).toHaveAttribute("data-tone", "verified");
  const warningTile = page.locator(".signal-tile").filter({ hasText: /^Warnings/i });
  await expect(warningTile).not.toHaveAttribute("data-tone", "warning");
  await expect(warningTile).toContainText("0 recorded");
  await expect(page.locator("#warning-summary h2")).toHaveText("Information only");
  await expect(page.getByText("Review recorded warnings", { exact: true })).toHaveCount(0);
  await page.getByRole("link", { name: /Inspect environment/i }).first().click();
  await expect(page.getByText("None declared · public ingress not applicable", { exact: true })).toBeVisible();
  await expect(page.locator('#topology-warnings [data-severity="info"]')).toContainText("No public website declared");
  await expect(page.locator("#tls-evidence")).toContainText("Public TLS is not applicable");
  await expect(page.getByText("TLS evidence missing", { exact: true })).toHaveCount(0);
  await expect(page.locator(".diagnosis-callout")).toHaveCount(0);
  await page.screenshot({ path: `../tmp/browser-smoke/no-website-${test.info().project.name}.png`, fullPage: true });
  products[0].environments.find((lane: {environment: string}) => lane.environment === "testing")!.health_monitoring.checks[0].status = "fail";
  await page.goto(`/ui/products/${detail.product}`);
  await expect(signal).toHaveAttribute("data-tone", "danger");
  // An old public failure stays available as history after the check is disabled.
  detail.topology.desired.public_website = "required";
  detail.health_monitoring.monitoring_intent = "prelaunch";
  detail.health_monitoring.checks[0].probe_effective = false;
  detail.health_monitoring.checks[0].enabled = false;
  Object.assign(detail.public_ingress, { status: "fail", summary: "Historical invalid URL", incident_severity: "critical" });
  detail.topology.warnings = [
    { code: "public_ingress_failure", severity: "info", scope: "observation", detail: "Historical failure of a disabled check", domain_name: "" },
    { code: "public_website_check_missing", severity: "warning", scope: "observation", detail: "Website check required", domain_name: "" },
  ];
  await page.goto(`/ui/products/${detail.product}/environments/testing`);
  await expect(page.locator(".condition-tile").filter({ hasText: /^Monitoring/i })).not.toHaveAttribute("data-tone", "danger");
  await expect(page.locator(".diagnosis-callout")).toHaveAttribute("data-severity", "warning");
  await expect(page.getByText("Public observation history", { exact: true })).toBeVisible();
  await expect(page.locator('#topology-warnings [data-severity="info"]')).toContainText("Historical failure");
});

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
  await expect(sidebarDot).toHaveAttribute("data-trust", "verified");
  await page.clock.runFor(2000);
  await expect(signal).toHaveAttribute("data-tone", "warning");
  if (!detail) await expect(signal).toContainText("Review warning");
  await expect(signal).toContainText("Stale");
  await expect(sidebarDot).toHaveAttribute("data-trust", "stale");
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
