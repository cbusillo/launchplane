import { expect, test } from "@playwright/test";

for (const outcome of ["success", "uncertain", "stale"]) {
  const uncertain = outcome === "uncertain";
  test(`service restart reviews identity and handles ${outcome}`, async ({ page }, testInfo) => {
    await page.goto("/ui/products?fixture=empty");
    const fixtures = await page.evaluate(async () => {
      const module = await import("/ui/src/dev-fixtures.ts");
      return { identity: module.fixtureIdentity, products: module.productsForFixture("products"),
        environment: module.environmentForFixture("products", "atlas-commerce", "testing") };
    });
    const environment = fixtures.environment!;
    for (const product of fixtures.products) {
      product.environments = product.environments.filter((lane, index, lanes) =>
        lanes.findIndex(other => other.context === lane.context && other.environment === lane.environment) === index);
    }
    environment.driver_id = "odoo";
    environment.available_actions.push({ action_id: "service_restart", label: "Restart a service (same version)",
      description: "Restart current artifact.", safety: "mutation", scope: "instance", enabled: true,
      disabled_reasons: [], trust_state: "recorded", method: "POST", route_path: "/v1/drivers/odoo/service-restart",
      authz_action: "live_target_runtime.apply", alternate_authz_actions: [] });
    await page.route("**/v1/auth/session", route => route.fulfill({ json: {
      status: "ok", trace_id: "session", csrf_token: "fixture-csrf", identity: fixtures.identity,
    } }));
    await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: fixtures.products } }));
    await page.route("**/v1/products/atlas-commerce/environments/testing", route => route.fulfill({ json: { status: "ok", trace_id: "lane", environment } }));
    await page.route("**/operational-readiness?*", route => route.fulfill({ status: 503, json: {
      error: { code: "fixture_readiness_unavailable", message: "Readiness is unavailable in this isolated restart journey." },
    } }));
    const applies: Array<{ key: string; body: unknown }> = [];
    await page.route("**/v1/drivers/odoo/service-restart", async route => {
      const body = route.request().postDataJSON();
      expect(route.request().headers()["x-csrf-token"]).toBe("fixture-csrf");
      if (body.mode === "apply") {
        applies.push({ key: route.request().headers()["idempotency-key"], body });
        if (uncertain && applies.length === 1) { await route.abort(); return; }
        if (outcome === "stale" && applies.length === 1) {
          await route.fulfill({ status: 409, json: { error: { code: "restart_identity_changed", message: "Service identity changed." } } }); return;
        }
      }
      await route.fulfill({ json: { status: "accepted", trace_id: "restart", records: {}, result: {
        status: body.mode === "dry-run" ? "ready" : "pass", plan_sha256: "a".repeat(64),
        plan: { artifact_id: "fixture-current-artifact", service: body.service, before: { container_id: "b".repeat(64) } },
        after: null, error_message: "",
      } } });
    });
    await page.goto("/ui/products/atlas-commerce/environments/testing/actions");
    let panel = page.getByRole("region", { name: "Restart on the same version" });
    await expect(panel).toBeVisible();
    const inspect = panel.getByRole("button", { name: "Inspect restart", exact: true });
    await expect(inspect).toBeDisabled();
    await panel.getByLabel("Reason", { exact: true }).fill("Recover an isolated worker.");
    await inspect.click();
    await expect(panel.getByText(/Current artifact: fixture-current-artifact/)).toBeVisible();
    await expect(panel.getByRole("button", { name: "Restart web (same version)", exact: true })).toBeDisabled();
    await panel.getByLabel("Reason", { exact: true }).fill("Updated reason.");
    await expect(panel.getByRole("button", { name: "Restart web (same version)", exact: true })).toHaveCount(0);
    await inspect.click();
    await panel.getByRole("checkbox").check();
    await panel.screenshot({ path: testInfo.outputPath("restart-reviewed.png") });
    await panel.getByRole("button", { name: "Restart web (same version)", exact: true }).click();
    if (uncertain) {
      await expect(panel.getByRole("button", { name: "Resume existing restart request" })).toBeEnabled();
      await page.reload();
      panel = page.getByRole("region", { name: "Restart on the same version" });
      await expect(panel.getByLabel("Reason", { exact: true })).toBeDisabled();
      await panel.getByRole("button", { name: "Resume existing restart request" }).click();
      await expect.poll(() => applies.length).toBe(2);
      expect(applies[1]).toEqual(applies[0]);
    } else if (outcome === "stale") {
      await expect(panel.getByRole("status")).toContainText("refused before a service change");
      await expect(panel.getByLabel("Reason", { exact: true })).toBeEnabled();
      await expect(panel.getByRole("button", { name: "Resume existing restart request" })).toHaveCount(0);
      await inspect.click();
      await panel.getByRole("checkbox").check();
      await panel.getByRole("button", { name: "Restart web (same version)", exact: true }).click();
    }
    await expect(panel.getByRole("status")).toContainText("Restart verified. Same version; service healthy.");
    expect(applies[0].key).toBeTruthy();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await panel.screenshot({ path: testInfo.outputPath(`restart-${uncertain ? "resumed" : "verified"}.png`) });
  });
}
