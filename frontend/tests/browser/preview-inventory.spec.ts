import { expect, test } from "@playwright/test";
import type { ProductPreviewSummary } from "../../src/generated/openapi.ts";

const cases: Array<{ name: string; enabled: boolean; count: number;
  trust: ProductPreviewSummary["trust_state"]; headline: string }> = [
  { name: "missing nonzero", enabled: true, count: 3, trust: "missing", headline: "Inventory unknown" },
  { name: "missing zero", enabled: true, count: 0, trust: "missing", headline: "Inventory unknown" },
  { name: "disabled", enabled: false, count: 0, trust: "missing", headline: "Not enabled" },
  { name: "recorded nonzero", enabled: true, count: 3, trust: "recorded", headline: "3 recorded" },
  { name: "recorded zero", enabled: true, count: 0, trust: "recorded", headline: "0 recorded" },
  { name: "verified active", enabled: true, count: 3, trust: "verified", headline: "3 active" },
  { name: "verified empty", enabled: true, count: 0, trust: "verified", headline: "No active previews" },
  { name: "stale", enabled: true, count: 3, trust: "stale", headline: "Inventory stale" },
  { name: "unsupported", enabled: true, count: 3, trust: "unsupported", headline: "Inventory unavailable" },
];

for (const state of cases) {
  test(`preview ${state.name} agrees across product surfaces`, async ({ page }, testInfo) => {
    await page.goto("/ui/products?fixture=products");
    const fixtures = await page.evaluate(async () => {
      const modulePath = "/ui/src/dev-fixtures.ts";
      const module = await import(modulePath);
      return { product: module.productsForFixture("products")[0], identity: module.fixtureIdentity };
    });
    const product = fixtures.product;
    Object.assign(product.preview, { enabled: state.enabled, active_count: state.count, trust_state: state.trust });
    const mutations: string[] = [];
    page.on("request", request => {
      if (request.url().includes("/v1/") && request.method() !== "GET") mutations.push(request.url());
    });
    await page.route("**/v1/**", route => route.fulfill({ status: 403, json: {
      status: "error", error: { code: "authorization_denied", message: "Fixture read unavailable" },
      trace_id: "unused-read",
    } }));
    await page.route("**/v1/auth/session", route => route.fulfill({ json: {
      status: "ok", trace_id: "session", csrf_token: "csrf", identity: fixtures.identity,
    } }));
    await page.route("**/v1/products", route => route.fulfill({ json: {
      status: "ok", trace_id: "products", products: [product],
    } }));
    await page.route(`**/v1/products/${product.product}`, route => route.fulfill({ json: {
      status: "ok", trace_id: "product", product,
    } }));
    await page.goto("/ui/products");
    const row = page.locator(".product-directory-row");
    await expect(row.locator(".directory-preview > strong")).toHaveText(state.headline);
    if (state.trust === "missing" && state.enabled) {
      await expect(row.locator(".directory-preview")).not.toContainText(/\d+ active|No active/);
    }
    await row.click();
    const summary = page.getByRole("region", { name: "Product summary" }).locator(".signal-tile")
      .filter({ hasText: /^Previews/i });
    await expect(summary.locator("strong")).toHaveText(state.headline);
    const detail = page.locator(".preview-summary");
    await expect(detail.getByRole("heading")).toHaveText(state.headline);
    if (state.trust === "missing" && state.enabled) {
      await expect(detail).toContainText("cannot determine whether previews exist");
      await expect(detail).toContainText(`Recorded count: ${state.count}`);
    }
    if (state.trust === "recorded") {
      await expect(detail).toContainText("has not been verified");
      await expect(detail).not.toContainText("read completed without");
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    if (state.name === "missing nonzero") {
      await page.screenshot({ path: `../tmp/browser-smoke/preview-unknown-${testInfo.project.name}.png`, fullPage: true });
      // A supported fresh read, rather than the recorded count, changes the claim.
      product.preview.trust_state = "verified";
      await page.getByRole("button", { name: "Refresh current evidence" }).click();
      await expect(summary.locator("strong")).toHaveText("3 active");
      await expect(detail.getByRole("heading")).toHaveText("3 active");
      await page.getByRole("link", { name: "Launchplane home", exact: true }).first().click();
      await expect(page.locator(".directory-preview > strong")).toHaveText("3 active");
    }
    expect(mutations).toEqual([]);
  });
}
