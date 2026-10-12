import { expect, test } from "@playwright/test";

for (const outcome of ["applied", "lost-response", "stale", "denied", "storage-failure"] as const) {
  test(`No-website cleanup ${outcome}: reviewed atomic request and recovery @responsive`, async ({ page }, testInfo) => {
    await page.goto("/ui/products/atlas-commerce?fixture=products");
    const { products, fixtureIdentity } = await page.evaluate(async () => {
      const fixtures = await import("/ui/src/dev-fixtures.ts");
      return { products: fixtures.productsForFixture("products"), fixtureIdentity: fixtures.fixtureIdentity };
    });
    const sent: Array<{ key: string; body: Record<string, unknown> }> = [];
    let publicWebsite = "required";
    let productReads = 0;
    let receiptCompleted = false;
    await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "csrf", identity: fixtureIdentity } }));
    await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products } }));
    await page.route("**/v1/products/atlas-commerce", route => {
      productReads++;
      return route.fulfill({ json: { status: "ok", trace_id: "product", product: products[0] } });
    });
    await page.route("**/v1/product-profiles/atlas-commerce", route => route.fulfill({ json: {
      status: "ok", trace_id: "profile", profile: {
        public_website: publicWebsite, repository: "example/atlas-commerce",
        owner: { github_login: "example-owner", github_id: "9001" },
        image: { repository: "ghcr.io/example/atlas-commerce" }, production_use: "live",
        lanes: [{ context: "atlas", instance: "testing", health_monitoring: { monitoring_intent: "private" } }],
      },
    } }));
    await page.route("**/v1/product-profiles/atlas-commerce/website-cleanup", async route => {
      const body = route.request().postDataJSON();
      if (outcome === "denied") {
        await route.fulfill({ status: 403, json: { trace_id: "denial", error: { code: "authorization_denied", message: "Caller cannot change this product profile." } } }); return;
      }
      if (body.mode === "apply") {
        sent.push({ key: route.request().headers()["idempotency-key"], body });
        if (outcome === "stale") {
          await route.fulfill({ status: 409, json: { trace_id: "stale", error: { code: "stale", message: "Profile changed. Preview again." } } }); return;
        }
        publicWebsite = "none";
        if (outcome === "lost-response") { await route.abort("failed"); return; }
      }
      await route.fulfill({ status: 202, json: { status: "accepted", trace_id: "cleanup", records: { product_profile: "atlas-commerce" }, result: {
        product: "atlas-commerce", public_website_after: "none", changed: true,
        applied: body.mode === "apply", plan_sha256: "a".repeat(64), changes: [
          { path: "public_website", before: "required", after: "none" },
          { path: "lanes[0] (atlas/testing).health_url", before: "https://obsolete.example.test/launchplane/health", after: "" },
          { path: "lanes[0] (atlas/testing).checks[1] (public-ingress).enabled", before: true, after: false },
        ],
      } } });
    });
    await page.route("**/v1/product-profiles/atlas-commerce/mutation-receipts/website-cleanup?*", route => route.fulfill({ json: {
      trace_id: "receipt", product: "atlas-commerce", field: "website-cleanup",
      idempotency_key: new URL(route.request().url()).searchParams.get("operation_key"),
      state: receiptCompleted ? "completed" : "unresolved", original_trace_id: receiptCompleted ? "cleanup" : "",
    } }));
    if (outcome === "storage-failure") await page.addInitScript(() => {
      const original = Storage.prototype.setItem;
      Storage.prototype.setItem = function(key, value) {
        if (key.endsWith(":website-cleanup") && key.startsWith("launchplane:product-profile-draft:")) throw new DOMException("Quota exceeded", "QuotaExceededError");
        return original.call(this, key, value);
      };
    });
    await page.goto("/ui/products/atlas-commerce");
    let panel = page.getByRole("region", { name: "Public website", exact: true });
    await expect(panel.getByText("Current: Public website required", { exact: true })).toBeVisible();
    const preview = panel.getByRole("button", { name: "Preview no-website cleanup", exact: true });
    const apply = panel.getByRole("button", { name: "Apply no-website cleanup", exact: true });
    await expect(apply).toBeDisabled();
    await panel.getByLabel("Change reason").fill("This product has no public website.");
    await preview.click();
    if (outcome === "denied") {
      await expect(panel).toContainText("Caller cannot change this product profile.");
      await expect(apply).toBeDisabled(); expect(sent).toEqual([]); return;
    }
    await expect(panel).toContainText("Reviewed changes");
    await expect(apply).toBeEnabled();
    await panel.getByLabel("Change reason").fill("Reason changed after review.");
    await expect(apply).toBeDisabled();
    await preview.click();
    await expect(apply).toBeEnabled();
    await panel.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await panel.screenshot({ path: testInfo.outputPath("website-cleanup-reviewed.png") });
    await apply.click();
    if (outcome === "storage-failure") {
      await expect(panel).toContainText("Nothing was sent."); expect(sent).toEqual([]); return;
    }
    await expect.poll(() => sent.length).toBe(1);
    expect(sent[0].key).toBeTruthy();
    expect(sent[0].body).toEqual({ schema_version: 1, mode: "apply", reason: "Reason changed after review.", reviewed_plan_sha256: "a".repeat(64) });
    if (outcome === "stale") {
      await expect(panel).toContainText("Profile changed. Preview again.");
      await expect(preview).toBeEnabled();
    } else if (outcome === "lost-response") {
      await expect(preview).toBeDisabled();
      // Lost reviewed draft must still recover only through a completed receipt.
      await page.evaluate(() => sessionStorage.removeItem("launchplane:product-profile-draft:atlas-commerce:website-cleanup"));
      await page.reload();
      panel = page.getByRole("region", { name: "Public website", exact: true });
      const check = panel.getByRole("button", { name: "Check original operation", exact: true });
      await check.click();
      await expect(panel).toContainText("another change remains locked");
      await expect(panel.getByRole("button", { name: "Preview no-website cleanup", exact: true })).toBeDisabled();
      receiptCompleted = true;
      await check.click();
      await expect(panel).toContainText("Original cleanup completed.");
      await panel.getByLabel("Change reason").fill("A fresh reviewed request after receipt recovery.");
      await expect(panel.getByRole("button", { name: "Preview no-website cleanup", exact: true })).toBeEnabled();
      expect(sent).toHaveLength(1);
    } else {
      await expect(panel).toContainText("Applied and read back: no public website.");
      await expect(apply).toBeDisabled();
      expect(productReads).toBeGreaterThan(1);
    }
    await panel.screenshot({ path: testInfo.outputPath(`website-cleanup-${outcome}.png`) });
  });
}
