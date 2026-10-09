import { expect, test } from "@playwright/test";

for (const mode of ["missing", "stale", "fresh", "disabled", "inapplicable"] as const) {
  test(`incident empty state distinguishes ${mode} monitoring and recovers`, async ({ page }, testInfo) => {
    await page.goto("/ui/products?fixture=products");
    const fixture = await page.evaluate(async () => {
      const modulePath = "/ui/src/dev-fixtures.ts";
      const module = await import(modulePath);
      const product = module.productsForFixture("products")[0];
      product.environments = [product.environments[0]];
      const check = product.environments[0].health_monitoring.checks[0];
      product.environments[0].health_monitoring.checks = [check];
      check.incident_status = "none";
      check.incident_id = "";
      check.probe_effective = true;
      check.incident_eligible = true;
      return { product, identity: module.fixtureIdentity };
    });
    const start = Date.parse(fixture.product.environments[0].provenance.refreshed_at);
    await page.clock.install({ time: new Date(start) });
    const check = fixture.product.environments[0].health_monitoring.checks[0];
    check.trust_state = mode === "stale" ? "verified" : mode === "fresh" ? "verified" : "missing";
    check.provenance.stale_after = new Date(start + (mode === "stale" ? -1000 : 60_000)).toISOString();
    check.probe_effective = mode !== "disabled" && mode !== "inapplicable";
    check.incident_eligible = check.probe_effective;
    await page.route("**/v1/**", route => route.fulfill({ status: 403, json: { error: { code: "authorization_denied" } } }));
    await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", identity: fixture.identity, csrf_token: "fixture" } }));
    await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", products: [fixture.product] } }));
    await page.goto("/ui/products");
    const overview = page.getByRole("region", { name: "Active public ingress incidents" });
    await expect(overview).toContainText("No open incidents recorded");
    if (mode === "missing" || mode === "stale") await expect(overview).toContainText("monitoring evidence is incomplete");
    else {
      await expect(overview).not.toContainText("incomplete");
      await expect(overview).toContainText(mode === "fresh" ? "All incident-eligible health checks returned" : "No incident-eligible health checks are effective");
    }
    await page.screenshot({ path: `../tmp/browser-smoke/monitoring-${mode}-${testInfo.project.name}.png`, fullPage: true });
    check.probe_effective = true;
    check.incident_eligible = true;
    check.trust_state = "verified";
    check.provenance.stale_after = new Date(start + 60_000).toISOString();
    await page.getByRole("button", { name: "Refresh current evidence" }).click();
    await expect(overview).toContainText("All incident-eligible health checks returned");
    await expect(overview).not.toContainText("incomplete");
  });
}

for (const incidentEligible of [true, false]) {
  test(`environment incident empty state respects unsupported and eligibility ${incidentEligible}`, async ({ page }) => {
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
    fixture.detail.health_monitoring.checks = [fixture.detail.health_monitoring.checks[0]];
    fixture.detail.health_monitoring.checks[0].trust_state = "unsupported";
    fixture.detail.health_monitoring.checks[0].incident_eligible = incidentEligible;
    await page.route("**/v1/**", route => route.fulfill({ status: 403, json: { error: { code: "authorization_denied" } } }));
    await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", identity: fixture.identity, csrf_token: "fixture" } }));
    await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", products: [fixture.product] } }));
    await page.route(`**/v1/products/${fixture.product.product}`, route => route.fulfill({ json: { status: "ok", product: fixture.product } }));
    await page.route(`**/v1/products/${fixture.product.product}/environments/testing`, route => route.fulfill({ json: { status: "ok", environment: fixture.detail } }));
    await page.route(`**/v1/products/${fixture.product.product}/environments/testing/public-ingress/incidents`, route => route.fulfill({ json: { status: "ok", incident_list: fixture.incidents } }));
    await page.goto(`/ui/products/${fixture.product.product}/environments/testing`);
    const empty = page.locator(".incident-empty-state");
    await expect(empty).toHaveAttribute("data-incomplete", String(incidentEligible));
    await expect(empty).toContainText(incidentEligible ? "Incident evidence is incomplete" : "No incidents recorded");
  });
}
