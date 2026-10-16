import { expect, setClockTime, test, type Page } from "./state";
import type { Locator } from "@playwright/test";

// Each badge's container, label and indicator must all carry the color the
// shared badge rule gives its state, measured on a bare badge of that state.
async function expectEvidenceColors(badges: Locator) {
  const colors = await badges.evaluateAll(elements => elements.map(badge => {
    const reference = document.createElement("span");
    reference.className = "evidence-badge";
    reference.dataset.state = badge.dataset.state;
    document.body.append(reference);
    const expected = getComputedStyle(reference).color;
    reference.remove();
    return {
      state: badge.dataset.state,
      expected,
      badge: getComputedStyle(badge).color,
      label: getComputedStyle(badge.querySelector("strong")!).color,
      indicator: getComputedStyle(badge.querySelector(":scope > span")!).backgroundColor,
    };
  }));
  expect(colors.length).toBeGreaterThan(0);
  for (const { expected, state, ...rendered } of colors) {
    expect(rendered, `${state} evidence badge`).toEqual({ badge: expected, label: expected, indicator: expected });
  }
}

async function themeColor(page: Page, scope: Locator, name: string) {
  return scope.evaluate((element, variable) => {
    const probe = document.createElement("span");
    probe.style.color = `var(--${variable})`;
    element.append(probe);
    const color = getComputedStyle(probe).color;
    probe.remove();
    return color;
  }, name);
}

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
    // The failed lane's headline keeps its health color; its badge does not.
    const danger = await themeColor(page, failedLane, "danger");
    await expect(failedLane.locator(":scope > strong")).toHaveCSS("color", danger);
    await expect(failedLane.locator(".evidence-badge")).not.toHaveCSS("color", danger);
    await expectEvidenceColors(row.locator(".directory-lane .evidence-badge"));
    await row.screenshot({ path: `../tmp/browser-smoke/evidence-badge-color-${state}-${testInfo.project.name}.png` });
  }
});

test("TLS domain evidence badges keep their evidence colors", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await page.goto("/ui/products/atlas-commerce/environments/prod?fixture=products");
  const list = page.locator(".tls-domain-list");
  await expect(list.locator(".evidence-badge").first()).toBeVisible();
  await list.screenshot({ path: `../tmp/browser-smoke/evidence-badge-color-tls-${testInfo.project.name}.png` });
  await expectEvidenceColors(list.locator(".evidence-badge"));
});
