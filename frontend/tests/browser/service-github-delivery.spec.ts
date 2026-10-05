import { expect, test, type Page } from "@playwright/test";

const metadata = {
  status: "ok", trace_id: "metadata", app_id: "", integration: "", advisory_app_id: "77",
  existing_keys: [{ integration: "existing-delivery", binding_id: "binding-key", secret_id: "key", context: "launchplane" }],
  obsolete_tokens: [
    { secret_id: "token-global", scope: "global", context: "", status: "configured", binding_ids: ["binding-global"] },
    { secret_id: "token-context", scope: "context", context: "launchplane", status: "configured", binding_ids: ["binding-context"] },
  ],
};

async function setup(page: Page, outcome = "ok") {
  const current = structuredClone(metadata);
  const requests: { path: string; body: Record<string, any>; key: string }[] = [];
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error" && !message.text().includes("Failed to load resource")) errors.push(message.text()); });
  await page.route("**/v1/auth/session", route => route.fulfill({ json: {
    status: "ok", trace_id: "session", csrf_token: "csrf-fixture",
    identity: { provider: "github", login: "admin-fixture", github_id: 42, name: "Admin", email: "fixture@example.invalid", organizations: [], teams: [], role: "admin" },
  } }));
  await page.route("**/v1/service/github-delivery", route => route.fulfill({ json: current }));
  await page.route("**/v1/service/github-delivery/configuration", async route => {
    const body = route.request().postDataJSON();
    expect(route.request().headers()["x-csrf-token"]).toBe("csrf-fixture");
    if (body.mode === "apply") {
      requests.push({ path: "configuration", body, key: route.request().headers()["idempotency-key"] });
      if (outcome === "stale") { await route.fulfill({ status: 409, json: { trace_id: "stale", error: { code: "stale", message: "Configuration changed; review a new dry-run." } } }); return; }
      if (outcome !== "mismatch") { current.app_id = String(body.app_id); current.integration = body.integration; }
      if (outcome === "uncertain" && requests.length === 1) { await route.abort("failed"); return; }
    }
    await route.fulfill({ json: { status: "ok", trace_id: "selection", actor: "github:42", ...body, plan_digest: "a".repeat(64), runtime_environment: {} } });
  });
  await page.route("**/v1/service/github-delivery/token-retirement", async route => {
    const body = route.request().postDataJSON();
    expect(route.request().headers()["x-csrf-token"]).toBe("csrf-fixture");
    const tokens = current.obsolete_tokens.filter(token => body.secret_ids.includes(token.secret_id));
    if (body.mode === "apply") {
      requests.push({ path: "retirement", body, key: route.request().headers()["idempotency-key"] });
      expect(body.director_confirmed).toBe(true);
      for (const token of tokens) token.status = "disabled";
      if (outcome === "retirement-uncertain" && requests.filter(request => request.path === "retirement").length === 1) { await route.abort("failed"); return; }
    }
    await route.fulfill({ json: { status: "ok", trace_id: "retirement", mode: body.mode, actor: "github:42", reason: body.reason, app_id: current.app_id, advisory_app_id: current.advisory_app_id, tokens, plan_digest: "b".repeat(64) } });
  });
  return { current, requests, errors };
}

async function selectDelivery(page: Page) {
  const panel = page.getByRole("region", { name: "Select Delivery App", exact: true });
  await panel.getByLabel("Delivery App id", { exact: true }).fill("76");
  await panel.getByLabel("Existing managed key").selectOption("existing-delivery");
  await panel.getByLabel("Selection reason").fill("Select the existing Delivery App key.");
  await panel.getByRole("button", { name: "Dry run", exact: true }).click();
  await panel.getByLabel("I am the Director approving this App id and existing key selection.").check();
  return panel;
}

async function reviewRetirement(page: Page) {
  const panel = page.getByRole("region", { name: "Retire obsolete service tokens", exact: true });
  await panel.getByLabel("token-global", { exact: false }).check();
  await panel.getByLabel("Advisory check receipt URL").fill("https://github.com/example/site/actions/runs/11/job/12");
  await panel.getByLabel("Delivery comment receipt URL").fill("https://github.com/example/site/pull/13#issuecomment-14");
  await panel.getByLabel("Delivery release issue receipt URL").fill("https://github.com/example/site/issues/15");
  await panel.getByLabel("Remaining-consumer check evidence").fill("Director checked remaining consumers after the migration.");
  await panel.getByLabel("Retirement reason").fill("Retire the confirmed obsolete global service token.");
  await panel.getByRole("button", { name: "Dry run", exact: true }).click();
  return panel;
}

test("Director selection and individually confirmed retirement read back at desktop and narrow widths", async ({ page }, testInfo) => {
  const { current, requests, errors } = await setup(page);
  await page.goto("/ui/engineering/github-delivery");
  await expect(page.getByRole("heading", { name: "GitHub delivery", level: 1 })).toBeFocused();
  const retire = page.getByRole("region", { name: "Retire obsolete service tokens", exact: true });
  await expect(retire.getByRole("button", { name: "Dry run", exact: true })).toBeDisabled();
  const selection = await selectDelivery(page);
  await selection.getByLabel("Selection reason").fill("Updated reason invalidates the prior review.");
  await expect(selection.getByRole("button", { name: "Apply", exact: true })).toBeDisabled();
  await selection.getByRole("button", { name: "Dry run", exact: true }).click();
  await expect(selection.getByRole("button", { name: "Apply", exact: true })).toBeDisabled();
  await selection.getByLabel("I am the Director approving this App id and existing key selection.").check();
  await selection.getByRole("button", { name: "Apply", exact: true }).click();
  await expect(selection.getByRole("status")).toContainText("Applied and read back.");
  await expect(page.getByRole("region", { name: "Current service selection" })).toContainText("76");
  await page.getByRole("heading", { name: "GitHub delivery", level: 1 }).click();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: testInfo.outputPath("delivery-selection-viewport.png") });
  await page.screenshot({ path: testInfo.outputPath("delivery-selection-applied.png"), fullPage: true });
  const retirement = await reviewRetirement(page);
  await expect(retirement.getByRole("button", { name: "Apply", exact: true })).toBeDisabled();
  await retirement.getByLabel(/I am the Director\. I verified Advisory App/).check();
  await retirement.getByLabel("Retirement reason").fill("Updated retirement reason.");
  await expect(retirement.getByRole("button", { name: "Apply", exact: true })).toBeDisabled();
  await retirement.getByRole("button", { name: "Dry run", exact: true }).click();
  await retirement.getByLabel(/I am the Director\. I verified Advisory App/).check();
  await page.getByRole("heading", { name: "GitHub delivery", level: 1 }).click();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: testInfo.outputPath("service-token-retirement-reviewed.png"), fullPage: true });
  await retirement.getByRole("button", { name: "Apply", exact: true }).click();
  await expect(retirement.getByRole("status")).toContainText("Applied and read back.");
  expect(current.obsolete_tokens[0].status).toBe("disabled");
  expect(current.obsolete_tokens[1].status).toBe("configured");
  expect(requests.map(request => request.path)).toEqual(["configuration", "retirement"]);
  expect(requests.every(request => Boolean(request.key))).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(await page.locator("[id]").evaluateAll(elements => new Set(elements.map(element => element.id)).size === elements.length)).toBe(true);
  expect(errors).toEqual([]);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: testInfo.outputPath("service-token-retirement-applied.png"), fullPage: true });
});

for (const outcome of ["uncertain", "stale", "mismatch"] as const) {
  test(`selection handles ${outcome} apply`, async ({ page }) => {
    const { requests } = await setup(page, outcome);
    await page.goto("/ui/engineering/github-delivery");
    let panel = await selectDelivery(page);
    await panel.getByRole("button", { name: "Apply", exact: true }).click();
    if (outcome === "uncertain") {
      await expect(panel.getByRole("button", { name: "Retry Apply" })).toBeEnabled();
      await page.reload();
      panel = page.getByRole("region", { name: "Select Delivery App", exact: true });
      await expect(panel.getByLabel("Delivery App id", { exact: true })).toBeDisabled();
      await panel.getByRole("button", { name: "Retry Apply" }).click();
      await expect(panel.getByRole("status")).toContainText("Applied and read back.");
      expect(requests[1]).toEqual(requests[0]);
    } else if (outcome === "stale") {
      await expect(panel.getByRole("status")).toContainText("review a new dry-run");
      await expect(panel.getByRole("button", { name: "Dry run", exact: true })).toBeEnabled();
    } else {
      await expect(panel.getByRole("alert")).toContainText("current metadata does not match");
      await expect(panel.getByRole("button", { name: "Apply", exact: true })).toBeDisabled();
    }
  });
}

test("retirement retry after reload keeps the exact selected record and operation key", async ({ page }) => {
  const { current, requests } = await setup(page, "retirement-uncertain");
  current.app_id = "76"; current.integration = "existing-delivery";
  await page.goto("/ui/engineering/github-delivery");
  let panel = await reviewRetirement(page);
  await panel.getByLabel(/I am the Director\. I verified Advisory App/).check();
  await panel.getByRole("button", { name: "Apply", exact: true }).click();
  await expect(panel.getByRole("button", { name: "Retry Apply" })).toBeEnabled();
  await page.reload();
  panel = page.getByRole("region", { name: "Retire obsolete service tokens", exact: true });
  await panel.getByRole("button", { name: "Retry Apply" }).click();
  await expect(panel.getByRole("status")).toContainText("Applied and read back.");
  expect(requests[1]).toEqual(requests[0]);
  expect(current.obsolete_tokens[1].status).toBe("configured");
});

test("unavailable metadata and absent key leave controls unavailable", async ({ page }) => {
  const { current } = await setup(page);
  current.existing_keys = [];
  current.obsolete_tokens = [];
  await page.goto("/ui/engineering/github-delivery");
  await expect(page.getByText("No eligible configured service-context private_key binding.", { exact: false })).toBeVisible();
  await expect(page.getByText("No eligible service-token records found.")).toBeVisible();
  await page.route("**/v1/service/github-delivery", route => route.fulfill({ status: 403, json: { trace_id: "denied", error: { code: "authorization_denied", message: "Missing standing service read authority." } } }));
  await page.reload();
  await expect(page.getByText("Missing standing service read authority.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Apply", exact: true })).toHaveCount(0);
});
