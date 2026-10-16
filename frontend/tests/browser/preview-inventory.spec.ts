import { expect, test } from "./state";
import type { ProductPreviewSummary } from "../../src/generated/openapi.ts";

const cases: Array<{ name: string; enabled: boolean; count: number;
  trust: ProductPreviewSummary["trust_state"]; headline: string; scan?: NonNullable<ProductPreviewSummary["inventory"]>["status"]; observedCount?: number }> = [
  { name: "scanned empty with retained identities", enabled: true, count: 3, trust: "verified", scan: "empty", observedCount: 0, headline: "No previews observed" },
  { name: "scanned nonempty with untracked identity", enabled: true, count: 3, trust: "verified", scan: "nonempty", observedCount: 1, headline: "1 observed" },
  { name: "scan failed", enabled: true, count: 0, trust: "missing", scan: "failed", headline: "Inventory scan failed" },
  { name: "scan unavailable", enabled: true, count: 0, trust: "missing", scan: "unavailable", headline: "Inventory unavailable" },
  { name: "scan unsupported", enabled: true, count: 0, trust: "unsupported", scan: "unsupported", headline: "Inventory unsupported" },
  { name: "scan stale", enabled: true, count: 0, trust: "stale", scan: "stale", observedCount: 0, headline: "Inventory stale" },
  { name: "missing nonzero", enabled: true, count: 3, trust: "missing", headline: "Inventory unknown" },
  { name: "missing zero", enabled: true, count: 0, trust: "missing", headline: "Inventory unknown" },
  { name: "disabled", enabled: false, count: 0, trust: "missing", headline: "Not applicable" },
  { name: "recorded nonzero", enabled: true, count: 3, trust: "recorded", headline: "3 recorded" },
  { name: "recorded zero", enabled: true, count: 0, trust: "recorded", headline: "0 recorded" },
  { name: "verified active", enabled: true, count: 3, trust: "verified", headline: "3 active" },
  { name: "verified empty", enabled: true, count: 0, trust: "verified", headline: "No active previews" },
  { name: "stale", enabled: true, count: 3, trust: "stale", headline: "Inventory stale" },
  { name: "unsupported", enabled: true, count: 3, trust: "unsupported", headline: "Inventory unavailable" },
];

for (const state of cases) {
  test(`preview ${state.name} agrees across product surfaces`, { tag: "@layout" }, async ({ page }, testInfo) => {
    await page.goto("/ui/products?fixture=products");
    const fixtures = await page.evaluate(async () => {
      const modulePath = "/ui/src/dev-fixtures.ts";
      const module = await import(modulePath);
      return { product: module.productsForFixture("products")[0], identity: module.fixtureIdentity };
    });
    const product = fixtures.product;
    Object.assign(product.preview, { enabled: state.enabled, active_count: state.count, trust_state: state.trust });
    const now = await page.evaluate(() => Date.now());
    product.preview.provenance = { ...product.preview.provenance, freshness_status: state.trust,
      stale_after: new Date(now + 60_000).toISOString() };
    if (state.scan) {
      const provenance = { ...product.preview.provenance, source_record_id: "actual-provider-scan",
        recorded_at: new Date(now).toISOString(), refreshed_at: new Date(now).toISOString(),
        stale_after: new Date(now + 60_000).toISOString(), freshness_status: state.trust };
      product.preview.provenance = provenance;
      product.preview.inventory = { status: state.scan, observed_count: state.observedCount ?? null,
        observed_slugs: state.observedCount ? ["pr-99"] : [], provenance };
      product.preview.unobserved_record_slugs = state.count ? ["pr-1", "pr-2", "pr-3"] : [];
      product.preview.untracked_observed_slugs = state.observedCount ? ["pr-99"] : [];
    }
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
    if (state.trust === "missing" && state.enabled && !state.scan) {
      await expect(row.locator(".directory-preview")).not.toContainText(/\d+ active|No active/);
    }
    await row.click();
    const summary = page.getByRole("region", { name: "Product summary" }).locator(".signal-tile")
      .filter({ hasText: /^Previews/i });
    await expect(summary.locator("strong")).toHaveText(state.headline);
    const detail = page.locator(".preview-summary");
    await expect(detail.getByRole("heading")).toHaveText(state.headline);
    if (state.trust === "missing" && state.enabled && !state.scan) {
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
      product.preview.provenance.freshness_status = "verified";
      await page.getByRole("button", { name: "Refresh current evidence" }).click();
      await expect(summary.locator("strong")).toHaveText("3 active");
      await expect(detail.getByRole("heading")).toHaveText("3 active");
      await page.getByRole("link", { name: "Launchplane home", exact: true }).first().click();
      await expect(page.locator(".directory-preview > strong")).toHaveText("3 active");
    }
    if (state.scan === "empty") {
      await expect(detail).toContainText("3 recorded preview identities");
      await expect(detail).toContainText("3 recorded preview(s) absent from scan");
      await page.screenshot({ path: `../tmp/browser-smoke/preview-scanned-empty-${testInfo.project.name}.png`, fullPage: true });
      await page.clock.fastForward(60_001);
      await expect(detail.getByRole("heading")).toHaveText("Inventory stale");
      await expect(summary.locator("strong")).toHaveText("Inventory stale");
      await page.getByRole("link", { name: "Launchplane home", exact: true }).first().click();
      await expect(page.locator(".directory-preview > strong")).toHaveText("Inventory stale");
    }
    if (state.scan === "nonempty") {
      await expect(detail).toContainText("1 observed preview(s) without a current record");
    }
    expect(mutations).toEqual([]);
  });
}
