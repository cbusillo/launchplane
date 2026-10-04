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

for (const storageFailure of ["unavailable", "read-back-mismatch", "operation-key-unavailable"] as const) {
test(`Client-login Save refuses ${storageFailure} storage and can retry after recovery`, async ({ page }) => {
  await mockProduct(page);
  const applies: Record<string, unknown>[] = [];
  await page.route("**/v1/product-profiles/atlas-commerce/owner", async route => {
    if (route.request().postDataJSON().mode === "apply") applies.push(route.request().postDataJSON());
    await respondWithPlan(route);
  });
  await page.addInitScript(({ storageFailure }) => {
    window.__denyProfileDraft = true;
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function(key, value) {
      const selected = storageFailure === "operation-key-unavailable"
        ? key.startsWith("launchplane.browser-operation.") : key.endsWith(":owner");
      if (window.__denyProfileDraft && selected) {
        if (storageFailure !== "read-back-mismatch") throw new DOMException("Quota exceeded", "QuotaExceededError");
        return; // A browser that silently fails to retain the draft.
      }
      return original.call(this, key, value);
    };
  }, { storageFailure });
  await page.goto("/ui/products/atlas-commerce");
  const panel = page.getByRole("region", { name: "example-owner (id 9001)", exact: true });
  await panel.getByLabel("GitHub login").fill("new-client");
  await panel.getByLabel("Change reason").fill("Review the original reason.");
  await panel.getByRole("button", { name: "Preview change", exact: true }).click();
  await panel.getByLabel("Change reason").fill("An unreviewed reason.");
  await expect(panel.getByRole("button", { name: "Save", exact: true })).toBeDisabled();
  await panel.getByLabel("Change reason").fill("Review the original reason.");
  await panel.getByRole("button", { name: "Save", exact: true }).click();
  if (storageFailure === "operation-key-unavailable") {
    await expect(panel.getByRole("status").filter({ hasText: "Save was not sent" })).toBeVisible();
  } else await expect(panel.getByRole("alert")).toContainText("Nothing was sent");
  expect(applies).toHaveLength(0);
  await page.evaluate(() => { window.__denyProfileDraft = false; });
  await panel.getByRole("button", { name: storageFailure === "operation-key-unavailable" ? "Retry save" : "Save", exact: true }).click();
  await expect(page.getByRole("region", { name: "new-client (id 7009)", exact: true }).getByRole("status")).toContainText("Saved.");
  expect(applies).toEqual([{ schema_version: 1, mode: "apply", github_login: "new-client", reason: "Review the original reason." }]);
});
}

async function respondWithPlan(route: Route) {
  const body = route.request().postDataJSON();
  await route.fulfill({ status: 202, json: { status: "accepted", trace_id: "client-change", records: {}, result: {
    owner_before: { github_login: "example-owner", github_id: "9001" },
    owner_after: body.clear ? null : { github_login: body.github_login, github_id: "7009" },
    changed: true, applied: body.mode === "apply",
  } } });
}

for (const interruption of ["lost", "lost-reload", "reload-submitting", "navigation"] as const) {
  test(`Client-login dry run recovers after ${interruption}`, async ({ page }) => {
    await mockProduct(page);
    const requests: Array<{ body: Record<string, unknown>; key: string }> = [];
    let held: Route | null = null;
    await page.route("**/v1/product-profiles/atlas-commerce/owner", async route => {
      requests.push({ body: route.request().postDataJSON(), key: route.request().headers()["idempotency-key"] });
      if (requests.length === 1) {
        if (interruption === "navigation" || interruption === "reload-submitting") held = route;
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
    if (interruption === "navigation" || interruption === "reload-submitting") await expect.poll(() => requests.length).toBe(1);
    else await expect(panel.getByRole("status")).toContainText("Failed to fetch");
    if (interruption === "navigation") {
      await page.getByRole("link", { name: "Product Ops", exact: true }).click();
      await expect(panel).toHaveCount(0);
      await page.locator(".product-directory-row").filter({ hasText: "Atlas Commerce" }).click();
    } else if (interruption !== "lost") await page.reload();
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

for (const recovery of ["same-tab", "reload", "clear-reload", "navigation", "reload-submitting"] as const) {
test(`Client-login uncertain Apply recovers after ${recovery} with the same request and key`, async ({ page }, testInfo) => {
  await mockProduct(page);
  const applies: Array<{ body: Record<string, unknown>; key: string }> = [];
  let held: Route | null = null;
  await page.route("**/v1/product-profiles/atlas-commerce/owner", async route => {
    if (route.request().postDataJSON().mode === "apply") {
      applies.push({ body: route.request().postDataJSON(), key: route.request().headers()["idempotency-key"] });
      if (applies.length === 1) {
        if (recovery === "reload-submitting") held = route;
        else await route.abort("failed");
        return;
      }
    }
    await respondWithPlan(route);
  });
  await page.goto("/ui/products/atlas-commerce");
  const panel = page.getByRole("region", { name: "example-owner (id 9001)", exact: true });
  await panel.getByLabel("GitHub login").fill("new-client");
  await panel.getByLabel("Change reason").fill("Save the reviewed Client.");
  await panel.getByRole("button", { name: recovery === "clear-reload" ? "Clear" : "Preview change", exact: true }).click();
  await panel.getByRole("button", { name: "Save", exact: true }).click();
  if (recovery === "reload-submitting") await expect.poll(() => applies.length).toBe(1);
  else await expect(panel.getByRole("button", { name: "Retry save", exact: true })).toBeEnabled();
  if (recovery === "navigation") {
    await page.getByRole("link", { name: "Product Ops", exact: true }).click();
    await expect(panel).toHaveCount(0);
    await page.locator(".product-directory-row").filter({ hasText: "Atlas Commerce" }).click();
  } else if (recovery !== "same-tab") await page.reload();
  await expect(panel.getByRole("button", { name: "Retry save", exact: true })).toBeEnabled();
  await expect(panel.getByLabel("GitHub login")).toBeDisabled();
  await expect(panel.getByLabel("Change reason")).toBeDisabled();
  await expect(panel.getByRole("button", { name: "Preview change", exact: true })).toBeDisabled();
  await expect(panel.getByRole("button", { name: "Clear", exact: true })).toBeDisabled();
  await expect(panel.getByLabel("Change reason")).toHaveValue("Save the reviewed Client.");
  if (recovery === "reload") await panel.screenshot({ path: testInfo.outputPath("client-login-recovered.png") });
  await panel.getByRole("button", { name: "Retry save", exact: true }).click();
  await expect(page.getByRole("region", { name: recovery === "clear-reload" ? "No Client set" : "new-client (id 7009)", exact: true }).getByRole("status")).toContainText("Saved.");
  expect(applies).toHaveLength(2);
  expect(applies[0].key).toBeTruthy();
  expect(applies[1]).toEqual(applies[0]);
  await page.reload();
  await expect(panel.getByRole("button", { name: "Save", exact: true })).toBeDisabled();
  await expect(panel.getByLabel("Change reason")).toBeEnabled();
  if (held) await held.abort().catch(() => {});
});
}
