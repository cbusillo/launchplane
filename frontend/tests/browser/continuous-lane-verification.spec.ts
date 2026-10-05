import { expect, test } from "@playwright/test";

for (const detail of [false, true]) test(`an open ${detail ? "environment" : "workspace"} expires monitor verification`, async ({ page }) => {
  await page.goto("/ui/products?fixture=products");
  const { product, expiry } = await page.evaluate(async () => {
    const modulePath = "/ui/src/dev-fixtures.ts";
    const { productsForFixture } = await import(modulePath);
    const product = productsForFixture("products")[0];
    const lane = product.environments.find((environment: { environment: string }) => environment.environment === "testing");
    return { product: product.product, expiry: Date.parse(lane.provenance.stale_after) };
  });
  await page.clock.install({ time: new Date(expiry - 1000) });
  await page.goto(`/ui/products/${product}${detail ? "/environments/testing" : ""}?fixture=products`);
  const signal = detail
    ? page.locator(".condition-tile").filter({ hasText: /^Runtime identity/i })
    : page.locator(".signal-tile").filter({ hasText: /^Testing/i });
  await expect(signal).toHaveAttribute("data-tone", detail ? "pass" : "verified");
  await page.clock.runFor(2000);
  await expect(signal).toHaveAttribute("data-tone", "warning");
  await expect(signal).toContainText(detail ? "Stale" : "Review warning");
  await expect(signal).toContainText("Stale");
});
