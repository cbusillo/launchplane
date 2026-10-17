import { expect, test } from "./state";
import { previewInventoryPresentation } from "../../src/preview-inventory";
import { expireProductEvidence } from "../../src/product-environment-signal";
import type { ProductPreviewSummary } from "../../src/generated/openapi.ts";

const cases: Array<{ name: string; enabled: boolean; count: number;
  trust: ProductPreviewSummary["trust_state"]; scan?: NonNullable<ProductPreviewSummary["inventory"]>["status"]; observedCount?: number }> = [
  { name: "scanned empty with retained identities", enabled: true, count: 3, trust: "verified", scan: "empty", observedCount: 0 },
  { name: "scanned nonempty with untracked identity", enabled: true, count: 3, trust: "verified", scan: "nonempty", observedCount: 1 },
  { name: "scan failed", enabled: true, count: 0, trust: "missing", scan: "failed" },
  { name: "scan unavailable", enabled: true, count: 0, trust: "missing", scan: "unavailable" },
  { name: "scan unsupported", enabled: true, count: 0, trust: "unsupported", scan: "unsupported" },
  { name: "scan stale", enabled: true, count: 0, trust: "stale", scan: "stale", observedCount: 0 },
  { name: "current missing nonzero", enabled: true, count: 3, trust: "missing", scan: "missing" },
  { name: "current missing zero", enabled: true, count: 0, trust: "missing", scan: "missing" },
  { name: "missing nonzero", enabled: true, count: 3, trust: "missing" },
  { name: "missing zero", enabled: true, count: 0, trust: "missing" },
  { name: "disabled", enabled: false, count: 0, trust: "missing" },
  { name: "recorded nonzero", enabled: true, count: 3, trust: "recorded" },
  { name: "recorded zero", enabled: true, count: 0, trust: "recorded" },
  { name: "verified active", enabled: true, count: 3, trust: "verified" },
  { name: "verified empty", enabled: true, count: 0, trust: "verified" },
  { name: "stale", enabled: true, count: 3, trust: "stale" },
  { name: "unsupported", enabled: true, count: 3, trust: "unsupported" },
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
    const expected = previewInventoryPresentation(product.preview);
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
    await expect(row.locator(".directory-preview > strong")).toHaveText(expected.headline);
    await expect(row.locator(".directory-preview")).toContainText(expected.detail);
    if (state.trust === "missing" && state.enabled && !state.scan) {
      await expect(row.locator(".directory-preview")).not.toContainText(/\d+ active|No active/);
    }
    await row.click();
    const summary = page.getByRole("region", { name: "Product summary" }).locator(".signal-tile")
      .filter({ hasText: /^Previews/i });
    await expect(summary.locator("strong")).toHaveText(expected.headline);
    const detail = page.locator(".preview-summary");
    await expect(detail.getByRole("heading")).toHaveText(expected.headline);
    if (state.trust === "missing" && state.enabled && !state.scan) {
      await expect(detail).toContainText(expected.detail);
    }
    if (state.trust === "recorded") {
      await expect(detail).toContainText(expected.detail);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    if (state.name === "missing nonzero") {
      await page.screenshot({ path: `../tmp/browser-smoke/preview-unknown-${testInfo.project.name}.png`, fullPage: true });
      // A supported fresh read, rather than the recorded count, changes the claim.
      product.preview.trust_state = "verified";
      product.preview.provenance.freshness_status = "verified";
      await page.getByRole("button", { name: "Refresh current evidence" }).click();
      await expect(summary.locator("strong")).toHaveText(previewInventoryPresentation(product.preview).headline);
      await expect(detail.getByRole("heading")).toHaveText(previewInventoryPresentation(product.preview).headline);
      await page.getByRole("link", { name: "Launchplane home", exact: true }).first().click();
      await expect(page.locator(".directory-preview > strong")).toHaveText(previewInventoryPresentation(product.preview).headline);
    }
    if (state.scan === "empty") {
      await expect(detail).toContainText(expected.detail);
      await page.screenshot({ path: `../tmp/browser-smoke/preview-scanned-empty-${testInfo.project.name}.png`, fullPage: true });
      const expiredPresentation = previewInventoryPresentation(expireProductEvidence(product, now + 60_001).preview);
      const recordedPresentation = previewInventoryPresentation({ ...product.preview, trust_state: "recorded" });
      // An expired scan must explain its historical counts differently from an
      // unverified recorded scan; intentional copy changes remain free to pass.
      expect(expiredPresentation.detail).not.toEqual(recordedPresentation.detail);
      const expiredHeadline = expiredPresentation.headline;
      await page.clock.fastForward(60_001);
      await expect(detail.getByRole("heading")).toHaveText(expiredHeadline);
      await expect(summary.locator("strong")).toHaveText(expiredHeadline);
      await page.getByRole("link", { name: "Launchplane home", exact: true }).first().click();
      await expect(page.locator(".directory-preview > strong")).toHaveText(expiredHeadline);
      await expect(page.locator(".directory-preview")).toContainText(expiredPresentation.detail);
    }
    if (state.scan === "nonempty") {
      await expect(detail).toContainText(expected.detail);
    }
    expect(mutations).toEqual([]);
  });
}
