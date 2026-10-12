import { expect, test } from "@playwright/test";

for (const outcome of ["completed", "uncertain", "denied"] as const) {
  test(`Product retirement ${outcome}`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await page.goto("/ui/products/atlas-commerce?fixture=products");
    const { products, fixtureIdentity } = await page.evaluate(async () => {
      const fixtures = await import("/ui/src/dev-fixtures.ts");
      return { products: fixtures.productsForFixture("products"), fixtureIdentity: fixtures.fixtureIdentity };
    });
    let state = "active";
    const sent: Array<{ body: unknown; key: string }> = [];
    await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "csrf", identity: fixtureIdentity } }));
    await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products } }));
    await page.route("**/v1/products/atlas-commerce", route => route.fulfill({ json: { status: "ok", trace_id: "product", product: products[0] } }));
    await page.route("**/v1/product-profiles/atlas-commerce", route => route.fulfill({ json: { status: "ok", trace_id: "profile", profile: { driver_id: "generic-web", lifecycle_state: state, production_use: "prelaunch", lanes: [{ context: "atlas", instance: "testing" }], preview: { enabled: state === "active" }, image: { repository: "ghcr.io/example/atlas" }, repository: "example/atlas" } } }));
    await page.route("**/v1/product-retirement", async route => {
      const body = route.request().postDataJSON();
      if (outcome === "denied") { await route.fulfill({ status: 403, json: { error: { code: "authorization_denied", message: "Administrator access is required." } } }); return; }
      if (body.mode === "apply") {
        sent.push({ body, key: route.request().headers()["idempotency-key"] }); state = "retired";
        if (outcome === "uncertain" && sent.length === 1) { await route.abort("failed"); return; }
      }
      await route.fulfill({ status: 202, json: { status: "accepted", trace_id: "retirement", records: { product_retirement_plan_id: "plan-1", product_retirement_record_id: "record-1" }, result: { product: "atlas-commerce", instance: "testing", mode: body.mode, outcome: body.mode === "plan" ? "planned" : "retired", lifecycle_after: body.mode === "apply" ? "retired" : "active", provider_absence_verified: body.mode === "apply", plan_sha256: "a".repeat(64), confirmation: "retire product atlas-commerce including its previews", testing_plan: { application_name: "Atlas testing", routes: ["https://testing.example.invalid"], affected_records: ["testing target", "testing runtime"], ownership: "Exclusive product-owned testing application" }, preview_plan: [{ preview_id: "preview-1", application_name: "Atlas preview 1", provider_state: "present", routes: ["https://pr-1.example.invalid"], affected_records: ["preview record"], ownership: "Exclusive product-owned preview" }] } } });
    });
    await page.goto("/ui/products/atlas-commerce");
    let panel = page.getByRole("region", { name: "Retire product", exact: true });
    await expect(panel).toBeVisible();
    await panel.getByLabel("Change reason").fill("This product is obsolete.");
    await panel.getByLabel("Retirement related issue").fill("example/launchplane#1");
    await panel.getByRole("button", { name: "Review retirement plan" }).click();
    if (outcome === "denied") { await expect(panel).toContainText("Administrator access is required."); expect(sent).toEqual([]); return; }
    await expect(panel).toContainText("Atlas preview 1");
    await expect(panel).toContainText("https://testing.example.invalid");
    await expect(panel.getByRole("button", { name: "Confirm retirement", exact: true })).toBeDisabled();
    await panel.getByRole("checkbox").check();
    await page.screenshot({ path: testInfo.outputPath("product-retirement-reviewed.png"), fullPage: true });
    await panel.screenshot({ path: testInfo.outputPath("product-retirement-card-reviewed.png") });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await panel.getByRole("button", { name: "Confirm retirement", exact: true }).click();
    if (outcome === "uncertain") {
      await expect(panel).toContainText("The result is uncertain.");
      await page.reload(); panel = page.getByRole("region", { name: "Retire product", exact: true });
      await expect(panel.getByLabel("Change reason")).toBeDisabled();
      await panel.getByRole("button", { name: "Resume original retirement" }).click();
      await expect.poll(() => sent.length).toBe(2);
      expect(sent[1]).toEqual(sent[0]);
    }
    await expect(panel).toContainText("Product retired.");
    expect(sent).toHaveLength(outcome === "uncertain" ? 2 : 1);
    await page.screenshot({ path: testInfo.outputPath("product-retirement-completed.png"), fullPage: true });
  });
}
