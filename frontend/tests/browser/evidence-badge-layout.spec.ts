import { expect, setClockTime, test } from "./state";

test("lane evidence stays readable within its column", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await page.goto("/ui/products?fixture=products");
  const { product, identity } = await page.evaluate(async () => {
    const modulePath = "/ui/src/dev-fixtures.ts";
    const module = await import(modulePath) as typeof import("../../src/dev-fixtures");
    return { product: structuredClone(module.productsForFixture("products")[0]), identity: module.fixtureIdentity };
  });
  await setClockTime(page, product.provenance.refreshed_at);
  await page.route("**/v1/**", route => route.fulfill({ status: 403, json: { error: { code: "unused_fixture_read" } } }));
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", csrf_token: "fixture", identity } }));
  await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", products: [product] } }));
  await page.route(`**/v1/products/${product.product}`, route => route.fulfill({ json: { status: "ok", product } }));
  await page.goto("/ui/products");
  const row = page.locator(".product-directory-row");
  await expect(row).toBeVisible();
  for (const state of ["verified", "missing", "stale"] as const) {
    for (const lane of product.environments) lane.topology.trust_state = state;
    await page.getByRole("button", { name: "Refresh current evidence" }).click();
    const badges = row.locator(".directory-lane .evidence-badge");
    await expect(badges).toHaveCount(product.environments.length);
    for (const badge of await badges.all()) {
      await expect(badge).toHaveAttribute("data-state", state);
      await expect(badge.locator("strong")).toBeVisible();
    }
    // Check the rendered text, not just the clipped badge's own box. The
    // entire label must fit its lane, without ellipsis or hidden overflow.
    expect(await row.locator(".directory-lane").evaluateAll(lanes => lanes.every(lane => {
      const bounds = lane.getBoundingClientRect();
      const badge = lane.querySelector<HTMLElement>(".evidence-badge")!;
      const label = badge.querySelector("strong")!;
      const range = document.createRange();
      range.selectNodeContents(label);
      const badgeBounds = badge.getBoundingClientRect();
      return label.scrollWidth <= label.clientWidth
        && label.scrollHeight <= label.clientHeight
        && Array.from(range.getClientRects()).every(rect =>
          rect.left >= bounds.left && rect.right <= bounds.right + 1
          && rect.top >= badgeBounds.top && rect.bottom <= badgeBounds.bottom + 1);
    }))).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `../tmp/browser-smoke/evidence-badge-${state}-${testInfo.project.name}.png`, fullPage: true });
  }
  await row.click();
  await expect(page.getByRole("heading", { name: product.display_name, exact: true })).toBeVisible();
});
