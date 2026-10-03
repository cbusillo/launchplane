import { expect, test, type Page, type Route } from "@playwright/test";

async function mockProduct(page: Page) {
  await page.goto("/ui/products/atlas-commerce?fixture=products");
  const { products, fixtureIdentity } = await page.evaluate(async () => {
    const fixtures = await import("/ui/src/dev-fixtures.ts");
    return { products: fixtures.productsForFixture("products"), fixtureIdentity: fixtures.fixtureIdentity };
  });
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "csrf", identity: fixtureIdentity } }));
  await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products } }));
  await page.route("**/v1/products/atlas-commerce", route => route.fulfill({ json: { status: "ok", trace_id: "product", product: products[0] } }));
  await page.route("**/v1/product-profiles/atlas-commerce", route => route.fulfill({ json: { status: "ok", trace_id: "profile", profile: {
    repository: "example/atlas-commerce", owner: { github_login: "example-owner", github_id: "9001" },
    image: { repository: "ghcr.io/example/old-package" }, production_use: "unknown",
  } } }));
}

async function respondWithPlan(route: Route) {
  const body = route.request().postDataJSON();
  await route.fulfill({ status: 202, json: { status: "accepted", trace_id: "client-change", records: {}, result: {
    owner_before: { github_login: "example-owner", github_id: "9001" },
    owner_after: { github_login: body.github_login, github_id: "7009" },
    changed: true, applied: body.mode === "apply",
  } } });
}

for (const interruption of ["lost", "lost-reload", "navigation"] as const) {
  test(`Client-login dry run recovers after ${interruption}`, async ({ page }) => {
    await mockProduct(page);
    const requests: Array<{ body: Record<string, unknown>; key: string }> = [];
    let held: Route | null = null;
    await page.route("**/v1/product-profiles/atlas-commerce/owner", async route => {
      requests.push({ body: route.request().postDataJSON(), key: route.request().headers()["idempotency-key"] });
      if (requests.length === 1) {
        if (interruption === "navigation") held = route;
        else await route.abort("failed");
        return;
      }
      await respondWithPlan(route);
    });
    await page.goto("/ui/products/atlas-commerce");
    const panel = page.getByRole("region", { name: "example-owner (id 9001)", exact: true });
    await panel.getByLabel("GitHub login").fill("first-client");
    await panel.getByLabel("Change reason").fill("First read-only preview.");
    await panel.getByRole("button", { name: "Preview change", exact: true }).click();
    if (interruption === "navigation") await expect.poll(() => requests.length).toBe(1);
    else await expect(panel.getByRole("status")).toContainText("Failed to fetch");
    if (interruption !== "lost") await page.reload();
    await panel.getByLabel("GitHub login").fill("revised-client");
    await panel.getByLabel("Change reason").fill("Revised read-only preview.");
    await panel.getByRole("button", { name: "Preview change", exact: true }).click();
    await expect(panel.getByRole("status")).toContainText("Set the Client to revised-client (id 7009).");
    await expect(panel.getByRole("button", { name: "Save", exact: true })).toBeEnabled();
    expect(requests.map(request => request.body)).toEqual([
      { schema_version: 1, mode: "dry-run", github_login: "first-client", reason: "First read-only preview." },
      { schema_version: 1, mode: "dry-run", github_login: "revised-client", reason: "Revised read-only preview." },
    ]);
    expect(requests[1].key).toBeTruthy();
    expect(requests[1].key).not.toBe(requests[0].key);
    if (held) await held.abort().catch(() => {}); // Reload already cancelled this test-only request.
  });
}

test("Client-login uncertain Apply locks the draft and retries the same request and key", async ({ page }) => {
  await mockProduct(page);
  const applies: Array<{ body: Record<string, unknown>; key: string }> = [];
  await page.route("**/v1/product-profiles/atlas-commerce/owner", async route => {
    if (route.request().postDataJSON().mode === "apply") {
      applies.push({ body: route.request().postDataJSON(), key: route.request().headers()["idempotency-key"] });
      if (applies.length === 1) { await route.abort("failed"); return; }
    }
    await respondWithPlan(route);
  });
  await page.goto("/ui/products/atlas-commerce");
  const panel = page.getByRole("region", { name: "example-owner (id 9001)", exact: true });
  await panel.getByLabel("GitHub login").fill("new-client");
  await panel.getByLabel("Change reason").fill("Save the reviewed Client.");
  await panel.getByRole("button", { name: "Preview change", exact: true }).click();
  await panel.getByRole("button", { name: "Save", exact: true }).click();
  await expect(panel.getByRole("button", { name: "Retry save", exact: true })).toBeEnabled();
  await expect(panel.getByLabel("GitHub login")).toBeDisabled();
  await expect(panel.getByLabel("Change reason")).toBeDisabled();
  await expect(panel.getByRole("button", { name: "Preview change", exact: true })).toBeDisabled();
  await expect(panel.getByRole("button", { name: "Clear", exact: true })).toBeDisabled();
  await panel.getByRole("button", { name: "Retry save", exact: true }).click();
  await expect(page.getByRole("region", { name: "new-client (id 7009)", exact: true }).getByRole("status")).toContainText("Saved.");
  expect(applies).toHaveLength(2);
  expect(applies[0].key).toBeTruthy();
  expect(applies[1]).toEqual(applies[0]);
});
