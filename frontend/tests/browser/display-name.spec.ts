import { expect, test } from "@playwright/test";

for (const outcome of ["applied", "lost-response", "lost-response-stale", "stale", "denied", "storage-failure"] as const) {
  test(`Display-name change ${outcome}: reviewed atomic request and recovery @responsive`, async ({ page }, testInfo) => {
    await page.goto("/ui/products/atlas-commerce?fixture=products");
    const { products, fixtureIdentity } = await page.evaluate(async () => {
      const fixtures = await import("/ui/src/dev-fixtures.ts");
      return { products: fixtures.productsForFixture("products"), fixtureIdentity: fixtures.fixtureIdentity };
    });
    const draftKey = await page.evaluate(async () => {
      const panel = await import("/ui/src/ProductDisplayNamePanel.tsx");
      return panel.productDisplayNameDraftKey("atlas-commerce");
    });
    const sent: Array<{ key: string; body: Record<string, unknown> }> = [];
    let displayName = products[0].display_name;
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
        display_name: displayName, repository: "example/atlas-commerce",
        owner: { github_login: "example-owner", github_id: "9001" },
        image: { repository: "ghcr.io/example/atlas-commerce" }, production_use: "live",
        lanes: [{ context: "atlas", instance: "testing", health_monitoring: { monitoring_intent: "private" } }],
      },
    } }));
    await page.route("**/v1/product-profiles/atlas-commerce/display-name", async route => {
      const body = route.request().postDataJSON();
      if (outcome === "denied") {
        await route.fulfill({ status: 403, json: { trace_id: "denial", error: { code: "authorization_denied", message: "Caller cannot change this product profile." } } }); return;
      }
      if (body.mode === "apply") {
        sent.push({ key: route.request().headers()["idempotency-key"], body });
        if (outcome === "stale" || outcome === "lost-response-stale" && sent.length > 1) {
          await route.fulfill({ status: 409, json: { trace_id: "stale", error: { code: "stale", message: "Profile changed. Preview again." } } }); return;
        }
        // Here the original request fails before commit; the subsequent retry is stale.
        if (outcome === "lost-response-stale") { await route.abort("failed"); return; }
        displayName = "Example Website"; products[0].display_name = displayName;
        if (outcome === "lost-response" || outcome === "lost-response-stale") { await route.abort("failed"); return; }
      }
      await route.fulfill({ status: 202, json: { status: "accepted", trace_id: "rename", records: { product_profile: "atlas-commerce" }, result: {
        product: "atlas-commerce", display_name_before: "Atlas Commerce", display_name_after: body.display_name,
        changed: true, applied: body.mode === "apply", plan_sha256: "a".repeat(64),
      } } });
    });
    await page.route("**/v1/product-profiles/atlas-commerce/mutation-receipts/display-name?*", route => route.fulfill({ json: {
      trace_id: "receipt", product: "atlas-commerce", field: "display-name",
      idempotency_key: new URL(route.request().url()).searchParams.get("operation_key"),
      state: receiptCompleted ? "completed" : "unresolved", original_trace_id: receiptCompleted ? "rename" : "",
    } }));
    if (outcome === "storage-failure") await page.addInitScript(draftKey => {
      const original = Storage.prototype.setItem;
      Storage.prototype.setItem = function(key, value) {
        if (key === draftKey) throw new DOMException("Quota exceeded", "QuotaExceededError");
        return original.call(this, key, value);
      };
    }, draftKey);
    await page.goto("/ui/products/atlas-commerce");
    let panel = page.getByRole("region", { name: "Display name", exact: true });
    await expect(panel.getByLabel("Current display name")).toHaveText(products[0].display_name);
    const preview = panel.getByRole("button", { name: "Preview name change", exact: true });
    const apply = panel.getByRole("button", { name: "Apply name change", exact: true });
    await expect(apply).toBeDisabled();
    await panel.getByLabel("New display name").fill("Example Website");
    await panel.getByLabel("Change reason").fill("Clean the sidebar.");
    await preview.click();
    if (outcome === "denied") {
      await expect(panel).toContainText("Caller cannot change this product profile.");
      await expect(apply).toBeDisabled(); expect(sent).toEqual([]); return;
    }
    await expect(apply).toBeEnabled();
    await panel.getByLabel("New display name").fill("Unreviewed Name");
    await expect(apply).toBeDisabled();
    await panel.getByLabel("New display name").fill("Example Website");
    await expect(apply).toBeEnabled();
    await panel.getByLabel("Change reason").fill("Reason changed after review.");
    await expect(apply).toBeDisabled();
    await preview.click();
    await expect(apply).toBeEnabled();
    await panel.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await panel.screenshot({ path: testInfo.outputPath("display-name-reviewed.png") });
    await apply.click();
    if (outcome === "storage-failure") {
      await expect(panel.getByRole("alert")).toBeVisible(); expect(sent).toEqual([]); return;
    }
    await expect.poll(() => sent.length).toBe(1);
    expect(sent[0].key).toBeTruthy();
    expect(sent[0].body).toEqual({ display_name: "Example Website", schema_version: 1, mode: "apply", reason: "Reason changed after review.", reviewed_plan_sha256: "a".repeat(64) });
    if (outcome === "stale") {
      await expect(panel).toContainText("Profile changed. Preview again.");
      await expect(preview).toBeEnabled();
      await expect(apply).toBeDisabled();
    } else if (outcome === "lost-response" || outcome === "lost-response-stale") {
      await expect(preview).toBeDisabled();
      if (outcome === "lost-response-stale") {
        await panel.getByRole("button", { name: "Retry original name change", exact: true }).click();
        await expect(panel).toContainText("Profile changed. Preview again.");
        await expect(preview).toBeEnabled();
        await expect(apply).toBeDisabled();
        await expect(panel.getByLabel("Change reason")).toBeEnabled();
        await expect(panel.getByRole("button", { name: "Check original operation", exact: true })).toHaveCount(0);
        expect(sent[1]).toEqual(sent[0]);
        await panel.screenshot({ path: testInfo.outputPath("display-name-settled-stale.png") });
        return;
      }
      // Lost reviewed draft must still recover only through a completed receipt.
      await page.evaluate(key => {
        if (sessionStorage.getItem(key) === null) throw new Error("The reviewed draft was not preserved before Apply.");
        sessionStorage.removeItem(key);
      }, draftKey);
      await page.reload();
      panel = page.getByRole("region", { name: "Display name", exact: true });
      const check = panel.getByRole("button", { name: "Check original operation", exact: true });
      await check.click();
      await expect(panel.getByRole("button", { name: "Check original operation", exact: true })).toBeEnabled();
      await expect(panel.getByRole("button", { name: "Preview name change", exact: true })).toBeDisabled();
      receiptCompleted = true;
      await check.click();
      await expect(panel.getByRole("button", { name: "Check original operation", exact: true })).toHaveCount(0);
      await panel.getByLabel("New display name").fill("Another Website");
      await panel.getByLabel("Change reason").fill("A fresh reviewed request after receipt recovery.");
      await expect(panel.getByRole("button", { name: "Preview name change", exact: true })).toBeEnabled();
      expect(sent).toHaveLength(1);
    } else {
      await expect(panel.getByLabel("Current display name")).toHaveText("Example Website");
      await expect(apply).toBeDisabled();
      expect(productReads).toBeGreaterThan(1);
      await expect(page.locator('.rail-product-link span[title="Example Website"]')).toHaveText("Example Website");
    }
    await panel.screenshot({ path: testInfo.outputPath(`display-name-${outcome}.png`) });
  });
}
