import { expect, setClockTime, test, waitForState, type Page } from "./state";
import type { ProviderHoldAttentionPage, ProviderHoldAttentionItem, ProductSiteOverview } from "../../src/generated/openapi.ts";

const attentionPath = "**/v1/operations/provider-hold-attention**";

async function fixture(page: Page) {
  await page.goto("/ui/products?fixture=products");
  const result = await page.evaluate(async () => {
    const modulePath = "/ui/src/dev-fixtures.ts";
    const module = await import(modulePath);
    const product = module.productsForFixture("products")[0];
    const lane = product.environments[0];
    const deadline = new Date(Date.parse(lane.provenance.refreshed_at) + 120_000).toISOString();
    lane.warnings = []; lane.topology.warnings = [];
    lane.topology.desired.public_website = "none";
    lane.topology.observed.placement.runtime_identity_status = "match";
    lane.topology.observed.placement.trust_state = "verified";
    lane.topology.observed.tls_domains = [];
    lane.provenance.freshness_status = "verified";
    lane.provenance.stale_after = deadline;
    lane.health_monitoring.open_incidents = [];
    lane.health_monitoring.checks = [lane.health_monitoring.checks[0]];
    for (const check of lane.health_monitoring.checks) {
      check.incident_status = "none"; check.incident_id = ""; check.status = "pass";
      check.kind = "private_http";
      check.trust_state = "verified"; check.probe_effective = true; check.incident_eligible = true;
      check.provenance.stale_after = deadline;
    }
    lane.topology.observed.placement.provenance.stale_after = deadline;
    product.environments = [lane, { ...structuredClone(lane), environment: "prod", instance: "prod" }];
    return { product, identity: module.fixtureIdentity };
  });
  await setClockTime(page, Date.parse(result.product.environments[0].provenance.refreshed_at));
  return result;
}

function hold(known = false, _reference = "retained-operation"): ProviderHoldAttentionItem {
  return { operation_reference: known ? `provider-operation:v1:mutation-reservation-${"a".repeat(64)}:11111111-1111-4111-8111-111111111111` : null, original_scope: known ? { product: "Retired product", context: "removed-context", instance: "prod" } : null,
    attribution: known ? "retained" : "unavailable", state: "running", phase: "not_recorded",
    created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z", lease_expires_at: "2026-10-01T00:01:00Z", effect_started_at: "" };
}
async function attention(page: Page, items: ProviderHoldAttentionItem[]): Promise<ProviderHoldAttentionPage> {
  const now = await page.evaluate(() => Date.now());
  items = items.map(item => ({ ...item, created_at: new Date(now - 60_000).toISOString(), updated_at: new Date(now).toISOString(), lease_expires_at: new Date(now + 60_000).toISOString() }));
  return { items, has_more: false, next_cursor: null, completeness: "single_page", observed_at: new Date(now).toISOString(), stale_after: new Date(now + 60_000).toISOString() };
}
async function inventory(page: Page, products: ProductSiteOverview[], identity: unknown) {
  await page.route("**/v1/**", route => {
    expect(route.request().method()).toBe("GET");
    return route.fulfill({ status: 403, json: { error: { code: "authorization_denied" } } });
  });
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", identity, csrf_token: "fixture" } }));
  await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", products } }));
}

for (const empty of [false, true]) {
  test(`retained unknown work remains visible with ${empty ? "empty inventory" : "healthy lanes"} @layout`, async ({ page }, testInfo) => {
    const { product, identity } = await fixture(page);
    await inventory(page, empty ? [] : [product], identity);
    const data = await attention(page, [hold()]);
    await page.route(attentionPath, route => route.fulfill({ json: { status: "ok", attention: { ...data, private_intent: "PRIVATE-SENTINEL", provider_target_key: "PRIVATE-TARGET" } } }));
    await page.goto("/ui/products");
    const panel = page.getByRole("region", { name: "Service operation attention" });
    await expect(panel).toContainText("Original ownership unknown");
    await expect(panel).toContainText("Running operation");
    await expect(panel).not.toContainText("stuck");
    if (empty) await expect(page.getByRole("heading", { name: "No products are owned by Launchplane yet" })).toBeVisible();
    else {
      await expect(page.getByRole("region", { name: "Active public ingress incidents" })).toContainText("unknown original ownership");
      await expect(page.locator(".product-directory")).toContainText(product.display_name);
      await expect(page.locator('.directory-lane[data-operational="verified"]')).toHaveCount(2);
    }
    await expect(page.locator("body")).not.toContainText("PRIVATE-");
    await expect(panel.getByRole("button", { name: /retry|release|clear|resume/i })).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: `../tmp/browser-smoke/provider-attention-${empty ? "empty" : "lanes"}-${testInfo.project.name}.png`, fullPage: true });
  });
}

test("bounded continuation remains partial on an empty last page and refresh resets", async ({ page }) => {
  const { product, identity } = await fixture(page);
  await inventory(page, [product], identity);
  const first = { ...await attention(page, [hold(true)]), has_more: true, next_cursor: "next-fixture", completeness: "partial" as const };
  const last = { ...await attention(page, []), completeness: "partial" as const };
  const queries: string[] = [];
  await page.route(attentionPath, route => { const query = new URL(route.request().url()).search; queries.push(query); return route.fulfill({ json: { status: "ok", attention: query.includes("cursor") ? last : first } }); });
  await page.goto("/ui/products");
  const panel = page.getByRole("region", { name: "Service operation attention" });
  await expect(panel).toContainText("Retired product");
  await expect(panel).toContainText("partial");
  await panel.getByRole("button", { name: "Next operation page" }).click();
  await expect(panel).toContainText("No operations returned on this continuation page");
  await expect(panel).not.toContainText("Retired product");
  await expect(panel).not.toContainText("No retained service operations");
  await panel.getByRole("button", { name: "Refresh operation attention" }).click();
  await expect(panel).toContainText("Retired product");
  expect(queries.some(query => query.includes("cursor=next-fixture"))).toBe(true);
  expect(queries.at(-1)).not.toContain("cursor");
});

test("one completed operation cannot hide another retained operation", async ({ page }) => {
  const { identity } = await fixture(page);
  await inventory(page, [], identity);
  let data = await attention(page, [hold(true, "first-operation"), hold(false, "second-operation")]);
  await page.route(attentionPath, route => route.fulfill({ json: { status: "ok", attention: data } }));
  await page.goto("/ui/products");
  const panel = page.getByRole("region", { name: "Service operation attention" });
  await expect(panel.locator("li")).toHaveCount(2);
  data = await attention(page, [hold(false, "second-operation")]);
  await page.getByRole("button", { name: "Refresh current evidence" }).click();
  await expect(panel.locator("li")).toHaveCount(1);
  await expect(panel).toContainText("Original ownership unknown");
});

test("loading stale transient failure and forbidden responses preserve honest evidence and clear sensitive rows", async ({ page }) => {
  const { product, identity } = await fixture(page);
  await inventory(page, [product], identity);
  let status = 200;
  const data = await attention(page, [hold(true)]);
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  let held = true;
  await page.route(attentionPath, async route => {
    if (held) await gate;
    await route.fulfill({ status, json: status === 200 ? { status: "ok", attention: data } : { status: "rejected", error: { code: status === 403 ? "authorization_denied" : "unavailable" } } });
  });
  await page.goto("/ui/products");
  const panel = page.getByRole("region", { name: "Service operation attention" });
  await expect(panel).toContainText("attention is loading");
  held = false;
  release();
  await expect(panel).toContainText("Retired product");
  status = 503;
  await page.clock.runFor(60_001);
  await expect(panel).toContainText("attention is unavailable");
  await expect(panel).toContainText("Last received evidence is stale");
  await expect(panel).toContainText("Retired product");
  status = 403;
  await panel.getByRole("button", { name: "Refresh operation attention" }).click();
  await expect(panel.locator("li")).toHaveCount(0);
  await expect(panel).toContainText("attention is unavailable");
  await expect(page.getByRole("region", { name: "Active public ingress incidents" })).toContainText("authorized lane incidents only");
  status = 200;
  await panel.getByRole("button", { name: "Refresh operation attention" }).click();
  await expect(panel).toContainText("Retired product");
});

test("authentication loss removes cached service evidence and a fresh session reads again", async ({ page }) => {
  const { identity } = await fixture(page);
  await inventory(page, [], identity);
  let expired = false;
  let data = await attention(page, [hold(true)]);
  await page.route(attentionPath, route => route.fulfill({ status: expired ? 401 : 200, json: expired ? { error: { code: "authentication_required" } } : { status: "ok", attention: data } }));
  await page.goto("/ui/products");
  await expect(page.getByRole("region", { name: "Service operation attention" })).toContainText("Retired product");
  expired = true;
  await page.getByRole("button", { name: "Refresh current evidence" }).click();
  await expect(page.getByRole("region", { name: "Service operation attention" })).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText("Retired product");
  expired = false;
  data = await attention(page, [hold(false)]);
  await page.reload();
  await expect(page.getByRole("region", { name: "Service operation attention" })).toContainText("Original ownership unknown");
  await expect(page.locator("body")).not.toContainText("Retired product");
});

test("demo and non-admin sessions never fetch service attention", async ({ page }) => {
  let calls = 0;
  await page.route(attentionPath, route => { calls++; return route.abort(); });
  const { identity } = await fixture(page);
  await expect(page.getByRole("region", { name: "Service operation attention" })).toContainText("Demo mode");
  expect(calls).toBe(0);
  await inventory(page, [], { ...identity, role: "read_only" });
  await page.goto("/ui/products");
  await expect(page.getByRole("region", { name: "Service operation attention" })).toContainText("unavailable for this session");
  expect(calls).toBe(0);
});


test("a cancelled continuation cannot replace the refreshed first page", async ({ page }) => {
  const { identity } = await fixture(page);
  await inventory(page, [], identity);
  const first = { ...await attention(page, [hold(true)]), has_more: true, next_cursor: "held-page", completeness: "partial" as const };
  let pending = false;
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route(attentionPath, async route => {
    if (new URL(route.request().url()).searchParams.has("cursor")) {
      pending = true;
      await gate;
      await route.fulfill({ json: { status: "ok", attention: { ...first, items: [], has_more: false, next_cursor: null } } }).catch(() => {});
    } else await route.fulfill({ json: { status: "ok", attention: first } });
  });
  await page.goto("/ui/products");
  const panel = page.getByRole("region", { name: "Service operation attention" });
  await expect(panel).toContainText("Retired product");
  await panel.getByRole("button", { name: "Next operation page" }).click();
  await waitForState(() => pending).toBe(true);
  await expect(panel).toHaveAttribute("aria-busy", "true");
  await page.getByRole("button", { name: "Refresh current evidence" }).click();
  await expect(panel).toHaveAttribute("aria-busy", "false");
  await expect(panel).toContainText("Retired product");
  release();
  await expect(panel).toContainText("Retired product");
  await expect(panel).not.toContainText("No operations returned on this continuation page");
});

test("explicit sign out removes service attention and stops its refresh", async ({ page }) => {
  const { identity } = await fixture(page);
  await inventory(page, [], identity);
  const data = await attention(page, [hold(true)]);
  let reads = 0;
  await page.route(attentionPath, route => { reads++; return route.fulfill({ json: { status: "ok", attention: data } }); });
  await page.route("**/auth/logout", route => route.fulfill({ json: { status: "ok" } }));
  await page.goto("/ui/products");
  await expect(page.getByRole("region", { name: "Service operation attention" })).toContainText("Retired product");
  await page.getByLabel("Open account menu").click();
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page.getByRole("region", { name: "Service operation attention" })).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText("Retired product");
  const before = reads;
  await page.clock.runFor(60_001);
  expect(reads).toBe(before);
});
