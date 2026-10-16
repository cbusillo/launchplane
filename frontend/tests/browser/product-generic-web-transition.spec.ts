import { expect, test } from "./state";

async function arrange(page: import("@playwright/test").Page, failure: "none" | "uncertain" | "uncertain-changed" | "stale" = "none", omitPreviewExtensions = false) {
  await page.goto("/ui/products/atlas-commerce?fixture=products");
  const { products, fixtureIdentity } = await page.evaluate(async () => {
    const fixtures = await import("/ui/src/dev-fixtures.ts");
    return { products: fixtures.productsForFixture("products"), fixtureIdentity: fixtures.fixtureIdentity };
  });
  let stored = {
    driver_id: "verireel", display_name: "Atlas Commerce", repository: "example/atlas-commerce",
    owner: { github_login: "example-owner", github_id: "9001" }, image: { repository: "ghcr.io/example/atlas-commerce" },
    production_use: "live", public_website: "required", release_on_acceptance: "held",
    lanes: [{ context: "atlas", instance: "testing", generic_web_extensions: { maintenance: [], schedules: [] } }, { context: "atlas", instance: "prod", generic_web_extensions: { maintenance: [], schedules: [] } }],
    preview: { data_transport_mode: "driver", transport_extension: "", transport_database_prefix: "", app_name_suffix: "" },
  };
  const requests: Array<{ body: Record<string, unknown>; key: string }> = [];
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "csrf", identity: fixtureIdentity } }));
  await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products } }));
  await page.route("**/v1/products/atlas-commerce", route => route.fulfill({ json: { status: "ok", trace_id: "product", product: products[0] } }));
  await page.route("**/v1/product-profiles/atlas-commerce", route => route.fulfill({ json: { status: "ok", trace_id: "profile", profile: stored } }));
  await page.route("**/v1/product-profiles/atlas-commerce/generic-web-transition", async route => {
    const body = route.request().postDataJSON();
    if (body.mode === "apply") {
      requests.push({ body, key: route.request().headers()["idempotency-key"] });
      if (failure === "stale") {
        stored = { ...stored, driver_id: "generic-web" };
        await route.fulfill({ status: 409, json: { trace_id: "stale", error: { code: "stale", message: "Review a new profile." } } }); return;
      }
      stored = { ...stored, driver_id: "generic-web", lanes: body.lanes.map((lane: { context: string; instance: string; extensions: typeof stored.lanes[0]["generic_web_extensions"] }) => ({ context: lane.context, instance: lane.instance, generic_web_extensions: lane.extensions })), preview: body.preview };
      if (omitPreviewExtensions) {
        const { generic_web_extensions: _omitted, ...preview } = body.preview;
        stored = { ...stored, preview };
      }
      if (failure === "uncertain-changed") stored = { ...stored, preview: { ...stored.preview, app_name_suffix: "-concurrent" } };
      if (failure === "uncertain" || failure === "uncertain-changed") { await route.abort("failed"); return; }
    }
    await route.fulfill({ status: 202, json: { status: "accepted", trace_id: "transition", records: { product_profile: "atlas-commerce" }, result: {
      product: "atlas-commerce", driver_before: "verireel", driver_after: "generic-web", changed: true, applied: body.mode === "apply", plan_sha256: "a".repeat(64), lanes: body.lanes, preview: body.preview,
    } } });
  });
  await page.route("**/v1/product-profiles/atlas-commerce/mutation-receipts/generic-web-transition?*", route => route.fulfill({ json: { trace_id: "receipt", product: "atlas-commerce", field: "generic-web-transition", state: "completed", idempotency_key: requests[0]?.key, original_trace_id: "original-transition" } }));
  await page.goto("/ui/products/atlas-commerce");
  const panel = page.getByRole("region", { name: "Shared web driver", exact: true });
  await expect(panel.getByRole("button", { name: "Review shared-web transition" })).toBeDisabled();
  await expect(panel.locator("output")).toHaveText("verireel");
  const definition = { lanes: [
    { context: "atlas", instance: "testing", extensions: { verification_extension: "verireel-testing", maintenance: [], schedules: [] } },
    { context: "atlas", instance: "prod", extensions: { maintenance: [{ name: "migrate", command: "node /app/migrate.js", timeout_seconds: 300 }], schedules: [{ name: "recover", command: "node /app/recover.js", timeout_seconds: 300, cron_expression: "*/10 * * * *", enabled: true }] } },
  ], preview: { data_transport_mode: "driver", transport_extension: "verireel-postgres", transport_database_prefix: "atlas", app_name_suffix: "-app" } };
  await panel.getByLabel("Lane and preview declarations").fill(JSON.stringify(definition, null, 2));
  await panel.getByLabel("Change reason").fill("Review declared lifecycle before testing qualification.");
  return { panel, requests };
}

test("shared-web review, draft invalidation, apply and exact readback", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const { panel, requests } = await arrange(page);
  await panel.getByRole("button", { name: "Review shared-web transition" }).click();
  const apply = panel.getByRole("button", { name: "Apply shared-web profile" });
  await expect(apply).toBeEnabled();
  await expect(panel.locator(".product-owner-plan")).toContainText("verireel-testing");
  await panel.getByLabel("Change reason").fill("Revised review.");
  await expect(apply).toBeDisabled();
  await panel.getByRole("button", { name: "Review shared-web transition" }).click();
  await apply.click();
  await expect(panel.locator("p[role=status]")).toBeVisible();
  await expect(panel.locator("output")).toHaveText("generic-web");
  expect(requests).toHaveLength(1);
  expect(requests[0].key).toBeTruthy();
  expect(requests[0].body.reviewed_plan_sha256).toBeTruthy();
  await expect(apply).toBeDisabled();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await panel.screenshot({ path: testInfo.outputPath("shared-web-applied.png") });
  await page.mouse.wheel(0, -180);
  await page.getByRole("heading", { name: "Shared web driver", exact: true }).click();
  await page.screenshot({ path: testInfo.outputPath("shared-web-visible.png") });
});

for (const configured of [false, true]) {
  test(`preview maintenance and schedules read back after ${configured ? "declaration" : "clearing"}`, async ({ page }) => {
    // Empty extensions are omitted from stored-profile responses by the service.
    const { panel, requests } = await arrange(page, "none", !configured);
    const input = panel.getByLabel("Lane and preview declarations");
    const definition = JSON.parse(await input.inputValue());
    const extensions = configured ? {
      maintenance: [{ name: "migrate", command: "node /app/migrate.js", timeout_seconds: 300, on_reset: false }],
      schedules: [{ name: "billing", command: "node /app/billing.js", timeout_seconds: 300, on_reset: false, cron_expression: "*/10 * * * *", enabled: true }],
    } : { maintenance: [], schedules: [] };
    definition.preview.generic_web_extensions = extensions;
    await input.fill(JSON.stringify(definition));
    await panel.getByRole("button", { name: "Review shared-web transition" }).click();
    await panel.getByRole("button", { name: "Apply shared-web profile" }).click();
    await expect(panel.locator("p[role=status]")).toBeVisible();
    await expect(panel.getByRole("alert")).toHaveCount(0);
    expect(requests).toHaveLength(1);
    expect((requests[0].body.preview as { generic_web_extensions: unknown }).generic_web_extensions).toEqual(extensions);
  });
}

test("lost transition response survives reload and settles only from original receipt", async ({ page }) => {
  const { panel, requests } = await arrange(page, "uncertain");
  await panel.getByRole("button", { name: "Review shared-web transition" }).click();
  await panel.getByRole("button", { name: "Apply shared-web profile" }).click();
  await expect(panel.getByRole("button", { name: "Check original operation" })).toBeEnabled();
  await expect(panel.getByLabel("Lane and preview declarations")).toBeDisabled();
  const first = requests[0];
  await page.reload();
  const reloaded = page.getByRole("region", { name: "Shared web driver", exact: true });
  await expect(reloaded.getByRole("button", { name: "Retry original transition" })).toBeEnabled();
  await reloaded.getByRole("button", { name: "Check original operation" }).click();
  await expect(reloaded.locator("p[role=status]")).toBeVisible();
  await expect(reloaded.locator("output")).toHaveText("generic-web");
  await expect(reloaded.getByLabel("Lane and preview declarations")).toBeEnabled();
  expect(requests).toEqual([first]);
});

test("a stale transition refreshes the profile and requires another review", async ({ page }) => {
  const { panel, requests } = await arrange(page, "stale");
  await panel.getByRole("button", { name: "Review shared-web transition" }).click();
  await panel.getByRole("button", { name: "Apply shared-web profile" }).click();
  await expect(panel.locator("output")).toHaveText("generic-web");
  await expect(panel.getByRole("button", { name: "Apply shared-web profile" })).toBeDisabled();
  await expect(panel.getByRole("button", { name: "Review shared-web transition" })).toBeEnabled();
  expect(requests).toHaveLength(1);
});


test("a completed original receipt exposes later declaration changes", async ({ page }) => {
  const { panel, requests } = await arrange(page, "uncertain-changed");
  await panel.getByRole("button", { name: "Review shared-web transition" }).click();
  await panel.getByRole("button", { name: "Apply shared-web profile" }).click();
  await panel.getByRole("button", { name: "Check original operation" }).click();
  await expect(panel.getByRole("alert")).toBeVisible();
  await expect(panel.getByRole("button", { name: "Apply shared-web profile" })).toBeDisabled();
  await expect(panel.getByLabel("Lane and preview declarations")).toBeEnabled();
  expect(requests).toHaveLength(1);
});
