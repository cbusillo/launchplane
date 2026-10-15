import { expect, test } from "@playwright/test";
import { READ_REFRESH_INTERVAL_MS } from "../../src/use-evidence-refresh";

for (const view of ["products", "workspace", "environment"] as const) {
  for (const trigger of ["timer", "visibility"] as const) {
    test(`background ${view} ${trigger} expiry shows sign-in and recovers fresh evidence @responsive`, async ({ page }, testInfo) => {
      const clockStart = Date.now() + 60_000;
      await page.clock.install({ time: new Date(clockStart) });
      await page.clock.pauseAt(new Date(clockStart + 60_000));
      await page.goto("/ui/products?fixture=products");
      const fixture = await page.evaluate(async () => {
        const modulePath = "/ui/src/dev-fixtures.ts";
        const module = await import(modulePath);
        const products = module.productsForFixture("products");
        return { products, product: products[0], identity: module.fixtureIdentity,
          detail: module.environmentForFixture("products", products[0].product, "testing") };
      });
      let signedIn = false;
      let expired = false;
      let recovered = false;
      let sessionReads = 0;
      let evidenceReads = 0;
      let rejectedReads = 0;
      const mutations: string[] = [];
      page.on("request", request => {
        if (request.url().includes("/v1/") && request.method() !== "GET") mutations.push(request.url());
      });
      const authFailure = { status: 401, json: { error: { code: "authentication_required", message: "Sign in required" } } };
      await page.route("**/v1/**", route => route.fulfill({ status: 403, json: { error: { code: "authorization_denied" } } }));
      await page.route("**/v1/auth/session", route => {
        sessionReads++;
        return route.fulfill(signedIn
          ? { json: { status: "ok", csrf_token: "fixture", identity: fixture.identity } }
          : authFailure);
      });
      const productUrl = `/v1/products/${fixture.product.product}`;
      const target = view === "products" ? "/v1/products"
        : view === "workspace" ? productUrl : `${productUrl}/environments/testing`;
      await page.route("**/v1/products**", route => {
        const path = new URL(route.request().url()).pathname;
        if (path === target && expired) {
          rejectedReads++;
          return route.fulfill(authFailure);
        }
        evidenceReads++;
        const product = { ...fixture.product, display_name: recovered ? "Fresh recovered product" : fixture.product.display_name };
        if (path === "/v1/products") return route.fulfill({ json: { status: "ok", products: [product] } });
        if (path === productUrl) return route.fulfill({ json: { status: "ok", product } });
        if (path === `${productUrl}/environments/testing`) return route.fulfill({ json: { status: "ok", environment: fixture.detail } });
        return route.fulfill({ status: 403, json: { error: { code: "authorization_denied" } } });
      });
      const returnTo = view === "products" ? "/ui/products?search=Atlas"
        : `/ui/products/${fixture.product.product}${view === "environment" ? "/environments/testing" : ""}?tab=overview`;
      // The fixture session is separate; this route exercises the real auth gate.
      await page.goto(returnTo);
      const signIn = page.getByRole("link", { name: "Sign in with GitHub" });
      await expect(signIn).toBeVisible();
      expect(new URL((await signIn.getAttribute("href"))!, page.url()).searchParams.get("return_to")).toBe(returnTo);
      expect(evidenceReads).toBe(0);
      await page.route("**/auth/github/login?*", route => {
        signedIn = true;
        expired = false;
        const destination = new URL(route.request().url()).searchParams.get("return_to")!;
        return route.fulfill({ status: 302, headers: { location: destination } });
      });
      await signIn.click();
      await expect(page.locator(".control-shell")).toBeVisible();
      await expect(page.locator(".rail-products")).toContainText(fixture.product.display_name);
      expect(sessionReads).toBeGreaterThan(1);
      const beforeExpirySessionReads = sessionReads;
      expired = true;
      signedIn = false;
      if (trigger === "visibility") {
        await page.evaluate(() => Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" }));
        await page.clock.runFor(READ_REFRESH_INTERVAL_MS + 1);
        expect(rejectedReads).toBe(0);
        await page.evaluate(() => {
          Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
          document.dispatchEvent(new Event("visibilitychange"));
          window.dispatchEvent(new Event("focus"));
        });
      } else {
        await page.clock.runFor(READ_REFRESH_INTERVAL_MS + 1);
      }
      await expect(page.locator(".session-card h1")).toContainText(/expired|ended/i);
      await expect(signIn).toBeVisible();
      expect(new URL((await signIn.getAttribute("href"))!, page.url()).searchParams.get("return_to")).toBe(returnTo);
      await expect(page.locator(".control-shell")).toHaveCount(0);
      const stoppedReads = evidenceReads;
      await page.clock.runFor(2 * READ_REFRESH_INTERVAL_MS);
      expect(evidenceReads).toBe(stoppedReads);
      await page.screenshot({ path: `../tmp/browser-smoke/session-${view}-${trigger}-expired-${testInfo.project.name}.png`, fullPage: true });
      recovered = true;
      await signIn.click();
      await expect(page.locator(".control-shell")).toBeVisible();
      await expect(page.locator(".rail-products")).toContainText("Fresh recovered product");
      await expect(page).toHaveURL(new RegExp(returnTo.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "$"));
      expect(sessionReads).toBeGreaterThan(beforeExpirySessionReads);
      expect(mutations).toEqual([]);
      expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
    });
  }
}

for (const status of [403, 503]) {
  test(`${status} background failure keeps its meaning without signing out @responsive`, async ({ page }) => {
    const clockStart = Date.now() + 60_000;
    await page.clock.install({ time: new Date(clockStart) });
    await page.clock.pauseAt(new Date(clockStart + 60_000));
    await page.goto("/ui/products?fixture=products");
    const fixture = await page.evaluate(async () => {
      const modulePath = "/ui/src/dev-fixtures.ts";
      const module = await import(modulePath);
      return { products: module.productsForFixture("products"), identity: module.fixtureIdentity };
    });
    let failing = false;
    await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", csrf_token: "fixture", identity: fixture.identity } }));
    await page.route("**/v1/products", route => route.fulfill(failing
      ? { status, json: { error: { code: status === 403 ? "authorization_denied" : "read_unavailable", message: status === 403 ? "Inventory access denied" : "Inventory temporarily unavailable" } } }
      : { json: { status: "ok", products: fixture.products } }));
    await page.goto("/ui/products");
    await expect(page.locator(".product-directory-row").first()).toBeVisible();
    failing = true;
    await page.clock.runFor(READ_REFRESH_INTERVAL_MS + 1);
    await expect(page.getByText(status === 403 ? "Inventory access denied" : "Inventory temporarily unavailable", { exact: false }).first()).toBeVisible();
    await expect(page.getByRole("link", { name: "Sign in with GitHub" })).toHaveCount(0);
    await expect(page.locator(".product-directory-row").first()).toBeVisible();
    await expect(page.locator(".inline-error")).toContainText(/last|stale|cached/i);
  });
}

test("pending identity verification recovers initial load without another sign-in", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const clockStart = Date.now() + 60_000;
  await page.clock.install({ time: new Date(clockStart) });
  await page.clock.pauseAt(new Date(clockStart + 60_000));
  await page.goto("/ui/products?fixture=products");
  await expect(page.locator(".product-directory-row").first()).toBeVisible();
  const fixture = await page.evaluate(async () => {
    const modulePath = "/ui/src/dev-fixtures.ts";
    const module = await import(modulePath);
    return { products: module.productsForFixture("products"), identity: module.fixtureIdentity };
  });
  let sessionReads = 0;
  await page.route("**/v1/**", route => route.fulfill({ status: 403, json: { error: { code: "authorization_denied" } } }));
  await page.route("**/v1/auth/session", route => {
    sessionReads++;
    return route.fulfill(sessionReads === 1
      ? { status: 503, json: { error: { code: "identity_verification_pending", message: "Verification in progress" } } }
      : { json: { status: "ok", csrf_token: "fixture", identity: fixture.identity } });
  });
  await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", products: fixture.products } }));
  await page.goto("/ui/products");
  await expect(page.locator(".session-card h1")).toBeVisible();
  await expect(page.getByRole("link", { name: "Sign in with GitHub" })).toHaveCount(0);
  await page.screenshot({ path: `../tmp/browser-smoke/session-renewal-pending-${testInfo.project.name}.png`, fullPage: true });
  await page.clock.runFor(60_000);
  await expect(page.locator(".product-directory-row").first()).toBeVisible();
  await page.getByRole("button", { name: "Refresh current evidence" }).click();
  await expect(page.getByRole("link", { name: "Sign in with GitHub" })).toHaveCount(0);
  await page.screenshot({ path: `../tmp/browser-smoke/session-renewal-recovered-${testInfo.project.name}.png`, fullPage: true });
});
