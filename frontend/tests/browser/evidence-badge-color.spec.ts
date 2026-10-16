import { expect, setClockTime, test } from "./state";

test("lane evidence badge colors follow evidence, not lane health", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await page.goto("/ui/products?fixture=products");
  const { product, identity } = await page.evaluate(async () => {
    const modulePath = "/ui/src/dev-fixtures.ts";
    const module = await import(modulePath) as typeof import("../../src/dev-fixtures");
    return { product: structuredClone(module.productsForFixture("products")[0]), identity: module.fixtureIdentity };
  });
  await setClockTime(page, product.provenance.refreshed_at);
  // The fixture's production lane has an open public-ingress incident. Give it
  // verified evidence, and weaken the healthy testing lane's evidence instead.
  const lane = (name: string) => product.environments.find(environment => environment.environment === name)!;
  lane("prod").topology.trust_state = "verified";
  const healthy = lane("testing");
  await page.route("**/v1/**", route => route.fulfill({ status: 403, json: { error: { code: "unused_fixture_read" } } }));
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", csrf_token: "fixture", identity } }));
  await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", products: [product] } }));
  await page.route(`**/v1/products/${product.product}`, route => route.fulfill({ json: { status: "ok", product } }));
  await page.goto("/ui/products");
  const row = page.locator(".product-directory-row");
  await expect(row).toBeVisible();
  for (const state of ["missing", "stale"] as const) {
    healthy.topology.trust_state = state;
    await page.getByRole("button", { name: "Refresh current evidence" }).click();
    const failedLane = row.locator('.directory-lane[data-lane="prod"]');
    const healthyLane = row.locator('.directory-lane[data-lane="testing"]');
    await expect(failedLane).toHaveAttribute("data-operational", "danger");
    await expect(failedLane.locator(".evidence-badge")).toHaveAttribute("data-state", "verified");
    await expect(healthyLane).not.toHaveAttribute("data-operational", /danger|warning/);
    await expect(healthyLane.locator(".evidence-badge")).toHaveAttribute("data-state", state);
    const colors = await row.locator(".directory-lane").evaluateAll(elements => elements.map(lane => {
      const tone = lane.getAttribute("data-operational");
      const probe = document.createElement("span");
      probe.style.color = `var(--${tone})`;
      lane.append(probe);
      const toneColor = getComputedStyle(probe).color;
      probe.remove();
      const badge = lane.querySelector<HTMLElement>(".evidence-badge")!;
      return {
        tone,
        toneColor,
        headline: getComputedStyle(lane.querySelector(":scope > strong")!).color,
        badge: getComputedStyle(badge).color,
        label: getComputedStyle(badge.querySelector("strong")!).color,
        indicator: getComputedStyle(badge.querySelector(":scope > span")!).backgroundColor,
      };
    }));
    // The failed lane's headline still carries its health color, while each
    // badge's label and indicator agree with that badge's own evidence color.
    const failedColors = colors.find(color => color.tone === "danger")!;
    expect(failedColors.headline).toBe(failedColors.toneColor);
    expect(failedColors.badge).not.toBe(failedColors.toneColor);
    for (const color of colors) {
      expect(color.label).toBe(color.badge);
      expect(color.indicator).toBe(color.badge);
    }
    await row.screenshot({ path: `../tmp/browser-smoke/evidence-badge-color-${state}-${testInfo.project.name}.png` });
  }
});
