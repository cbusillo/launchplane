import { expect, test } from "./state";
import { ownerReviewPath } from "../../src/route-model";

for (const viewer of ["admin", "client", "client-reader", "other", "denied-admin"] as const) {
  test(`Product release review link for ${viewer}`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await page.goto("/ui/products?fixture=products");
    const { product, identity, review } = await page.evaluate(async () => {
      const modulePath = "/ui/src/dev-fixtures.ts";
      const fixtures = await import(modulePath) as typeof import("../../src/dev-fixtures");
      return {
        product: fixtures.productsForFixture("products")[0],
        identity: fixtures.fixtureIdentity,
        review: fixtures.releaseReviewForFixture("products"),
      };
    });
    identity.role = viewer === "client" ? "owner" : viewer === "client-reader" || viewer === "other" ? "read_only" : "admin";
    review.product = product.product;
    review.display_name = product.display_name;
    review.viewer_is_owner = viewer === "client" || viewer === "client-reader";
    review.can_override = viewer === "admin";
    await page.route("**/v1/**", route => route.fulfill({ status: 403, json: { error: { code: "authorization_denied", message: "Unavailable" } } }));
    await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", csrf_token: "fixture", identity } }));
    await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: [product] } }));
    await page.route(`**/v1/products/${product.product}`, route => route.fulfill({ json: { status: "ok", trace_id: "product", product } }));
    let checklistReads = 0;
    await page.route("**/v1/release-review?*", route => {
      checklistReads += 1;
      return viewer === "denied-admin"
        ? route.fulfill({ status: 403, json: { error: { code: "release_review_unavailable", message: "Unavailable" } } })
        : route.fulfill({ json: review });
    });
    await page.route("**/v1/release-review/access?*", route => viewer === "denied-admin"
      ? route.fulfill({ status: 403, json: { error: { code: "release_review_unavailable", message: "Unavailable" } } })
      : route.fulfill({ json: { product: product.product, display_name: product.display_name, viewer_is_owner: review.viewer_is_owner } }));
    await page.goto(`/ui/products/${product.product}`);
    await expect(page.getByRole("heading", { name: product.display_name, exact: true }).first()).toBeVisible();
    const link = page.getByRole("link", { name: "Release review", exact: true });
    await expect(page.locator('[data-release-review-access="settled"]')).toBeVisible();
    expect(checklistReads).toBe(0);
    if (viewer === "other" || viewer === "denied-admin") {
      await expect(link).toHaveCount(0);
    } else {
      await expect(link).toBeVisible();
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`release-link-${viewer}.png`), fullPage: true });
      await link.click();
      await expect(page).toHaveURL(`${new URL(ownerReviewPath(), page.url())}?product=${product.product}`);
      await expect(page.locator('[data-route-heading]')).toBeVisible();
    }
  });
}
