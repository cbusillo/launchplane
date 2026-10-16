import { expect, test } from "./state";

test("Product Ops scopes evidence and preserves each distinct warning", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await page.goto("/ui/products?fixture=products");
  const { product, identity } = await page.evaluate(async () => {
    const modulePath = "/ui/src/dev-fixtures.ts";
    const module = await import(modulePath) as typeof import("../../src/dev-fixtures");
    return { product: structuredClone(module.productsForFixture("products")[0]), identity: module.fixtureIdentity };
  });
  product.trust_state = "missing";
  product.provenance.recorded_at = "2020-01-01T00:00:00Z";
  product.provenance.refreshed_at = product.provenance.recorded_at;
  product.warnings = [];
  product.preview.enabled = false;
  product.preview.trust_state = "unsupported";
  const production = product.environments.find(lane => lane.environment === "prod")!;
  const warning = { code: "missing_route_authority" as const, severity: "error" as const,
    scope: "authority" as const, detail: "The lane route record is absent.", domain_name: "" };
  for (const lane of product.environments) {
    lane.warnings = [];
    lane.topology.warnings = [];
  }
  production.warnings = [warning.detail, "A separate runtime problem."];
  production.topology.warnings = [warning];
  production.trust_state = "missing";
  production.topology.trust_state = "missing";
  const testing = product.environments.find(lane => lane.environment === "testing")!;
  const tlsObservation = structuredClone(production.topology.observed.tls_domains[0]);
  testing.topology.desired.public_website = "none";
  testing.base_url = "";
  testing.health_url = "";
  testing.topology.desired.domains = [];
  testing.topology.observed.tls_domains = [];
  testing.topology.warnings = [{ code: "public_website_not_applicable", severity: "info", scope: "authority",
    detail: "This product has no public website.", domain_name: "" }];
  await page.route("**/v1/**", route => route.fulfill({ status: 403, json: {
    status: "error", error: { code: "authorization_denied", message: "Unused fixture read" }, trace_id: "unused",
  } }));
  await page.route("**/v1/auth/session", route => route.fulfill({ json: {
    status: "ok", trace_id: "session", csrf_token: "csrf", identity,
  } }));
  await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: [product] } }));
  await page.route(`**/v1/products/${product.product}`, route => route.fulfill({ json: { status: "ok", trace_id: "product", product } }));
  await page.goto("/ui/products");
  const row = page.locator(".product-directory-row");
  await expect(row.locator(".directory-warnings > strong")).toHaveText("2");
  await expect(row.locator(".directory-product > .evidence-badge > small")).toHaveCount(0);
  await expect(row.locator(".directory-preview")).toContainText("Not applicable");
  await page.screenshot({ path: `../tmp/browser-smoke/product-evidence-directory-${testInfo.project.name}.png`, fullPage: true });
  await row.click();
  const warnings = page.locator("#warning-summary");
  await expect(warnings.locator('li:not([data-severity="info"])')).toHaveCount(2);
  await expect(warnings.getByText(warning.detail, { exact: true })).toHaveCount(1);
  await expect(warnings.locator('[data-severity="error"]')).toContainText(warning.detail);
  const privateLane = page.locator("#lane-testing");
  await expect(privateLane).toContainText("Not applicable");
  const tlsWarnings = Array.from({ length: 7 }, (_, index) => ({
    code: "tls_observation_missing" as const, scope: "tls" as const, severity: "warning" as const,
    detail: "No certificate observation for this domain.", domain_name: `alias-${index}.example.invalid`,
  }));
  production.topology.warnings = [warning, ...tlsWarnings];
  production.warnings = [warning.detail, ...tlsWarnings.map(item => item.detail), "A separate runtime problem."];
  await page.getByRole("button", { name: "Refresh current evidence" }).click();
  await expect(warnings.locator("li")).toHaveCount(tlsWarnings.length + 3);
  for (const item of tlsWarnings) await expect(warnings.locator("li").filter({ hasText: item.domain_name })).toBeVisible();
  // A no-website declaration does not erase an actual negative observation.
  tlsObservation.status = "expired";
  tlsObservation.summary = "The retained certificate has expired.";
  testing.topology.observed.tls_domains = [tlsObservation];
  await page.getByRole("button", { name: "Refresh current evidence" }).click();
  await expect(privateLane).toContainText(tlsObservation.summary);
  await page.screenshot({ path: `../tmp/browser-smoke/product-evidence-workspace-${testInfo.project.name}.png`, fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
