import { waitForState, expect, test, type Page } from "./state";

type SetupOutcome = "success" | "uncertain" | "readback" | "uncertain-stale" | "declaration-uncertain" | "declaration-uncertain-stale" | "private" | "uncertain-profile";

async function setupScenario(page: Page, outcome: SetupOutcome, declarationsOnly = false) {
  const consoleErrors: string[] = [];
  page.on("console", message => { if (message.type() === "error") consoleErrors.push(message.text()); });
  await page.goto("/ui/products/atlas-commerce?fixture=products");
  const data = await page.evaluate(async () => {
    const f = await import("/ui/src/dev-fixtures.ts");
    return { products: f.productsForFixture("products"), identity: f.fixtureIdentity,
      environment: f.environmentForFixture("products", "atlas-commerce", "testing"),
      config: f.configStatusForFixture("products", "atlas-commerce", "testing") };
  });
  const profile = { product: "atlas-commerce", owner: { github_login: "example-owner", github_id: "9001" }, public_website: outcome === "private" ? "none" : "required", image: { repository: "ghcr.io/example/atlas-commerce" }, production_use: "live", release_on_acceptance: "held",
    lanes: [{ context: "atlas-commerce", instance: "prod", base_url: "https://example.com" }],
    expected_config: { runtime_environment_keys: [], managed_secret_bindings: [] as unknown[] } };
  if (declarationsOnly) profile.lanes.push({ context: "atlas-commerce", instance: "testing", base_url: "https://testing.example.com" });
  let refuseReadback = false;
  const state = { incompleteProfile: false };
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
        if (!environment && outcome === "declaration-uncertain-stale" && applies.length === 1) { await route.abort("failed"); return; }
        if (!environment && outcome === "declaration-uncertain-stale" && applies.length === 2) { await route.fulfill({ status: 409, json: { trace_id: "stale-declaration", error: { code: "stale", message: "Product profile changed while declaring configuration. Review again." } } }); return; }
        if (outcome === "readback" && applies.length === 1) refuseReadback = true;
        if (environment && !profile.lanes.some(lane => lane.instance === "testing")) profile.lanes.push({ context: body.context, instance: body.instance, base_url: body.base_url });
        if (!environment) profile.expected_config.managed_secret_bindings = body.managed_secret_bindings;
        if (!environment && outcome === "declaration-uncertain" && applies.length === 1) { await route.abort("failed"); return; }
        if (["uncertain", "uncertain-profile"].includes(outcome) && applies.length === 1) { await route.abort("failed"); return; }
      }
      await route.fulfill({ status: 202, json: { status: "accepted", trace_id: "setup", records: {}, result: { status: "ok", mode: body.mode, changed: true, product: body.product,
        ...(environment ? { context: body.context, instance: body.instance, base_url: body.base_url, plan_sha256: "a".repeat(64), preserved_lane_instances: ["prod"], applied: body.mode === "apply" }
          : { managed_secret_bindings: { added: body.managed_secret_bindings, unchanged: [] } }) } } }); return;
    }
    if (path.endsWith("/product-profiles/atlas-commerce") && refuseReadback) { refuseReadback = false; await route.fulfill({ status: 401, json: { trace_id: "readback", error: { code: "authentication_required", message: "Read-back session expired." } } }); return; }
    const response = path.endsWith("/auth/session") ? { csrf_token: "csrf", identity: data.identity }
      : path.endsWith("/product-profiles/atlas-commerce") ? { profile: state.incompleteProfile ? { ...profile, lanes: undefined } : profile }
      : path.endsWith("/config-status") ? { config_status: data.config }
      : path.endsWith("/environments/testing") ? { environment: data.environment }
      : path.endsWith("/owner-secret-inputs") ? { fields: [] }
      : path.endsWith("/products") ? { products: data.products }
      : { product: data.products[0] };
    await route.fulfill({ json: { status: "ok", trace_id: "read", ...response } });
  });
  await page.goto("/ui/products/atlas-commerce");
  const panel = page.getByRole("region", { name: "Set up a non-production environment" });
  return { panel, applies, consoleErrors, state };
}

for (const outcome of ["success", "uncertain", "readback", "uncertain-stale", "private", "uncertain-profile"] as const) {
  test(`signed-in environment setup: ${outcome}, exact diff and secret navigation`, async ({ page }) => {
    const scenario = await setupScenario(page, outcome);
    const { applies, consoleErrors, state } = scenario;
    let { panel } = scenario;
    await panel.getByText("Add environment", { exact: true }).click();
    if (outcome === "private") await expect(panel.getByLabel("Prepared target HTTPS origin")).toHaveCount(0);
    else await panel.getByLabel("Prepared target HTTPS origin").fill("https://testing.example.com");
    await panel.getByLabel("Reason", { exact: true }).fill("Isolated testing");
    if (outcome === "success") {
      const summary = panel.getByText("Add environment", { exact: true });
      await summary.press("Enter");
      await expect(panel.getByLabel("Reason", { exact: true })).toBeHidden();
      await summary.press("Space");
      await expect(panel.getByLabel("Reason", { exact: true })).toBeVisible();
    }
    await panel.getByRole("button", { name: "Run dry-run", exact: true }).click();
    await expect(panel.getByText("Exact service diff")).toBeVisible();
    await panel.getByLabel("Reason", { exact: true }).fill("Edited draft");
    await expect(panel.getByRole("button", { name: "Apply reviewed change" })).toHaveCount(0);
    await panel.getByRole("button", { name: "Run dry-run", exact: true }).click();
    await panel.getByRole("checkbox").check();
    await panel.getByRole("button", { name: "Apply reviewed change" }).click();
    if (outcome === "readback") {
      const signIn = page.getByRole("link", { name: "Sign in with GitHub" });
      await expect(signIn).toBeVisible();
      await expect(panel).toHaveCount(0);
      expect(applies).toHaveLength(1);
      await page.route("**/auth/github/login?*", route => route.fulfill({
        status: 302,
        headers: { location: new URL(route.request().url()).searchParams.get("return_to")! },
      }));
      await signIn.click();
      panel = page.getByRole("region", { name: "Set up a non-production environment" });
      await expect(panel).toBeVisible();
      expect(applies).toHaveLength(1);
    }
    if (["uncertain", "uncertain-profile", "readback", "uncertain-stale"].includes(outcome)) {
      await expect(panel.getByRole("button", { name: "Retry original apply" })).toBeVisible();
      if (outcome === "uncertain-profile") state.incompleteProfile = true;
      await page.reload();
      panel = page.getByRole("region", { name: "Set up a non-production environment" });
      if (outcome === "uncertain-profile") {
        await expect(panel.getByRole("status", { name: "Pending setup operation" })).toBeVisible();
        await expect(panel.getByRole("button", { name: "Retry original apply" })).toHaveCount(0);
        state.incompleteProfile = false;
        await panel.getByRole("button", { name: "Retry setup read" }).click();
      }
      await expect(panel.getByRole("button", { name: "Retry original apply" })).toBeVisible();
      await panel.getByRole("button", { name: "Retry original apply" }).click();
      await waitForState(() => applies.length).toBe(2);
      expect(applies[1]).toEqual(applies[0]);
      if (outcome === "uncertain-stale") {
        await expect(panel.getByRole("button", { name: "Retry original apply" })).toHaveCount(0);
        await panel.getByLabel("Prepared target HTTPS origin").fill("https://testing.example.com");
        await panel.getByLabel("Reason", { exact: true }).fill("Fresh plan after stale refusal");
        await panel.getByRole("button", { name: "Run dry-run", exact: true }).click();
        await panel.getByRole("checkbox").check();
        await panel.getByRole("button", { name: "Apply reviewed change" }).click();
        await waitForState(() => applies.length).toBe(3);
        expect(applies[2].key).not.toBe(applies[0].key);
      }
    }
    await expect(panel.getByRole("link", { name: "Open testing managed secrets" })).toBeVisible();
    const declarations = panel.locator("details").filter({ has: page.locator("summary", { hasText: "Declare managed-secret fields" }) });
    await expect(declarations.locator("select")).toBeVisible();
    if (outcome === "private") expect(applies[0].body.base_url).toBe("");
    await panel.getByRole("link", { name: "Open testing managed secrets" }).click();
    await expect(page).toHaveURL(/environments\/testing\/managed-secrets$/);
    await expect(page.locator('input[type="password"]').first()).toBeVisible();
    if (["success", "private"].includes(outcome)) expect(consoleErrors).toEqual([]);
  });
}

for (const outcome of ["success", "declaration-uncertain", "declaration-uncertain-stale"] as const) {
  test(`signed-in declarations: ${outcome}, exact diff, apply and secret navigation`, async ({ page }, testInfo) => {
    const scenario = await setupScenario(page, outcome, true);
    const { applies, consoleErrors } = scenario;
    let { panel } = scenario;
    await panel.getByText("Declare managed-secret fields", { exact: true }).click();
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
    await waitForState(() => applies.at(-1)?.body.mode === "apply" && Boolean(applies.at(-1)?.body.managed_secret_bindings)).toBe(true);
    const body = applies.at(-1)!.body;
    expect(body.managed_secret_bindings).toEqual([
      { binding_key: "BOT_TOKEN", integration: "runtime_environment", context: "atlas-commerce", instance: "testing", owner_input: null },
      { binding_key: "BRIDGE_TOKEN", integration: "runtime_environment", context: "atlas-commerce", instance: "testing", owner_input: null },
    ]);
    if (["declaration-uncertain", "declaration-uncertain-stale"].includes(outcome)) {
      await expect(panel.getByRole("button", { name: "Retry original apply" })).toBeVisible();
      await page.reload();
      panel = page.getByRole("region", { name: "Set up a non-production environment" });
      await expect(panel.getByRole("button", { name: "Retry original apply" })).toBeVisible();
      await panel.getByRole("button", { name: "Retry original apply" }).click();
      await waitForState(() => applies.length).toBe(2);
      expect(applies[1]).toEqual(applies[0]);
      if (outcome === "declaration-uncertain-stale") {
        await expect(panel.getByRole("button", { name: "Retry original apply" })).toHaveCount(0);
        const recoveredDeclarations = panel.locator("details").filter({ has: page.locator("summary", { hasText: "Declare managed-secret fields" }) });
        await recoveredDeclarations.getByRole("combobox", { name: "Environment", exact: true }).selectOption("testing");
        await recoveredDeclarations.getByLabel("Binding key 1").fill("BOT_TOKEN");
        await recoveredDeclarations.getByLabel("Reason", { exact: true }).fill("Fresh declaration after stale refusal");
        await recoveredDeclarations.getByRole("button", { name: "Run dry-run", exact: true }).click();
        await recoveredDeclarations.getByRole("checkbox").check();
        await recoveredDeclarations.getByRole("button", { name: "Apply reviewed change" }).click();
        await waitForState(() => applies.length).toBe(3);
        expect(applies[2].key).not.toBe(applies[0].key);
      }
    }
    await panel.getByRole("link", { name: "Open testing managed secrets" }).click();
    await expect(page).toHaveURL(/environments\/testing\/managed-secrets$/);
    await expect(page.locator('input[type="password"]').first()).toBeVisible();
    if (outcome === "success") expect(consoleErrors).toEqual([]);
  });
}

test("setup summaries close disclosures revealed by the browser", async ({ page }) => {
  const { panel } = await setupScenario(page, "success", true);
  for (const name of ["Add environment", "Declare managed-secret fields"]) {
    const details = panel.locator("details").filter({ has: page.locator("summary", { hasText: name }) });
    await details.evaluate(element => { (element as HTMLDetailsElement).open = true; });
    await expect(details).toHaveAttribute("open", "");
    await details.locator("summary").first().click();
    await expect(details).not.toHaveAttribute("open", "");
  }
});

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


test("incomplete setup metadata leaves existing product controls available and sends no setup writes", async ({ page }) => {
  await page.goto("/ui/products/atlas-commerce?fixture=products");
  const data = await page.evaluate(async () => {
    const fixtures = await import("/ui/src/dev-fixtures.ts");
    return { products: fixtures.productsForFixture("products"), identity: fixtures.fixtureIdentity };
  });
  const writes: string[] = [];
  const errors: string[] = [];
  let complete = false;
  page.on("request", request => { if (request.method() === "POST") writes.push(request.url()); });
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const response = path.endsWith("/auth/session") ? { csrf_token: "csrf", identity: data.identity }
      : path.endsWith("/products") ? { products: data.products }
      : path.endsWith("/product-profiles/atlas-commerce") ? { profile: {
        product: "atlas-commerce", expected_config: { managed_secret_bindings: [] },
        ...(complete ? { lanes: [{ context: "atlas-commerce", instance: "prod" }] } : {}),
        repository: "example/atlas-commerce", owner: { github_login: "example-owner", github_id: "9001" },
        image: { repository: "ghcr.io/example/atlas-commerce" }, production_use: "unknown",
      } }
      : { product: data.products[0] };
    await route.fulfill({ json: { status: "ok", trace_id: "partial-profile", ...response } });
  });
  await page.goto("/ui/products/atlas-commerce");
  const setup = page.getByRole("region", { name: "Set up a non-production environment" });
  await expect(setup.getByRole("alert")).toBeVisible();
  await expect(setup.getByRole("button", { name: "Run dry-run", exact: true })).toHaveCount(0);
  await page.getByLabel("GitHub login", { exact: true }).fill("another-client");
  complete = true;
  await setup.getByRole("button", { name: "Retry setup read" }).click();
  await expect(setup.getByText("Add environment", { exact: true })).toBeVisible();
  await expect(setup.getByRole("alert")).toHaveCount(0);
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});

test("a declaration review after a lost dry-run response reads current metadata", async ({ page }) => {
  await page.goto("/ui/products/atlas-commerce?fixture=products");
  const data = await page.evaluate(async () => {
    const fixtures = await import("/ui/src/dev-fixtures.ts");
    return { products: fixtures.productsForFixture("products"), identity: fixtures.fixtureIdentity };
  });
  const binding = { binding_key: "BOT_TOKEN", integration: "runtime_environment", context: "atlas-commerce", instance: "testing" };
  const profile = { product: "atlas-commerce", owner: { github_login: "example-owner", github_id: "9001" }, image: { repository: "ghcr.io/example/atlas-commerce" }, production_use: "unknown", public_website: "none", lanes: [{ context: binding.context, instance: binding.instance }], expected_config: { managed_secret_bindings: [] as unknown[] } };
  const receipts = new Map<string, unknown>();
  let loseResponse = true;
  let requests = 0;
  await page.route("**/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON();
      expect(body.mode).toBe("dry-run");
      const key = route.request().headers()["idempotency-key"];
      if (!receipts.has(key)) receipts.set(key, { status: "accepted", trace_id: "review", result: { status: "ok", mode: body.mode, product: body.product, changed: !profile.expected_config.managed_secret_bindings.length, managed_secret_bindings: { added: profile.expected_config.managed_secret_bindings.length ? [] : [binding], unchanged: profile.expected_config.managed_secret_bindings.length ? [binding] : [] } } });
      requests++;
      if (loseResponse) { loseResponse = false; await route.abort("failed"); return; }
      await route.fulfill({ status: 202, json: receipts.get(key) });
      return;
    }
    const response = path.endsWith("/auth/session") ? { csrf_token: "csrf", identity: data.identity }
      : path.endsWith("/products") ? { products: data.products }
      : path.endsWith("/product-profiles/atlas-commerce") ? { profile }
      : { product: data.products[0] };
    await route.fulfill({ json: { status: "ok", trace_id: "read", ...response } });
  });
  await page.goto("/ui/products/atlas-commerce");
  const panel = page.getByRole("region", { name: "Set up a non-production environment" });
  await panel.getByText("Declare managed-secret fields", { exact: true }).click();
  const declarations = panel.locator("details").filter({ has: page.locator("summary", { hasText: "Declare managed-secret fields" }) });
  await declarations.getByRole("combobox", { name: "Environment", exact: true }).selectOption("testing");
  await declarations.getByLabel("Binding key 1").fill(binding.binding_key);
  await declarations.getByLabel("Reason", { exact: true }).fill("Current testing declarations");
  const review = declarations.getByRole("button", { name: "Run dry-run", exact: true });
  await review.click();
  await waitForState(() => requests).toBe(1);
  await expect(review).toBeEnabled();
  // Another administrator declared the field while this response was lost.
  profile.expected_config.managed_secret_bindings = [binding];
  await review.click();
  await expect(declarations.getByRole("row").filter({ hasText: binding.binding_key })).toContainText("unchanged");
});
