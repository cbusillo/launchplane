import { expect, test } from "@playwright/test";

for (const field of ["Image repository", "Production use"]) {
  test(`${field}: dry run, draft invalidation, Apply and read-back`, async ({ page }, testInfo) => {
    const mutations: string[] = [];
    page.on("request", request => { if (request.method() === "POST") mutations.push(request.url()); });
    await page.goto("/ui/products/atlas-commerce?fixture=products");
    const panel = page.getByRole("region", { name: field, exact: true });
    await expect(panel).toBeVisible();
    const apply = panel.getByRole("button", { name: "Apply", exact: true });
    await expect(apply).toBeDisabled();
    if (field === "Image repository") await panel.getByLabel(field, { exact: true }).fill("ghcr.io/example/new-package");
    else await panel.getByLabel(field, { exact: true }).selectOption("live");
    await panel.getByLabel("Change reason").fill("Review the classification or package move.");
    await panel.getByRole("button", { name: "Dry run", exact: true }).click();
    await expect(apply).toBeEnabled();
    if (field === "Image repository") await expect(panel.getByText("prod: ghcr.io/example/old-package@sha256:fixture", { exact: true })).toBeVisible();
    await panel.getByLabel("Change reason").fill("Changed after reviewing.");
    await expect(apply).toBeDisabled();
    await panel.getByRole("button", { name: "Dry run", exact: true }).click();
    await expect(apply).toBeEnabled();
    await apply.click();
    await expect(panel.getByRole("status")).toContainText("Applied and read back.");
    await expect(apply).toBeDisabled();
    expect(mutations).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await panel.screenshot({ path: testInfo.outputPath(`${field.replaceAll(" ", "-")}-applied.png`) });
  });
}

// Mock the HTTP boundary, so recovery exercises the production API adapter.

test("uncertain Apply survives reload and retries its reviewed request and key", async ({ page }) => {
  await page.goto("/ui/products/atlas-commerce?fixture=products");
  const { products, fixtureIdentity } = await page.evaluate(async () => {
    const fixtures = await import("/ui/src/dev-fixtures.ts");
    return { products: fixtures.productsForFixture("products"), fixtureIdentity: fixtures.fixtureIdentity };
  });
  const requests: Array<{ body: Record<string, unknown>; key: string }> = [];
  let productionUse = "unknown";
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "csrf", identity: fixtureIdentity } }));
  await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products } }));
  await page.route("**/v1/products/atlas-commerce", route => route.fulfill({ json: { status: "ok", trace_id: "product", product: products[0] } }));
  await page.route("**/v1/product-profiles/atlas-commerce", route => route.fulfill({ json: { status: "ok", trace_id: "profile", profile: {
    owner: { github_login: "example-owner", github_id: "9001" }, image: { repository: "ghcr.io/example/atlas-commerce" }, production_use: productionUse,
  } } }));
  await page.route("**/v1/product-profiles/atlas-commerce/production-use", async route => {
    const body = route.request().postDataJSON();
    if (body.mode === "apply") {
      requests.push({ body, key: route.request().headers()["idempotency-key"] });
      productionUse = "live";
      if (requests.length === 1) { await route.abort("failed"); return; }
    }
    await route.fulfill({ status: 202, json: { status: "accepted", trace_id: "profile-change", records: {}, result: {
      production_use_before: "unknown", production_use_after: "live", changed: true, applied: body.mode === "apply", plan_sha256: "a".repeat(64),
    } } });
  });
  await page.goto("/ui/products/atlas-commerce");
  let panel = page.getByRole("region", { name: "Production use", exact: true });
  await panel.getByLabel("Production use", { exact: true }).selectOption("live");
  await panel.getByLabel("Change reason").fill("Confirm current production use.");
  await panel.getByRole("button", { name: "Dry run", exact: true }).click();
  await panel.getByRole("button", { name: "Apply", exact: true }).click();
  await expect(panel.getByRole("button", { name: "Retry Apply" })).toBeEnabled();
  await expect(panel.getByLabel("Change reason")).toBeDisabled();
  await page.reload();
  panel = page.getByRole("region", { name: "Production use", exact: true });
  await expect(panel.getByRole("button", { name: "Retry Apply" })).toBeEnabled();
  await panel.getByRole("button", { name: "Retry Apply" }).click();
  await expect(panel.getByRole("status")).toContainText("Applied and read back.");
  expect(requests).toHaveLength(2);
  expect(requests[1]).toEqual(requests[0]);
  expect(requests[0].body.reviewed_plan_sha256).toBe("a".repeat(64));
});
