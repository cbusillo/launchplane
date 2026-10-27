import { expect, test } from "./state";

for (const view of ["runtime-settings", "managed-secrets"] as const) {
  test(`preview ${view} uses the existing configuration form`, async ({ page }) => {
    await page.goto("/ui/products/atlas-commerce?fixture=products");
    const data = await page.evaluate(async () => {
      const fixtures = await import("/ui/src/dev-fixtures.ts");
      return {
        identity: fixtures.fixtureIdentity,
        products: fixtures.productsForFixture("products"),
        config: fixtures.configStatusForFixture("products", "atlas-commerce", "prod"),
      };
    });
    const writes: string[] = [];
    const stableReads: string[] = [];
    await page.route("**/v1/**", async route => {
      const path = new URL(route.request().url()).pathname;
      if (route.request().method() === "POST") {
        writes.push(path);
        await route.fulfill({ status: 503, json: { status: "rejected", trace_id: "preview-plan", error: { code: "unavailable", message: "Fixture stopped after plan submission." } } });
        return;
      }
      if (/environments\/(prod|testing)/.test(path)) stableReads.push(path);
      const payload = path.endsWith("/auth/session") ? { csrf_token: "fixture", identity: data.identity }
        : path.endsWith("/config-status") ? { config_status: { ...data.config, environment: "preview" } }
        : path.endsWith("/products") ? { products: data.products }
        : path.endsWith("/owner-secret-inputs") ? { fields: [] }
        : { product: data.products[0] };
      await route.fulfill({ json: { status: "ok", trace_id: "preview-read", ...payload } });
    });
    await page.goto(`/ui/products/atlas-commerce/environments/preview/${view}`);
    await expect(page.getByRole("heading", { name: view === "runtime-settings" ? "Preview settings" : "Preview secrets", exact: true })).toBeVisible();
    const field = page.locator(".product-config-field").filter({ hasText: view === "runtime-settings" ? "PUBLIC_ORIGIN" : "SMTP_PASSWORD" });
    await field.getByRole("checkbox").check();
    await field.getByRole("textbox").fill(view === "runtime-settings" ? "https://preview.invalid" : "fixture-secret");
    await page.getByLabel("Change reason").fill("Review preview settings");
    await page.getByRole("button", { name: "Run dry-run", exact: true }).click();
    await expect(page.getByText("Fixture stopped after plan submission.", { exact: false }).first()).toBeVisible();
    expect(writes).toEqual(["/v1/products/atlas-commerce/environments/preview/config/apply"]);
    expect(stableReads).toEqual([]);
  });
}
