import { expect, test } from "@playwright/test";

for (const outcome of ["rebound", "denied", "lost-response", "read-only"]) {
  test(`backup credential references handle ${outcome}`, { tag: "@layout" }, async ({ page }, testInfo) => {
    await page.goto("/ui/products?fixture=empty");
    const fixtures = await page.evaluate(async () => {
      const m = await import("/ui/src/dev-fixtures.ts");
      return { identity: m.fixtureIdentity, products: m.productsForFixture("products"), environment: m.environmentForFixture("products", "atlas-commerce", "prod")! };
    });
    let applied = false;
    const requests: Array<{ key: string; body: any }> = [];
    const keys = ["PRODUCTION_BACKUP_SSH_PRIVATE_KEY", "PRODUCTION_BACKUP_SSH_KNOWN_HOSTS"];
    await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "fixture-csrf", identity: { ...fixtures.identity, role: outcome === "read-only" ? "read_only" : "admin" } } }));
    await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: fixtures.products } }));
    await page.route("**/v1/products/atlas-commerce/environments/prod", route => route.fulfill({ json: { status: "ok", trace_id: "lane", environment: fixtures.environment } }));
    await page.route("**/v1/products/atlas-commerce/environments/prod/config-status", route => route.fulfill({ json: { status: "ok", trace_id: "requirements", config_status: { product: "atlas-commerce", context: fixtures.environment.context, environment: "prod", production_backup_authorities: [{ promotion_action: "generic_web_prod_promotion.execute" }] } } }));
    await page.route("**/v1/product-profiles/atlas-commerce", route => route.fulfill({ json: { status: "ok", trace_id: "profile", profile: { lanes: [] } } }));
    await page.route("**/promotion-status", route => route.fulfill({ status: 503, json: { error: { code: "fixture", message: "Outside this isolated credential test." } } }));
    await page.route("**/operational-readiness?*", route => route.fulfill({ status: 503, json: { error: { code: "fixture", message: "Outside this isolated credential test." } } }));
    await page.route("**/backup-worker-bindings", route => route.fulfill({ json: { status: "accepted", trace_id: "bindings", records: {}, result: { product: "atlas-commerce", context: fixtures.environment.context, instance: "prod", credentials: keys.map((key, index) => ({ binding_id: `stored-${index}`, binding_key: applied ? key : `LEGACY_${key}`, version_id: `existing-version-${index}` })) } } }));
    await page.route("**/backup-worker-bindings/apply", route => {
      expect(route.request().headers()["x-csrf-token"]).toBe("fixture-csrf");
      const body = route.request().postDataJSON();
      requests.push({ key: route.request().headers()["idempotency-key"] ?? "", body });
      expect(body).not.toHaveProperty("value"); expect(body).not.toHaveProperty("secrets");
      expect(body.bindings).toEqual(Object.fromEntries(keys.map((key, index) => [key, `stored-${index}`])));
      if (body.mode === "apply") {
        expect(body.reviewed_plan_sha256).toBe("c".repeat(64));
        if (outcome === "denied") return route.fulfill({ status: 403, json: { trace_id: "denial", error: { code: "authorization_denied", message: "Scoped credential apply is denied." } } });
        applied = true;
        if (outcome === "lost-response" && requests.filter(item => item.body.mode === "apply").length === 1) return route.abort();
      }
      return route.fulfill({ json: { status: "accepted", trace_id: "review", records: {}, result: { plan_sha256: "c".repeat(64), changed: true, applied: body.mode === "apply" } } });
    });
    await page.route("**/v1/production-backup-authority/preflight?*", route => {
      expect(route.request().method()).toBe("GET");
      return route.fulfill({ json: { status: "accepted", trace_id: "preflight", records: {}, result: { status: "pass", policy_revision: 1, boundary_status: "match_capture_only", storage_status: "active_pbs", capture_started: false } } });
    });
    await page.goto("/ui/products/atlas-commerce/environments/prod/actions");
    const panel = page.getByRole("region", { name: "Production backup credentials", exact: true });
    const credential = panel.getByRole("combobox", { name: "Capture SSH credential", exact: true });
    await expect(credential).toBeVisible();
    if (outcome === "read-only") {
      await expect(credential).toBeDisabled();
      await expect(panel.getByRole("button", { name: "Use these existing credentials", exact: true })).toBeDisabled();
      await panel.getByRole("button", { name: "Check capture boundary and PBS" }).click();
      await expect(panel.getByRole("status", { name: "Backup credential result" })).toBeVisible();
      await expect(panel.getByRole("alert")).toHaveCount(0);
      expect(requests).toHaveLength(0); return;
    }
    await credential.selectOption("stored-0");
    await panel.getByRole("combobox", { name: "SSH host verification", exact: true }).selectOption("stored-1");
    await panel.getByRole("button", { name: "Preview credential bindings" }).click();
    const apply = panel.getByRole("button", { name: "Use these existing credentials", exact: true });
    await expect(apply).toBeEnabled(); await apply.click();
    if (outcome === "denied") {
      await expect(panel.locator('.operation-notice[data-phase="failed"]')).toBeVisible();
      await expect(apply).toBeDisabled(); await expect(credential).toBeEnabled(); return;
    }
    if (outcome === "lost-response") {
      await expect(panel.getByRole("button", { name: "Retry original credential rebinding" })).toBeEnabled();
      await page.reload();
      await expect(credential).toBeDisabled();
      await panel.getByRole("button", { name: "Retry original credential rebinding" }).click();
      await expect.poll(() => requests.filter(item => item.body.mode === "apply").length).toBe(2);
      const attempts = requests.filter(item => item.body.mode === "apply"); expect(attempts[0]).toEqual(attempts[1]);
    }
    await expect(panel.getByRole("status", { name: "Backup credential result" })).toBeVisible();
      await expect(panel.getByRole("alert")).toHaveCount(0);
    await panel.getByRole("button", { name: "Check capture boundary and PBS" }).click();
    await expect(panel.getByRole("status", { name: "Backup credential result" })).toBeVisible();
      await expect(panel.getByRole("alert")).toHaveCount(0);
    expect(requests.filter(item => item.body.mode === "apply").every(item => item.key)).toBe(true);
    await panel.screenshot({ path: testInfo.outputPath(`backup-credentials-${outcome}.png`) });
  });
}
