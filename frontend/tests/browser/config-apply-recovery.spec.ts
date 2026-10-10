import { expect, test } from "@playwright/test";

for (const form of ["runtime-settings", "managed-secrets"] as const) {
  for (const storageFailure of [false, true]) {
  test(`${form}: ${storageFailure ? "storage refusal sends no Apply" : "exact-request recovery after reload"}`, async ({ page }, testInfo) => {
    await page.goto(`/ui/products/atlas-commerce/environments/prod/${form}?fixture=products`);
    const data = await page.evaluate(async () => {
      const f = await import("/ui/src/dev-fixtures.ts");
      return { products: f.productsForFixture("products"), identity: f.fixtureIdentity,
        environment: f.environmentForFixture("products", "atlas-commerce", "prod"),
        config: f.configStatusForFixture("products", "atlas-commerce", "prod") };
    });
    const plans: unknown[] = [];
    const applies: Array<{ body: unknown; key: string }> = [];
    await page.route("**/v1/**", async route => {
      const path = new URL(route.request().url()).pathname;
      if (route.request().method() === "POST") {
        const body = route.request().postDataJSON();
        const applying = body.mode === "apply";
        if (applying) applies.push({ body, key: route.request().headers()["idempotency-key"] });
        else plans.push(body);
        if (applying ? applies.length === 1 : plans.length === 1) await route.fulfill({ status: 502, json: { trace_id: "gateway", error: {
          code: "gateway_error", message: "Gateway unavailable" } } });
        else {
          const response = await page.evaluate(async body => {
            const f = await import("/ui/src/dev-fixtures.ts");
            return f.applyProductEnvironmentConfigForFixture("products", "atlas-commerce", "prod", body);
          }, route.request().postDataJSON());
          await route.fulfill({ status: 202, json: applying ? {
            ...response, replayed: true, original_trace_id: "committed-apply" } : response });
        }
        return;
      }
      const response = path.endsWith("/auth/session") ? { csrf_token: "csrf", identity: data.identity }
        : path.endsWith("/config-status") ? { config_status: data.config }
        : path.endsWith("/environments/prod") ? { environment: data.environment }
        : path.endsWith("/products") ? { products: data.products }
        : path.endsWith("/owner-secret-inputs") ? { fields: [] }
        : { product: data.products[0] };
      await route.fulfill({ json: { status: "ok", trace_id: "read", ...response } });
    });
    await page.goto(`/ui/products/atlas-commerce/environments/prod/${form}`);
    const field = page.locator(".product-config-field").filter({ hasText:
      form === "runtime-settings" ? "PUBLIC_ORIGIN" : "SMTP_PASSWORD" });
    await field.getByRole("checkbox").check();
    const valueInput = field.locator("input:not([type=checkbox])");
    await valueInput.fill(form === "runtime-settings"
      ? "https://example.invalid" : "inert-secret");
    await page.getByLabel("Change reason").fill("First review");
    await page.getByRole("button", { name: "Run dry-run" }).click();
    await expect(page.getByText("Gateway unavailable", { exact: true })).toBeVisible();
    await page.getByLabel("Change reason").fill("Edited review");
    if (form === "managed-secrets") await valueInput.fill("inert-secret");
    if (storageFailure) await page.evaluate(() => {
      const original = Storage.prototype.setItem;
      Storage.prototype.setItem = function(key, value) {
        if (key.startsWith("launchplane.browser-operation.")) throw new DOMException("Blocked", "QuotaExceededError");
        return original.call(this, key, value);
      };
    });
    await page.getByRole("button", { name: "Run dry-run" }).click();
    await expect.poll(() => plans.length).toBe(2);
    if (form === "managed-secrets") await valueInput.fill("inert-secret");
    const confirmation = page.getByRole("region", { name: "Apply confirmation" });
    await confirmation.getByRole("checkbox").check();
    await confirmation.getByRole("button", { name: "Apply reviewed change" }).click();
    if (storageFailure) {
      await expect(page.getByText(/Apply was not sent because this tab/)).toBeVisible();
      expect(applies).toHaveLength(0);
      await expect(page.getByLabel("Change reason")).toBeEnabled();
      if (form === "managed-secrets") await expect(valueInput).toHaveValue("");
      await page.screenshot({ path: testInfo.outputPath(`${form}-storage-refused.png`) });
      return;
    }
    await expect(page.getByText("Apply uncertain", { exact: true })).toBeVisible();
    await expect(page.getByLabel("Change reason")).toBeDisabled();
    await expect(page.getByRole("button", { name: "Start over" })).toHaveCount(0);
    expect(await page.evaluate(() => JSON.stringify(sessionStorage))).not.toContain("inert-secret");
    await page.reload();
    await page.getByRole("button", { name: "Re-enter original Apply" }).click();
    await field.getByRole("checkbox").check();
    await page.getByLabel("Change reason").fill("Edited review");
    await valueInput.fill(form === "runtime-settings" ? "https://different.invalid" : "different-secret");
    const recovery = page.getByRole("region", { name: "Original Apply recovery" });
    await recovery.getByRole("checkbox").check();
    await recovery.getByRole("button", { name: "Retry original Apply" }).click();
    await expect(page.getByText(/Cannot create a new browser operation while the previous result is uncertain/)).toBeVisible();
    expect(applies).toHaveLength(1);
    expect(await page.evaluate(() => JSON.stringify(sessionStorage))).toContain("gateway_error");
    if (form === "managed-secrets") await expect(valueInput).toHaveValue("");
    await valueInput.fill(form === "runtime-settings" ? "https://example.invalid" : "inert-secret");
    await recovery.getByRole("checkbox").check();
    await expect(page.getByRole("button", { name: "Run dry-run" })).toBeDisabled();
    await page.screenshot({ path: testInfo.outputPath(`${form}-original-reentry.png`) });
    await recovery.getByRole("button", { name: "Retry original Apply" }).click();
    await expect.poll(() => applies.length).toBe(2);
    expect(applies[1]).toEqual(applies[0]);
    await expect(page.getByLabel("Change reason")).toBeEnabled();
    await expect(page.getByLabel("Change reason")).toHaveValue("");
    expect(await page.evaluate(() => Object.keys(sessionStorage)
      .filter(key => key.includes("browser-operation")).length)).toBe(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`${form}-reset.png`) });
  });
  }
}
