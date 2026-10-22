import { expect, test, type Page } from "./state";

for (const outcome of ["saved", "lost-reply", "storage-failure"]) {
  const lostReply = outcome === "lost-reply";
  test(`Director product registration ${outcome} @responsive`, async ({ page }, testInfo) => {
    await page.goto("/ui/products?fixture=products");
    const identity = await page.evaluate(async () => (await import("/ui/src/dev-fixtures.ts")).fixtureIdentity);
    let profile: Record<string, unknown> | null = null;
    const applies: Array<{ key: string; body: unknown }> = [];
    await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "csrf", identity } }));
    await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: [] } }));
    await page.route("**/v1/product-profiles/desktop-tool", route => route.fulfill({ json: { status: "ok", trace_id: "profile", profile } }));
    await page.route("**/v1/product-onboarding/apply", async route => {
      const body = route.request().postDataJSON();
      const planned = { ...body.manifest, lifecycle_state: "active", lanes: [], updated_at: "2026-10-22T12:00:00Z", source: "fixture" };
      if (body.mode === "apply") {
        applies.push({ key: route.request().headers()["idempotency-key"], body });
        profile = planned;
        if (lostReply && applies.length === 1) { await route.abort("failed"); return; }
      }
      await route.fulfill({ status: 202, json: { status: "accepted", trace_id: "registration", records: {}, result: { mode: body.mode, plan_sha256: "a".repeat(64), product_profile: planned } } });
    });
    await page.goto("/ui/products");
    await page.getByText("Register a Director product", { exact: true }).click();
    const panel = page.getByRole("region", { name: "Director product registration" });
    await fillRegistration(page);
    await expect(panel.getByRole("button", { name: "Register product", exact: true })).toBeDisabled();
    await panel.getByRole("button", { name: "Review registration", exact: true }).click();
    await expect(panel.getByRole("button", { name: "Register product", exact: true })).toBeEnabled();
    await panel.getByLabel("Product name", { exact: true }).fill("Renamed desktop tool");
    await expect(panel.getByRole("button", { name: "Register product", exact: true })).toBeDisabled();
    await panel.getByRole("button", { name: "Review registration", exact: true }).click();
    await expect(panel.getByRole("button", { name: "Register product", exact: true })).toBeEnabled();
    if (outcome === "storage-failure") await page.evaluate(() => {
      Storage.prototype.setItem = () => { throw new DOMException("Storage unavailable", "QuotaExceededError"); };
    });
    await panel.getByRole("button", { name: "Register product", exact: true }).click();
    if (outcome === "storage-failure") {
      await expect(panel.getByRole("alert")).toBeVisible();
      expect(applies).toHaveLength(0);
      await page.screenshot({ path: testInfo.outputPath("registration-storage-failure.png"), fullPage: true });
      return;
    }
    if (lostReply) {
      await expect(panel.getByRole("button", { name: "Retry same registration" })).toBeEnabled();
      await page.reload();
      await page.getByText("Register a Director product", { exact: true }).click();
      await page.getByRole("button", { name: "Retry same registration" }).click();
    }
    await expect(panel.getByRole("status")).toContainText("Registered and read back");
    expect(applies).toHaveLength(lostReply ? 2 : 1);
    if (lostReply) expect(applies[1]).toEqual(applies[0]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: testInfo.outputPath(`registration-${lostReply ? "recovered" : "saved"}.png`), fullPage: true });
  });
}

async function fillRegistration(page: Page): Promise<void> {
  await page.getByLabel("Product key", { exact: true }).fill("desktop-tool");
  await page.getByLabel("Product name", { exact: true }).fill("Desktop tool");
  await page.getByLabel("Repository", { exact: true }).fill("example/desktop-tool");
  await page.getByLabel("Client GitHub login", { exact: true }).fill("director");
  await page.getByLabel("Client GitHub ID", { exact: true }).fill("1234");
  await page.getByLabel("The recorded Client is the Director and accepts this product under standing direction.").check();
}
