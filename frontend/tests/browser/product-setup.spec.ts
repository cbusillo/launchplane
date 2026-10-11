import { expect, test } from "@playwright/test";

for (const outcome of ["success", "uncertain", "readback", "uncertain-stale", "declaration-uncertain"] as const) {
  test(`signed-in setup: ${outcome}, exact diff, declaration apply and secret navigation`, async ({ page }, testInfo) => {
    await page.goto("/ui/products/atlas-commerce?fixture=products");
    const data = await page.evaluate(async () => {
      const f = await import("/ui/src/dev-fixtures.ts");
      return { products: f.productsForFixture("products"), identity: f.fixtureIdentity,
        environment: f.environmentForFixture("products", "atlas-commerce", "testing"),
        config: f.configStatusForFixture("products", "atlas-commerce", "testing") };
    });
    const profile = { product: "atlas-commerce", owner: { github_login: "example-owner", github_id: "9001" }, image: { repository: "ghcr.io/example/atlas-commerce" }, production_use: "live", release_on_acceptance: "held",
      lanes: [{ context: "atlas-commerce", instance: "prod", base_url: "https://example.com" }],
      expected_config: { runtime_environment_keys: [], managed_secret_bindings: [] as unknown[] } };
    let refuseReadback = false;
    const applies: Array<{ body: Record<string, unknown>; key: string }> = [];
    await page.route("**/v1/**", async route => {
      const path = new URL(route.request().url()).pathname;
      if (route.request().method() === "POST") {
        const body = route.request().postDataJSON();
        const environment = path.includes("stable-lane-repair");
        if (body.mode === "apply") {
          applies.push({ body, key: route.request().headers()["idempotency-key"] });
          if (outcome === "uncertain-stale" && applies.length === 1) { await route.abort("failed"); return; }
          if (outcome === "uncertain-stale" && applies.length === 2) { await route.fulfill({ status: 409, json: { trace_id: "stale", error: { code: "stale", message: "Reviewed plan changed." } } }); return; }
          if (outcome === "readback" && applies.length === 1) refuseReadback = true;
          if (environment && !profile.lanes.some(lane => lane.instance === "testing")) profile.lanes.push({ context: body.context, instance: body.instance, base_url: body.base_url });
          if (!environment) profile.expected_config.managed_secret_bindings = body.managed_secret_bindings;
          if (!environment && outcome === "declaration-uncertain" && applies.length === 2) { await route.abort("failed"); return; }
          if (outcome === "uncertain" && applies.length === 1) { await route.abort("failed"); return; }
        }
        await route.fulfill({ status: 202, json: { status: "accepted", trace_id: "setup", records: {}, result: { status: "ok", mode: body.mode, changed: true, product: body.product,
          ...(environment ? { context: body.context, instance: body.instance, base_url: body.base_url, plan_sha256: "a".repeat(64), preserved_lane_instances: ["prod"], applied: body.mode === "apply" }
            : { managed_secret_bindings: { added: body.managed_secret_bindings, unchanged: [] } }) } } }); return;
      }
      if (path.endsWith("/product-profiles/atlas-commerce") && refuseReadback) { refuseReadback = false; await route.fulfill({ status: 401, json: { trace_id: "readback", error: { code: "authentication_required", message: "Read-back session expired." } } }); return; }
      const response = path.endsWith("/auth/session") ? { csrf_token: "csrf", identity: data.identity }
        : path.endsWith("/product-profiles/atlas-commerce") ? { profile }
        : path.endsWith("/config-status") ? { config_status: data.config }
        : path.endsWith("/environments/testing") ? { environment: data.environment }
        : path.endsWith("/owner-secret-inputs") ? { fields: [] }
        : path.endsWith("/products") ? { products: data.products }
        : { product: data.products[0] };
      await route.fulfill({ json: { status: "ok", trace_id: "read", ...response } });
    });
    await page.goto("/ui/products/atlas-commerce");
    let panel = page.getByRole("region", { name: "Set up a non-production environment" });
    await panel.getByText("Add environment", { exact: true }).click();
    await panel.getByLabel("Prepared target HTTPS origin").fill("https://testing.example.com");
    await panel.getByLabel("Reason", { exact: true }).fill("Isolated testing");
    await panel.getByRole("button", { name: "Run dry-run", exact: true }).click();
    await expect(panel.getByText("Exact service diff")).toBeVisible();
    await panel.getByLabel("Reason", { exact: true }).fill("Edited draft");
    await expect(panel.getByRole("button", { name: "Apply reviewed change" })).toHaveCount(0);
    await panel.getByRole("button", { name: "Run dry-run", exact: true }).click();
    await panel.getByRole("checkbox").check();
    await panel.getByRole("button", { name: "Apply reviewed change" }).click();
    if (["uncertain", "readback", "uncertain-stale"].includes(outcome)) {
      await expect(panel.getByRole("button", { name: "Retry original apply" })).toBeVisible();
      await page.reload();
      panel = page.getByRole("region", { name: "Set up a non-production environment" });
      // Recovery must be visible after reload, without expanding a disclosure.
      await expect(panel.getByRole("button", { name: "Retry original apply" })).toBeVisible();
      await panel.getByRole("button", { name: "Retry original apply" }).click();
      await expect.poll(() => applies.length).toBe(2);
      expect(applies[1]).toEqual(applies[0]);
      if (outcome === "uncertain-stale") {
        await expect(panel.getByRole("button", { name: "Retry original apply" })).toHaveCount(0);
        await panel.getByLabel("Prepared target HTTPS origin").fill("https://testing.example.com");
        await panel.getByLabel("Reason", { exact: true }).fill("Fresh plan after stale refusal");
        await panel.getByRole("button", { name: "Run dry-run", exact: true }).click();
        await panel.getByRole("checkbox").check();
        await panel.getByRole("button", { name: "Apply reviewed change" }).click();
      }
    }
    await expect(panel.getByRole("link", { name: "Open testing managed secrets" })).toBeVisible();
    const declarations = panel.locator("details").filter({ has: page.locator("summary", { hasText: "Declare managed-secret fields" }) });
    await expect(declarations.locator("select")).toBeVisible();
    await declarations.getByRole("combobox", { name: "Environment", exact: true }).selectOption("testing");
    await declarations.getByLabel("Binding key 1").fill("BOT_TOKEN");
    await declarations.getByRole("button", { name: "Add another declaration" }).click();
    await declarations.getByLabel("Binding key 2").fill("BRIDGE_TOKEN");
    await declarations.getByLabel("Reason", { exact: true }).fill("Declare isolated fields");
    await declarations.getByRole("button", { name: "Run dry-run", exact: true }).click();
    await expect(declarations.getByText("Exact service diff")).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await panel.screenshot({ path: testInfo.outputPath("setup-declaration-review.png") });
    await declarations.getByRole("checkbox").check();
    await declarations.getByRole("button", { name: "Add another declaration" }).click();
    await expect(declarations.getByRole("button", { name: "Apply reviewed change" })).toHaveCount(0);
    await declarations.getByRole("button", { name: "Remove declaration 3" }).click();
    await declarations.getByRole("button", { name: "Run dry-run", exact: true }).click();
    await declarations.getByRole("checkbox").check();
    await declarations.getByRole("button", { name: "Apply reviewed change" }).click();
    await expect.poll(() => applies.at(-1)?.body.mode === "apply" && Boolean(applies.at(-1)?.body.managed_secret_bindings)).toBe(true);
    const body = applies.at(-1)!.body;
    expect(body.managed_secret_bindings).toEqual([
      { binding_key: "BOT_TOKEN", integration: "runtime_environment", context: "atlas-commerce", instance: "testing", owner_input: null },
      { binding_key: "BRIDGE_TOKEN", integration: "runtime_environment", context: "atlas-commerce", instance: "testing", owner_input: null },
    ]);
    if (outcome === "declaration-uncertain") {
      await expect(panel.getByRole("button", { name: "Retry original apply" })).toBeVisible();
      await page.reload();
      panel = page.getByRole("region", { name: "Set up a non-production environment" });
      await expect(panel.getByRole("button", { name: "Retry original apply" })).toBeVisible();
      await panel.getByRole("button", { name: "Retry original apply" }).click();
      await expect.poll(() => applies.length).toBe(3);
      expect(applies[2]).toEqual(applies[1]);
    }
    await panel.getByRole("link", { name: "Open testing managed secrets" }).click();
    await expect(page).toHaveURL(/environments\/testing\/managed-secrets$/);
    await expect(page.getByRole("heading", { name: "Plan managed-secret changes", exact: true })).toBeVisible();
    expect(profile.release_on_acceptance).toBe("held");
  });
}

test("read-only users receive no environment setup controls", async ({ page }) => {
  await page.goto("/ui/products/atlas-commerce?fixture=products");
  const data = await page.evaluate(async () => {
    const f = await import("/ui/src/dev-fixtures.ts");
    return { products: f.productsForFixture("products"), identity: { ...f.fixtureIdentity, role: "read_only" } };
  });
  const writes: string[] = [];
  page.on("request", request => { if (request.method() === "POST") writes.push(request.url()); });
  await page.route("**/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const response = path.endsWith("/auth/session") ? { csrf_token: "csrf", identity: data.identity }
      : path.endsWith("/products") ? { products: data.products }
      : path.endsWith("/product-profiles/atlas-commerce") ? { profile: { owner: {}, image: {}, production_use: "live", release_on_acceptance: "held" } }
      : { product: data.products[0] };
    await route.fulfill({ json: { status: "ok", trace_id: "read", ...response } });
  });
  await page.goto("/ui/products/atlas-commerce");
  await expect(page.getByRole("heading", { name: "Atlas Commerce", exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Set up a non-production environment" })).toHaveCount(0);
  expect(writes).toEqual([]);
});
