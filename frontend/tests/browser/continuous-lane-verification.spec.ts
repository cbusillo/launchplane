import { expect, test } from "@playwright/test";

test("an open workspace stops showing green when monitor evidence expires", async ({ page }) => {
  await page.goto("/ui/products?fixture=products");
  const { product, expiry } = await page.evaluate(async () => {
    const modulePath = "/ui/src/dev-fixtures.ts";
    const { productsForFixture } = await import(modulePath);
    const product = productsForFixture("products")[0];
    const lane = product.environments.find((environment: { environment: string }) => environment.environment === "testing");
    return { product: product.product, expiry: Date.parse(lane.provenance.stale_after) };
  });
  await page.clock.install({ time: new Date(expiry - 1000) });
  await page.goto(`/ui/products/${product}?fixture=products`);
  const signal = page.locator(".signal-tile").filter({ hasText: /^Testing/i });
  await expect(signal).toHaveAttribute("data-tone", "verified");
  await page.clock.runFor(2000);
  await expect(signal).toHaveAttribute("data-tone", "warning");
  await expect(signal).toContainText("Review warning");
});
