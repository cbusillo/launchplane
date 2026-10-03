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
    if (field === "Image repository") await panel.getByLabel(field, { exact: true }).fill("ghcr.io/example/atlas-commerce");
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

// Mock the HTTP boundary, so failures exercise the production API adapter.
for (const field of ["image", "production"] as const) {
  for (const outcome of ["uncertain", "stale", "mismatch"] as const) {
    test(`${field} Apply handles ${outcome} evidence`, async ({ page }) => {
      await page.goto("/ui/products/atlas-commerce?fixture=products");
      const { products, fixtureIdentity } = await page.evaluate(async () => {
        const fixtures = await import("/ui/src/dev-fixtures.ts");
        return { products: fixtures.productsForFixture("products"), fixtureIdentity: fixtures.fixtureIdentity };
      });
      const requests: Array<{ body: Record<string, unknown>; key: string }> = [];
      const title = field === "image" ? "Image repository" : "Production use";
      const suffix = field === "image" ? "image-repository" : "production-use";
      const prefix = field === "image" ? "image_repository" : "production_use";
      const before = field === "image" ? "ghcr.io/example/old-package" : "unknown";
      const after = field === "image" ? "ghcr.io/example/atlas-commerce" : "live";
      let stored = before;
      await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "csrf", identity: fixtureIdentity } }));
      await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products } }));
      await page.route("**/v1/products/atlas-commerce", route => route.fulfill({ json: { status: "ok", trace_id: "product", product: products[0] } }));
      await page.route("**/v1/product-profiles/atlas-commerce", route => route.fulfill({ json: { status: "ok", trace_id: "profile", profile: {
        repository: "example/atlas-commerce", owner: { github_login: "example-owner", github_id: "9001" },
        image: { repository: field === "image" ? stored : "ghcr.io/example/atlas-commerce" },
        production_use: field === "production" ? stored : "unknown",
      } } }));
      await page.route(`**/v1/product-profiles/atlas-commerce/${suffix}`, async route => {
        const body = route.request().postDataJSON();
        if (body.mode === "apply") {
          requests.push({ body, key: route.request().headers()["idempotency-key"] });
          if (outcome === "stale") {
            await route.fulfill({ status: 409, json: { trace_id: "stale", error: { code: "stale", message: "Review a new dry run." } } }); return;
          }
          if (outcome !== "mismatch") stored = after;
          if (outcome === "uncertain" && requests.length === 1) { await route.abort("failed"); return; }
        }
        await route.fulfill({ status: 202, json: { status: "accepted", trace_id: "profile-change", records: {}, result: {
          [`${prefix}_before`]: before, [`${prefix}_after`]: after,
          changed: true, applied: body.mode === "apply", plan_sha256: "a".repeat(64), lanes: [],
        } } });
      });
      await page.goto("/ui/products/atlas-commerce");
      let panel = page.getByRole("region", { name: title, exact: true });
      if (field === "image") {
        // The real route normalizes this slash before returning its plan.
        await panel.getByLabel(title, { exact: true }).fill(`${after}/`);
      } else await panel.getByLabel(title, { exact: true }).selectOption("live");
      await panel.getByLabel("Change reason").fill("Confirm the reviewed profile change.");
      await panel.getByRole("button", { name: "Dry run", exact: true }).click();
      await panel.getByRole("button", { name: "Apply", exact: true }).click();
      if (outcome === "stale") {
        await expect(panel.getByRole("button", { name: "Apply", exact: true })).toBeDisabled();
        await expect(panel.getByRole("button", { name: "Dry run", exact: true })).toBeEnabled();
        await panel.getByRole("button", { name: "Dry run", exact: true }).click();
        await expect(panel.getByRole("button", { name: "Apply", exact: true })).toBeEnabled();
      } else if (outcome === "mismatch") {
        await expect(panel.getByRole("alert")).toContainText("read-back differs");
        await expect(panel.getByText("Applied and read back.", { exact: false })).toHaveCount(0);
      } else {
        await expect(panel.getByRole("button", { name: "Retry Apply" })).toBeEnabled();
        await expect(panel.getByLabel("Change reason")).toBeDisabled();
        await page.reload();
        panel = page.getByRole("region", { name: title, exact: true });
        await expect(panel.getByRole("button", { name: "Retry Apply" })).toBeEnabled();
        await panel.getByRole("button", { name: "Retry Apply" }).click();
        await expect(panel.getByRole("status")).toContainText("Applied and read back.");
        expect(requests).toHaveLength(2);
        expect(requests[1]).toEqual(requests[0]);
        expect(field === "image" ? requests[0].body.expected_image_repository : requests[0].body.reviewed_plan_sha256)
          .toBe(field === "image" ? before : "a".repeat(64));
      }
    });
  }
}

test("image dry run offers the repository-named package and refuses another package", async ({ page }) => {
  await page.goto("/ui/products/atlas-commerce?fixture=products");
  const panel = page.getByRole("region", { name: "Image repository", exact: true });
  await expect(panel.getByLabel("Image repository", { exact: true })).toHaveValue("ghcr.io/example/atlas-commerce");
  await panel.getByLabel("Change reason").fill("Review new package.");
  await panel.getByLabel("Image repository", { exact: true }).fill("ghcr.io/example/wrong-package");
  await panel.getByRole("button", { name: "Dry run", exact: true }).click();
  await expect(panel.getByRole("alert")).toContainText("ghcr.io/example/atlas-commerce");
  await expect(panel.getByRole("button", { name: "Apply", exact: true })).toBeDisabled();
  await panel.getByLabel("Image repository", { exact: true }).fill("ghcr.io/example/atlas-commerce");
  await panel.getByRole("button", { name: "Dry run", exact: true }).click();
  await expect(panel.getByRole("button", { name: "Apply", exact: true })).toBeEnabled();
});
