import { expect, test } from "@playwright/test";

for (const outcome of ["saved", "uncertain", "uncertain-stale", "leftover", "private", "endpoint-changed", "removed-check", "refresh-during-review", "stale", "denied", "readback-failed", "read-only"]) {
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
    if (["private", "endpoint-changed"].includes(outcome)) Object.assign(lane.health_monitoring.checks[0], { kind: "private_http", private_endpoint_key: "registered-private-health" });
    for (const product of fixtures.products) {
      product.environments = product.environments.filter((item, index, items) => items.findIndex(other =>
        other.context === item.context && other.environment === item.environment) === index);
    }
    const applies: Array<{ key: string; body: Record<string, unknown> }> = [];
    const plans: Record<string, unknown>[] = [];
    let profileReads = 0;
    let releaseReview: (() => Promise<void>) | null = null;
    await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session",
      csrf_token: "fixture-csrf", identity: { ...fixtures.identity, role: outcome === "read-only" ? "read_only" : "admin" } } }));
    await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: fixtures.products } }));
    await page.route("**/v1/products/atlas-commerce/environments/testing", route => route.fulfill({ json: {
      status: "ok", trace_id: "lane", environment: fixtures.environment } }));
    await page.route("**/operational-readiness?*", route => route.fulfill({ status: 503, json: {
      error: { code: "fixture_unavailable", message: "No readiness evidence in this isolated editor journey." } } }));
    await page.route("**/v1/product-profiles/atlas-commerce", route => {
      profileReads += 1;
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
        if (["uncertain", "uncertain-stale"].includes(outcome) && applies.length === 1) return route.abort();
        if (outcome === "uncertain-stale" && applies.length === 2) return route.fulfill({ status: 409, json: {
          error: { code: "stale", message: "Stored profile changed after review." } } });
        if (outcome === "stale" && applies.length === 1) return route.fulfill({ status: 409, json: {
          error: { code: "stale", message: "Stored profile changed after review." } } });
        lane.health_monitoring = { monitoring_intent: body.monitoring_intent, checks: [{ ...lane.health_monitoring.checks[0],
          enabled: body.enabled, require_runtime_identity: body.require_runtime_identity }] };
        if (outcome === "private") lane.health_monitoring.checks.unshift({ ...lane.health_monitoring.checks[0], name: "background-health" });
      } else plans.push(body);
      const response = { status: 202, json: { status: "accepted", trace_id: "monitoring", records: {},
        result: { status: "ok", mode: body.mode, product: body.product, context: body.context, instance: body.instance,
          check_name: body.check_name, requested_check_kind: body.check_kind, private_endpoint_key: body.private_endpoint_key,
          current_private_endpoint_key: outcome === "endpoint-changed" ? "new-registered-private-health" : body.private_endpoint_key,
          operation: outcome === "removed-check" ? "create" : "update", current_check_kind: outcome === "removed-check" ? null : body.check_kind,
          current_monitoring_intent: "prelaunch", requested_monitoring_intent: body.monitoring_intent,
          current_enabled: false, requested_enabled: body.enabled,
          current_require_runtime_identity: false, requested_require_runtime_identity: body.require_runtime_identity,
          resolved_url: body.check_kind === "private_http" ? "" : lane.health_url, changed: true, applied: body.mode === "apply", reason: body.reason,
          plan_sha256: "a".repeat(64) } } };
      if (outcome === "refresh-during-review") {
        releaseReview = () => route.fulfill(response);
        return;
      }
      return route.fulfill(response);
    });
    if (outcome === "leftover") await page.evaluate(({ context }) => {
      sessionStorage.setItem(`launchplane:monitoring-request:atlas-commerce:${context}:testing:health-monitoring:apply`, JSON.stringify({
        product: "atlas-commerce", context, instance: "testing", check_name: "public-ingress", check_kind: "public_http",
        mode: "apply", monitoring_intent: "prelaunch", enabled: false, require_runtime_identity: false,
        private_endpoint_key: "", reason: "Abandoned older request", reviewed_plan_sha256: "b".repeat(64),
      }));
    }, { context: lane.context });
    await page.goto("/ui/products/atlas-commerce/environments/testing/actions");
    let panel = page.getByRole("region", { name: "Health monitoring", exact: true });
    const initialLaneStatus = await page.getByRole("status", { name: "Lane status" }).innerText();
    await expect(panel.getByLabel("Stored check")).toHaveValue("public-ingress");
    const review = panel.getByRole("button", { name: "Review monitoring change" });
    if (outcome === "read-only") {
      await expect(review).toBeDisabled();
      expect(plans).toHaveLength(0); expect(applies).toHaveLength(0);
      return;
    }
    await panel.getByRole("combobox", { name: "Monitoring intent", exact: true }).selectOption(["private", "endpoint-changed"].includes(outcome) ? "private" : "public");
    await panel.getByLabel("Check enabled", { exact: true }).check();
    await panel.getByLabel("Require runtime identity", { exact: true }).check();
    await panel.getByLabel("Reason", { exact: true }).fill("Enable the approved testing monitor.");
    await review.click();
    if (outcome === "refresh-during-review") {
      await expect.poll(() => releaseReview !== null).toBe(true);
      const readsBeforeRefresh = profileReads;
      await page.getByRole("button", { name: "Refresh current evidence" }).click();
      await expect.poll(() => profileReads).toBeGreaterThan(readsBeforeRefresh);
      await (releaseReview as unknown as () => Promise<void>)();
      await expect(panel).toHaveAttribute("aria-busy", "false");
      await expect(panel.getByRole("button", { name: "Apply reviewed monitoring" })).toHaveCount(0);
      expect(applies).toHaveLength(0);
      return;
    }
    if (outcome === "denied") {
      await expect(panel.getByRole("alert")).toContainText("Exact lane monitoring plan denied");
      await expect(panel.getByRole("button", { name: "Apply reviewed monitoring" })).toHaveCount(0);
      expect(applies).toHaveLength(0);
      return;
    }
    if (outcome === "removed-check") {
      await expect(panel.getByRole("status", { name: "Monitoring change result" })).toContainText("stored check changed or was removed");
      await expect(panel.getByRole("button", { name: "Apply reviewed monitoring" })).toHaveCount(0);
      expect(applies).toHaveLength(0);
      lane.health_monitoring.checks = [];
      await page.getByRole("button", { name: "Refresh current evidence" }).click();
      await expect(panel.getByLabel("Stored check")).toHaveValue("");
      return;
    }
    if (outcome === "endpoint-changed") {
      await expect(panel.getByRole("button", { name: "Apply reviewed monitoring" })).toHaveCount(0);
      await expect(panel.getByRole("status", { name: "Monitoring change result" })).toContainText("stored private endpoint changed");
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
    if (outcome === "saved") {
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: testInfo.outputPath("monitoring-page-reviewed.png"), fullPage: true });
    }
    await panel.getByRole("button", { name: "Apply reviewed monitoring" }).click();
    if (["uncertain", "uncertain-stale"].includes(outcome)) {
      await expect(panel.getByRole("button", { name: "Retry original monitoring request" })).toBeEnabled();
      await expect(panel.getByLabel("Reason", { exact: true })).toBeDisabled();
      await page.reload();
      panel = page.getByRole("region", { name: "Health monitoring", exact: true });
      await panel.getByRole("button", { name: "Retry original monitoring request" }).click();
      await expect.poll(() => applies.length).toBe(2);
      expect(applies[1]).toEqual(applies[0]);
      if (outcome === "uncertain-stale") {
        await expect(panel.getByLabel("Reason", { exact: true })).toBeDisabled();
        await panel.getByRole("button", { name: "Retry original monitoring request" }).click();
        await expect.poll(() => applies.length).toBe(3);
        expect(applies[2]).toEqual(applies[0]);
      }
    }
    if (outcome === "stale") {
      await expect(panel.getByRole("alert")).toContainText("Stored profile changed after review");
      await expect(panel.getByLabel("Reason", { exact: true })).toBeEnabled();
      await expect(panel.getByRole("button", { name: "Apply reviewed monitoring" })).toHaveCount(0);
      return;
    }
    await expect(panel).toHaveAttribute("aria-busy", "false");
    await expect(panel.getByRole("status", { name: "Monitoring change result" })).toContainText(outcome === "readback-failed" ? "readback not confirmed" : "saved and read back");
    if (outcome === "private") await expect(panel.getByLabel("Stored check")).toHaveValue("public-ingress");
    await expect(page.getByRole("status", { name: "Lane status" })).toHaveText(initialLaneStatus);
    expect(applies[0].key).toBeTruthy();
    expect(applies[0].body).toEqual({ ...plans.at(-1), mode: "apply", reviewed_plan_sha256: "a".repeat(64) });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await panel.screenshot({ path: testInfo.outputPath(`monitoring-${outcome}.png`) });
  });
}
