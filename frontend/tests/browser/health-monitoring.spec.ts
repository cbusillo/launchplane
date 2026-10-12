import { expect, test } from "@playwright/test";

for (const outcome of ["saved", "uncertain", "stale", "denied", "readback-failed", "read-only"]) {
  test(`monitoring editor reviews stored policy and handles ${outcome}`, { tag: "@layout" }, async ({ page }, testInfo) => {
    await page.goto("/ui/products?fixture=empty");
    const fixtures = await page.evaluate(async () => {
      const module = await import("/ui/src/dev-fixtures.ts");
      return { identity: module.fixtureIdentity, products: module.productsForFixture("products"),
        environment: module.environmentForFixture("products", "atlas-commerce", "testing")! };
    });
    const lane = {
      context: fixtures.environment.context, instance: "testing",
      health_url: "https://testing.example.com/health", base_url: "https://testing.example.com",
      health_monitoring: { monitoring_intent: "prelaunch", checks: [{ name: "public-ingress", kind: "public_http",
        enabled: false, require_runtime_identity: false, url: "", private_endpoint_key: "" }] },
    };
    for (const product of fixtures.products) {
      product.environments = product.environments.filter((item, index, items) => items.findIndex(other =>
        other.context === item.context && other.environment === item.environment) === index);
    }
    const applies: Array<{ key: string; body: Record<string, unknown> }> = [];
    const plans: Record<string, unknown>[] = [];
    await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session",
      csrf_token: "fixture-csrf", identity: { ...fixtures.identity, role: outcome === "read-only" ? "read_only" : "admin" } } }));
    await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: fixtures.products } }));
    await page.route("**/v1/products/atlas-commerce/environments/testing", route => route.fulfill({ json: {
      status: "ok", trace_id: "lane", environment: fixtures.environment } }));
    await page.route("**/operational-readiness?*", route => route.fulfill({ status: 503, json: {
      error: { code: "fixture_unavailable", message: "No readiness evidence in this isolated editor journey." } } }));
    await page.route("**/v1/product-profiles/atlas-commerce", route => {
      if (outcome === "readback-failed" && applies.length) return route.abort();
      return route.fulfill({ json: { status: "ok", trace_id: "profile", profile: { product: "atlas-commerce", lanes: [lane] } } });
    });
    await page.route("**/v1/product-profiles/health-monitoring/apply", async route => {
      const body = route.request().postDataJSON();
      expect(route.request().headers()["x-csrf-token"]).toBe("fixture-csrf");
      expect(body).not.toHaveProperty("url");
      expect(body).not.toHaveProperty("health_url");
      expect(body.product).toBe(fixtures.environment.product);
      expect(body.context).toBe(lane.context);
      expect(body.instance).toBe(lane.instance);
      if (outcome === "denied") return route.fulfill({ status: 403, json: {
        trace_id: "denied", error: { code: "authorization_denied", message: "Exact lane monitoring plan denied." } } });
      if (body.mode === "apply") {
        applies.push({ key: route.request().headers()["idempotency-key"], body });
        if (outcome === "uncertain" && applies.length === 1) return route.abort();
        if (outcome === "stale" && applies.length === 1) return route.fulfill({ status: 409, json: {
          error: { code: "stale", message: "Stored profile changed after review." } } });
        lane.health_monitoring = { monitoring_intent: body.monitoring_intent, checks: [{ ...lane.health_monitoring.checks[0],
          enabled: body.enabled, require_runtime_identity: body.require_runtime_identity }] };
      } else plans.push(body);
      return route.fulfill({ status: 202, json: { status: "accepted", trace_id: "monitoring", records: {},
        result: { status: "ok", mode: body.mode, product: body.product, context: body.context, instance: body.instance,
          check_name: body.check_name, requested_check_kind: body.check_kind, private_endpoint_key: "",
          current_monitoring_intent: "prelaunch", requested_monitoring_intent: body.monitoring_intent,
          current_enabled: false, requested_enabled: body.enabled,
          current_require_runtime_identity: false, requested_require_runtime_identity: body.require_runtime_identity,
          resolved_url: lane.health_url, changed: true, applied: body.mode === "apply", reason: body.reason,
          plan_sha256: "a".repeat(64) } } });
    });
    await page.goto("/ui/products/atlas-commerce/environments/testing/actions");
    let panel = page.getByRole("region", { name: "Health monitoring", exact: true });
    await expect(panel.getByLabel("Stored check")).toHaveValue("public-ingress");
    const review = panel.getByRole("button", { name: "Review monitoring change" });
    if (outcome === "read-only") {
      await expect(review).toBeDisabled();
      expect(plans).toHaveLength(0); expect(applies).toHaveLength(0);
      return;
    }
    await panel.getByRole("combobox", { name: "Monitoring intent", exact: true }).selectOption("public");
    await panel.getByLabel("Check enabled", { exact: true }).check();
    await panel.getByLabel("Require runtime identity", { exact: true }).check();
    await panel.getByLabel("Reason", { exact: true }).fill("Enable the approved testing monitor.");
    await review.click();
    if (outcome === "denied") {
      await expect(panel.getByRole("alert")).toContainText("Exact lane monitoring plan denied");
      await expect(panel.getByRole("button", { name: "Apply reviewed monitoring" })).toHaveCount(0);
      expect(applies).toHaveLength(0);
      return;
    }
    const apply = panel.getByRole("button", { name: "Apply reviewed monitoring" });
    await expect(apply).toBeDisabled();
    await expect(panel.getByText("Endpoint preserved", { exact: true })).toBeVisible();
    await panel.getByLabel("Reason", { exact: true }).fill("Approved testing monitoring; preserve endpoint.");
    await expect(apply).toHaveCount(0);
    await review.click();
    await panel.getByLabel("I confirm this monitoring change on testing.").check();
    await panel.screenshot({ path: testInfo.outputPath("monitoring-reviewed.png") });
    await panel.getByRole("button", { name: "Apply reviewed monitoring" }).click();
    if (outcome === "uncertain") {
      await expect(panel.getByRole("button", { name: "Retry original monitoring request" })).toBeEnabled();
      await expect(panel.getByLabel("Reason", { exact: true })).toBeDisabled();
      await page.reload();
      panel = page.getByRole("region", { name: "Health monitoring", exact: true });
      await panel.getByRole("button", { name: "Retry original monitoring request" }).click();
      await expect.poll(() => applies.length).toBe(2);
      expect(applies[1]).toEqual(applies[0]);
    }
    if (outcome === "stale") {
      await expect(panel.getByRole("alert")).toContainText("Stored profile changed after review");
      await expect(panel.getByLabel("Reason", { exact: true })).toBeEnabled();
      await expect(panel.getByRole("button", { name: "Apply reviewed monitoring" })).toHaveCount(0);
      return;
    }
    await expect(panel.getByRole("status")).toContainText(outcome === "readback-failed" ? "readback unavailable" : "saved and read back");
    if (outcome !== "readback-failed") await expect(panel.getByRole("status")).toContainText("remain unverified");
    expect(applies[0].key).toBeTruthy();
    expect(applies[0].body.reviewed_plan_sha256).toBe("a".repeat(64));
    expect(lane.health_url).toBe("https://testing.example.com/health");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await panel.screenshot({ path: testInfo.outputPath(`monitoring-${outcome}.png`) });
  });
}
