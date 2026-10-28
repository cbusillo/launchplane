import { waitForState, expect, test } from "./state";

test("private testing integration entry and reference recovery", { tag: "@layout" }, async ({ page }, testInfo) => {
  const bindingKey = "ODOO_OVERRIDE_SECRET__CONFIG_PARAM__PRINTNODE__API_KEY";
  await page.goto("/ui/products/atlas-commerce/environments/testing/managed-secrets?fixture=products");
  const data = await page.evaluate(async () => {
    const fixture = await import("/ui/src/dev-fixtures.ts");
    return { products: fixture.productsForFixture("products"), identity: fixture.fixtureIdentity,
      environment: fixture.environmentForFixture("products", "atlas-commerce", "testing"),
      config: fixture.configStatusForFixture("products", "atlas-commerce", "testing") };
  });
  if (!data.config || !data.environment) throw new Error("Missing testing fixture");
  data.config.driver_id = "odoo";
  data.config.managed_secrets = [{ binding_key: bindingKey, integration: "runtime_environment", context: data.config.context,
    instance: "testing", status: "missing", trust_state: "missing", updated_at: "" }];
  const secretRequests: Array<{ mode: string; managed_secrets: Array<{ secret_class?: string }> }> = [];
  const referenceApplies: Array<{ body: unknown; key: string }> = [];
  const allowanceApplies: Array<{ body: unknown; key: string }> = [];
  let allowances = [{ integration: "other", kind: "read_only_source", reason: "Existing independent source", evidence: "Existing proof" }];
  let saved = false;
  const referenceResult = () => ({ product: "atlas-commerce", context: data.config!.context, instance: "testing",
    entries: [{ key: "printnode.api_key", runtime_key: bindingKey, action: saved ? "already_agrees" : "add_reference" }],
    review_digest: "a".repeat(64), applied: saved, read_back_matches: null, values_redacted: true });
  await page.route("**/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.includes("/product-config/integration-allowances")) {
      const scope = { product: "atlas-commerce", context: data.config!.context, instance: "testing" };
      if (route.request().method() === "POST") {
        const body = route.request().postDataJSON();
        if (body.mode === "apply") {
          allowanceApplies.push({ body, key: route.request().headers()["idempotency-key"] });
          allowances = body.allowances;
          if (allowanceApplies.length === 1) { await route.abort("failed"); return; }
        }
        await route.fulfill({ json: { status: "accepted", trace_id: "allowance", records: scope,
          result: { ...scope, plan_sha256: "b".repeat(64), read_back_matches: body.mode === "apply" } } });
      } else await route.fulfill({ json: { status: "accepted", trace_id: "allowance-read", records: scope, result: { ...scope, allowances } } });
      return;
    }
    if (path.endsWith("/odoo-secret-references/reconcile")) {
      if (route.request().method() === "POST") {
        const body = route.request().postDataJSON();
        if (body.mode === "apply") {
          referenceApplies.push({ body, key: route.request().headers()["idempotency-key"] });
          saved = true;
          if (referenceApplies.length === 1) { await route.abort("failed"); return; }
        }
        await route.fulfill({ json: { status: "ok", trace_id: "reference", result: referenceResult(),
          ...(body.mode === "apply" ? { replayed: true, original_trace_id: "original-reference" } : {}) } });
      } else await route.fulfill({ json: { status: "ok", trace_id: "reference-read", result: referenceResult() } });
      return;
    }
    if (route.request().method() === "POST") {
      if (!path.endsWith("/config/apply")) throw new Error(`Unexpected mutation ${path}`);
      const body = route.request().postDataJSON();
      secretRequests.push(body);
      const response = await page.evaluate(async body => {
        const fixture = await import("/ui/src/dev-fixtures.ts");
        return fixture.applyProductEnvironmentConfigForFixture("products", "atlas-commerce", "testing", body);
      }, body);
      await route.fulfill({ status: 202, json: response });
      return;
    }
    const response = path.endsWith("/auth/session") ? { csrf_token: "csrf", identity: data.identity }
      : path.endsWith("/config-status") ? { config_status: data.config }
      : path.endsWith("/environments/testing") ? { environment: data.environment }
      : path.endsWith("/owner-secret-inputs") ? { fields: [] }
      : path.endsWith("/products") ? { products: data.products }
      : { product: data.products[0] };
    await route.fulfill({ json: { status: "ok", trace_id: "read", ...response } });
  });
  await page.goto("/ui/products/atlas-commerce/environments/testing/managed-secrets");
  const allowancePanel = page.getByRole("region", { name: "Testing integration allowances" });
  await allowancePanel.getByLabel("Integration name").fill("printnode");
  await allowancePanel.getByLabel("Testing account reason").fill("Independent testing account");
  await allowancePanel.getByLabel("Isolation evidence").fill("Dedicated account and testing printer verified");
  await allowancePanel.getByRole("button", { name: "Review testing allowance" }).click();
  await allowancePanel.getByRole("checkbox").check();
  await allowancePanel.getByRole("button", { name: "Save testing allowance" }).click();
  await expect(allowancePanel.getByText(/allowance outcome is uncertain/)).toBeVisible();
  await page.reload();
  await allowancePanel.getByRole("button", { name: "Retry original allowance apply" }).click();
  await expect(allowancePanel.getByText("printnode: dev store", { exact: true })).toBeVisible();
  await expect(allowancePanel.getByText("other: read only source", { exact: true })).toBeVisible();
  await waitForState(() => allowanceApplies.length).toBe(2);
  expect(allowanceApplies[1]).toEqual(allowanceApplies[0]);
  const field = page.locator(".product-config-field").filter({ hasText: bindingKey });
  await field.getByRole("checkbox").check();
  await field.getByRole("combobox").selectOption("testing");
  await page.getByLabel("Change reason").fill("Independent account and test printer verified");
  const value = field.getByLabel("Write-only value", { exact: true });
  await value.fill("inert-private-browser-credential");
  await page.getByRole("button", { name: "Run dry-run", exact: true }).click();
  await expect(page.getByRole("region", { name: "Apply confirmation" })).toBeVisible();
  await expect(value).toHaveValue("");
  expect(secretRequests[0].managed_secrets[0].secret_class).toBe("testing");
  await field.getByRole("combobox").selectOption("");
  await expect(page.getByRole("region", { name: "Apply confirmation" })).toHaveCount(0);
  await field.getByRole("combobox").selectOption("testing");
  await value.fill("inert-private-browser-credential");
  await page.getByRole("button", { name: "Run dry-run", exact: true }).click();
  const confirmation = page.getByRole("region", { name: "Apply confirmation" });
  await confirmation.getByRole("checkbox").check();
  await value.fill("inert-private-browser-credential");
  await confirmation.getByRole("button", { name: "Apply reviewed change" }).click();
  await waitForState(() => secretRequests.filter(request => request.mode === "apply").length).toBe(1);
  await expect(field.getByRole("checkbox")).not.toBeChecked();
  await expect(value).toHaveValue("");
  expect(secretRequests.at(-1)?.managed_secrets[0].secret_class).toBe("testing");
  expect(await page.evaluate(() => JSON.stringify({ session: sessionStorage, local: localStorage }))).not.toContain("inert-private-browser-credential");
  const panel = page.getByRole("region", { name: "Use a secret in Odoo" });
  await panel.getByLabel("Odoo parameter name").fill("printnode.api_key");
  await panel.getByRole("button", { name: "Review Odoo reference" }).click();
  await expect(panel.getByText(bindingKey, { exact: true })).toBeVisible();
  await expect(panel.getByText(/add reference/)).toBeVisible();
  await panel.getByLabel("Odoo parameter name").fill("different.api_key");
  await expect(panel.getByRole("button", { name: "Save Odoo reference" })).toHaveCount(0);
  await panel.getByLabel("Odoo parameter name").fill("printnode.api_key");
  await panel.getByRole("button", { name: "Review Odoo reference" }).click();
  await panel.getByRole("checkbox").check();
  await panel.getByRole("button", { name: "Save Odoo reference" }).click();
  await expect(panel.getByText(/apply outcome is uncertain/)).toBeVisible();
  await page.reload();
  await panel.getByRole("button", { name: "Retry original reference apply" }).click();
  await expect(panel.getByText(/original reference operation completed/)).toBeVisible();
  expect(referenceApplies).toHaveLength(2);
  expect(referenceApplies[1]).toEqual(referenceApplies[0]);
  await panel.getByRole("button", { name: "Read stored Odoo references" }).click();
  await expect(panel.getByText(/already agrees/)).toBeVisible();
  await expect(panel.getByText(/database presence verification are separate steps/)).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("private-testing-reference.png") });
});
