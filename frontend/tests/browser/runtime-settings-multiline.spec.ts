import type { ApplyProductEnvironmentConfigData } from "../../src/generated/openapi";
import { waitForState, expect, test } from "./state";

type ConfigRequest = ApplyProductEnvironmentConfigData["body"];

const originalToml = '[app]\nname = "Example"\n\n[features]\nenabled = true\nchannels = ["one", "two"]\n';
const editedToml = originalToml.replace('"Example"', '"Edited example"');

for (const fieldKind of ["declared", "site-added"] as const) {
  test(`${fieldKind} runtime setting preserves multiline values through review and original Apply recovery`, { tag: "@layout" }, async ({ page }, testInfo) => {
    await page.goto("/ui/products/atlas-commerce/environments/prod/runtime-settings?fixture=products");
    const data = await page.evaluate(async () => {
      const f = await import("/ui/src/dev-fixtures.ts");
      const config = f.configStatusForFixture("products", "atlas-commerce", "prod");
      config.runtime_settings[0].key = "APP_CONFIG";
      return { products: f.productsForFixture("products"), identity: f.fixtureIdentity,
        environment: f.environmentForFixture("products", "atlas-commerce", "prod"), config };
    });
    const plans: ConfigRequest[] = [];
    const applies: Array<{ body: ConfigRequest; key: string }> = [];
    await page.route("**/v1/**", async route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      if (request.method() === "POST") {
        const body = request.postDataJSON() as ConfigRequest;
        if (body.mode === "apply") {
          applies.push({ body, key: request.headers()["idempotency-key"] });
          if (applies.length === 1) {
            await route.fulfill({ status: 502, json: { trace_id: "lost-response", error: {
              code: "gateway_error", message: "Apply response unavailable" } } });
            return;
          }
        } else plans.push(body);
        const response = await page.evaluate(async body => {
          const f = await import("/ui/src/dev-fixtures.ts");
          return f.applyProductEnvironmentConfigForFixture("products", "atlas-commerce", "prod", body);
        }, body);
        await route.fulfill({ status: 202, json: response });
        return;
      }
      const response = path.endsWith("/auth/session") ? { csrf_token: "csrf", identity: data.identity }
        : path.endsWith("/config-status") ? { config_status: data.config }
        : path.endsWith("/environments/prod") ? { environment: data.environment }
        : path.endsWith("/products") ? { products: data.products }
        : { product: data.products[0] };
      await route.fulfill({ json: { status: "ok", trace_id: "read", ...response } });
    });
    await page.goto("/ui/products/atlas-commerce/environments/prod/runtime-settings");
    const declared = page.locator(".product-config-field").filter({ hasText: "APP_CONFIG" });
    const value = fieldKind === "declared" ? declared.getByLabel("New value") : page.getByLabel("Setting value");
    async function selectField() {
      if (fieldKind === "declared") await declared.getByRole("checkbox").check();
      else {
        await page.getByRole("button", { name: "Add a site setting" }).click();
        await page.getByLabel("Setting name").fill("SITE_CONFIG");
      }
    }
    const key = fieldKind === "declared" ? "APP_CONFIG" : "SITE_CONFIG";
    await selectField();
    await value.fill(originalToml);
    await expect(value).toHaveValue(originalToml);
    await value.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`${fieldKind}-multiline-draft.png`) });
    await page.getByLabel("Change reason").fill("Review multiline configuration");
    await page.getByRole("button", { name: "Run dry-run" }).click();
    const confirmation = page.getByRole("region", { name: "Apply confirmation" });
    await expect(confirmation).toBeVisible();
    expect(plans[0].runtime_settings).toEqual({ [key]: originalToml });
    expect(plans[0].managed_secrets ?? []).toEqual([]);
    await confirmation.getByRole("checkbox").check();
    await value.fill(editedToml);
    await expect(confirmation).toHaveCount(0);
    expect(applies).toHaveLength(0);
    await page.getByRole("button", { name: "Run dry-run" }).click();
    await expect(confirmation).toBeVisible();
    expect(plans[1].runtime_settings).toEqual({ [key]: editedToml });
    await confirmation.getByRole("checkbox").check();
    await confirmation.getByRole("button", { name: "Apply reviewed change" }).click();
    await expect(page.getByText("Apply uncertain", { exact: true })).toBeVisible();
    await expect(value).toBeDisabled();
    expect(applies[0].body.runtime_settings).toEqual({ [key]: editedToml });
    expect(applies[0].key).toBeTruthy();
    await page.reload();
    await page.getByRole("button", { name: "Re-enter original Apply" }).click();
    await selectField();
    await value.fill(editedToml);
    await page.getByLabel("Change reason").fill("Review multiline configuration");
    await expect(value).toHaveValue(editedToml);
    await value.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`${fieldKind}-multiline-reentered.png`) });
    const recovery = page.getByRole("region", { name: "Original Apply recovery" });
    await recovery.getByRole("checkbox").check();
    await recovery.getByRole("button", { name: "Retry original Apply" }).click();
    await waitForState(() => applies.length).toBe(2);
    expect(applies[1]).toEqual(applies[0]);
    await expect(page.getByLabel("Change reason")).toBeEnabled();
    await expect(page.getByLabel("Change reason")).toHaveValue("");
    if (fieldKind === "declared") await expect(value).toHaveValue("");
    else await expect(value).toHaveCount(0);
  });
}
