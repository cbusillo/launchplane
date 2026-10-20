import { waitForState, expect, test, type Page } from "./state";
import type { RuntimeSyncReview } from "../../src/generated/openapi.ts";
import { runtimeSyncStorageKey } from "../../src/runtime-sync-operation";

type Scenario = "saved" | "unchanged" | "odoo" | "non-admin" | "denied" | "apply-denied" | "target-busy" | "key-conflict" | "lane-unqualified" | "terminal-unverified" | "apply-unavailable" | "loading" | "missing-availability" | "config-error" |
  "uncertain" | "unknown-auth" | "unknown-config-error" | "unknown-unavailable" | "missing-request" | "provider-unknown" |
  "stale" | "uncertain-stale" | "receipt-scope" | "receipt-digest" | "receipt-target" | "receipt-fingerprint" |
  "malformed-plan" | "scope-plan" | "applied-plan" | "receipt-result" | "readback-error" | "readback-different" | "refresh" | "storage-unavailable";
type Call = { body: { mode: string; product: string; context: string; instance: string; browser_review: { reviewed_plan_sha256?: string } }; key: string };

async function fixture(page: Page, scenario: Scenario) {
  await page.goto("/ui/products?fixture=empty");
  const data = await page.evaluate(async () => {
    const f = await import("/ui/src/dev-fixtures.ts");
    return { identity: f.fixtureIdentity, products: f.productsForFixture("products"),
      environment: f.environmentForFixture("products", "atlas-commerce", "testing")!,
      config: f.configStatusForFixture("products", "atlas-commerce", "testing")! };
  });
  for (const product of data.products) product.environments = product.environments.filter((lane, index, lanes) =>
    lanes.findIndex(other => other.context === lane.context && other.environment === lane.environment) === index);
  const scope = { product: data.environment.product, context: data.environment.context, instance: data.environment.environment };
  data.config.runtime_sync = { plan: { available: true, reason: "" }, apply: { available: true, reason: "" } };
  if (scenario === "odoo" || scenario === "non-admin") data.config.runtime_sync = {
    plan: { available: false, reason: scenario === "odoo" ? "Runtime sync is not supported for this lane yet." : "A named administrator session is required." },
    apply: { available: false, reason: "Runtime sync apply is unavailable." },
  };
  if (scenario === "missing-availability") delete data.config.runtime_sync;
  const original: RuntimeSyncReview = { ...scope, operation: "runtime-sync", plan_sha256: "a".repeat(64),
    target_fingerprint: "opaque-fixture-fingerprint/" + "long-evidence-".repeat(12), target_id: "fixture-runtime-target", target_type: "application",
    target_name: "Recorded testing destination", missing_keys: ["PUBLIC_ORIGIN"], different_keys: ["WORKER_TOKEN_WITH_A_LONG_NAME_FOR_WRAPPING"],
    retired_keys_present: ["RETIRED_PROVIDER_KEY"], changed_keys: ["PUBLIC_ORIGIN", "WORKER_TOKEN_WITH_A_LONG_NAME_FOR_WRAPPING", "RETIRED_PROVIDER_KEY"],
    desired_key_count: 5, live_key_count: 5, unchanged_key_count: 3, applied: false };
  const calls: Call[] = [];
  let configReads = 0;
  let saved = scenario === "unchanged";
  let releasePlan: (() => Promise<void>) | null = null;
  let releaseConfig: (() => Promise<void>) | null = null;
  if (scenario === "apply-unavailable") data.config.runtime_sync!.apply = { available: false, reason: "Runtime sync apply is paused for this administrator." };
  const applies = () => calls.filter(call => call.body.mode === "apply");
  await page.route("**/v1/**", route => route.fulfill({ status: 503, json: { trace_id: "unrelated", error: { code: "fixture_unavailable", message: "Outside this isolated runtime sync journey." } } }));
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "runtime-csrf",
    is_administrator: scenario !== "non-admin", identity: data.identity } }));
  await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: data.products } }));
  await page.route("**/v1/products/atlas-commerce", route => route.fulfill({ json: { status: "ok", trace_id: "product", product: data.products[0] } }));
  await page.route("**/v1/products/atlas-commerce/environments/testing", route => route.fulfill({ json: { status: "ok", trace_id: "environment", environment: data.environment } }));
  await page.route("**/v1/products/atlas-commerce/environments/testing/config-status", route => {
    configReads += 1;
    if (scenario === "loading") {
      releaseConfig = () => route.fulfill({ json: { status: "ok", trace_id: "config", config_status: data.config } }); return;
    }
    if (scenario === "config-error" || (scenario === "unknown-config-error" && applies().length)) return route.fulfill({ status: 503,
      json: { trace_id: "config-error", error: { code: "runtime_sync_unavailable", message: "Current configuration evidence is unavailable." } } });
    if (scenario === "unknown-unavailable" && applies().length) return route.fulfill({ json: { status: "ok", trace_id: "config-drift",
      config_status: { ...data.config, runtime_sync: { plan: { available: false, reason: "The current target changed." }, apply: { available: false, reason: "The current target changed." } } } } });
    return route.fulfill({ json: { status: "ok", trace_id: "config", config_status: data.config } });
  });
  await page.route("**/v1/live-target-runtime/apply", async route => {
    const body = route.request().postDataJSON() as Call["body"];
    calls.push({ body, key: route.request().headers()["idempotency-key"] });
    expect(route.request().headers()["x-csrf-token"]).toBe("runtime-csrf");
    expect(Object.keys(body).sort()).toEqual(["browser_review", "context", "instance", "mode", "product"]);
    expect(body).not.toHaveProperty("deploy"); expect(body).not.toHaveProperty("reason"); expect(body).not.toHaveProperty("runtime_settings");
    const applying = body.mode === "apply";
    if (scenario === "denied" || (applying && scenario === "apply-denied")) return route.fulfill({ status: 403,
      json: { trace_id: "denied", error: { code: "authorization_denied", message: "Runtime sync is denied for this session." } } });
    if (applying) {
      if (scenario === "terminal-unverified") return route.fulfill({ status: 409,
        json: { trace_id: "unverified-terminal", error: { code: "runtime_sync_unverified",
          message: "The update ended, but target settings are unverified. Review the current lane again." } } });
      if (scenario === "target-busy" || scenario === "key-conflict" || scenario === "lane-unqualified") return route.fulfill({ status: 409,
        json: { trace_id: "not-reserved", error: { code: scenario === "target-busy" ? "runtime_sync_target_busy" : scenario === "lane-unqualified" ? "runtime_sync_lane_unqualified" : "idempotency_key_reused",
          message: "This request did not reserve the lane." } } });
      expect(body.browser_review).toEqual({ reviewed_plan_sha256: original.plan_sha256 });
      if ((scenario === "stale" && applies().length === 1) || (scenario === "uncertain-stale" && applies().length === 2)) return route.fulfill({ status: 409,
        json: { trace_id: "stale", error: { code: "stale", message: "Reviewed runtime inputs changed before any effect." } } });
      if (["uncertain", "unknown-auth", "unknown-config-error", "unknown-unavailable", "missing-request", "uncertain-stale"].includes(scenario) && applies().length === 1) {
        saved = true; return route.abort();
      }
      if (scenario === "provider-unknown" && applies().length === 1) return route.fulfill({ status: 409,
        json: { trace_id: "unknown", error: { code: "mutation_reconciliation_required", message: "The provider write outcome requires reconciliation." } } });
      if (scenario === "unknown-auth" && applies().length === 2) return route.fulfill({ status: 403,
        json: { trace_id: "later-denial", error: { code: "authorization_denied", message: "Later authorization refusal does not settle the original sync." } } });
      saved = true;
    } else if (saved && scenario === "readback-error") return route.fulfill({ status: 503,
      json: { trace_id: "readback-error", error: { code: "runtime_sync_unavailable", message: "Current target readback unavailable." } } });
    let review: RuntimeSyncReview = { ...original, applied: applying };
    if (!applying && saved) review = { ...review, missing_keys: [], different_keys: [], retired_keys_present: [], changed_keys: [],
      unchanged_key_count: 5, plan_sha256: "b".repeat(64), target_fingerprint: "different-current-baseline" };
    if (!applying && saved && scenario === "readback-different") review = { ...review, changed_keys: ["PUBLIC_ORIGIN"], missing_keys: ["PUBLIC_ORIGIN"] };
    if (!applying && scenario === "malformed-plan") review = { ...review, changed_keys: null } as unknown as RuntimeSyncReview;
    if (!applying && scenario === "scope-plan") review = { ...review, product: "other-product" };
    if (!applying && scenario === "applied-plan") review = { ...review, applied: true };
    if (applying && scenario === "receipt-scope") review = { ...review, context: "different-context" };
    if (applying && scenario === "receipt-digest") review = { ...review, plan_sha256: "e".repeat(64) };
    if (applying && scenario === "receipt-target") review = { ...review, target_id: "other-target" };
    if (applying && scenario === "receipt-fingerprint") review = { ...review, target_fingerprint: "other-opaque-fingerprint" };
    const response = { status: 202, json: { status: "accepted", trace_id: applying ? "apply-trace" : "plan-trace", records: {},
      replayed: applying && applies().length > 1, original_trace_id: applying && applies().length > 1 ? "original-apply-trace" : null,
      result: scenario === "receipt-result" && applying ? { mode: "dry-run" } : { mode: body.mode,
        apply: { applied: applying, verification: applying ? { status: "pass" } : null,
          recovered_by_readback: applying && applies().length > 1, update_acknowledged: applying }, deploy: { requested: false, triggered: false } }, review } };
    if (!applying && scenario === "refresh") { releasePlan = () => route.fulfill(response); return; }
    return route.fulfill(response);
  });
  await page.goto("/ui/products/atlas-commerce/environments/testing/actions");
  return { scope, calls, applies, configReads: () => configReads, releasePlan: () => releasePlan, releaseConfig: () => releaseConfig,
    panel: page.getByRole("region", { name: "Runtime sync", exact: true }) };
}

const scenarios: Scenario[] = ["saved", "unchanged", "odoo", "non-admin", "denied", "apply-denied", "target-busy", "key-conflict", "lane-unqualified", "terminal-unverified", "apply-unavailable", "loading", "missing-availability", "config-error",
  "uncertain", "unknown-auth", "unknown-config-error", "unknown-unavailable", "missing-request", "provider-unknown", "stale", "uncertain-stale",
  "receipt-scope", "receipt-digest", "receipt-target", "receipt-fingerprint", "receipt-result", "malformed-plan", "scope-plan", "applied-plan",
  "readback-error", "readback-different", "refresh", "storage-unavailable"];
for (const scenario of scenarios) {
  test(`runtime sync: ${scenario}`, { tag: ["saved", "odoo", "uncertain", "readback-error", "terminal-unverified"].includes(scenario) ? "@layout" : [] }, async ({ page }, testInfo) => {
    const state = await fixture(page, scenario);
    let panel = state.panel;
    const inspect = panel.getByRole("button", { name: "Review runtime sync", exact: true });
    await expect(panel).toBeVisible();
    if (["odoo", "non-admin", "missing-availability", "config-error"].includes(scenario)) {
      await expect(inspect).toBeDisabled();
      await expect(panel.getByRole("status")).toBeVisible();
      if (scenario === "non-admin") await expect(panel).toContainText("A named administrator session is required.");
      if (scenario === "config-error") await expect(panel).toContainText("Current configuration evidence is unavailable.");
      expect(state.calls).toHaveLength(0);
      if (scenario === "odoo") {
        await expect(panel).toContainText("Runtime sync is not supported for this lane yet.");
        await panel.screenshot({ path: testInfo.outputPath("runtime-sync-unavailable.png") });
        await page.screenshot({ path: testInfo.outputPath("runtime-sync-unavailable-viewport.png") });
      }
      return;
    }
    if (scenario === "loading") {
      await expect(inspect).toBeDisabled();
      await expect(panel.getByRole("status")).toBeVisible();
      await waitForState(() => !!state.releaseConfig()).toBe(true);
      await state.releaseConfig()!();
      await expect(inspect).toBeEnabled(); expect(state.calls).toHaveLength(0); return;
    }
    await expect(inspect).toBeEnabled();
    if (scenario === "saved") await page.screenshot({ path: testInfo.outputPath("runtime-sync-initial-viewport.png") });
    await inspect.click();
    if (scenario === "refresh") {
      await waitForState(() => !!state.releasePlan()).toBe(true);
      const before = state.configReads();
      await page.getByRole("button", { name: "Refresh current evidence" }).click();
      await waitForState(state.configReads).toBeGreaterThan(before);
      await state.releasePlan()!();
      await expect(panel).toHaveAttribute("aria-busy", "false");
      await expect(panel.getByLabel("Runtime sync review", { exact: true })).toHaveCount(0); return;
    }
    if (scenario === "denied") { await expect(panel.getByRole("alert")).toContainText("Runtime sync is denied"); return; }
    if (["malformed-plan", "scope-plan", "applied-plan"].includes(scenario)) {
      await expect(panel.getByRole("status")).toBeVisible();
      await expect(panel.getByRole("button", { name: "Apply reviewed runtime sync" })).toHaveCount(0); return;
    }
    const reviewed = panel.getByLabel("Runtime sync review", { exact: true });
    await expect(reviewed).toBeVisible();
    if (scenario === "unchanged") {
      await expect(panel.getByRole("button", { name: "Apply reviewed runtime sync" })).toHaveCount(0); return;
    }
    const apply = panel.getByRole("button", { name: "Apply reviewed runtime sync", exact: true });
    await expect(apply).toBeDisabled();
    if (scenario === "apply-unavailable") {
      await expect(panel).toContainText("Runtime sync apply is paused");
      await expect(reviewed.getByRole("checkbox")).toBeDisabled(); expect(state.applies()).toHaveLength(0); return;
    }
    await reviewed.getByRole("checkbox").check();
    await expect(apply).toBeEnabled();
    if (scenario === "saved") {
      await expect(reviewed).toContainText("Recorded testing destination");
      await expect(reviewed).toContainText("WORKER_TOKEN_WITH_A_LONG_NAME_FOR_WRAPPING");
      await reviewed.getByText("Review evidence", { exact: true }).click();
      await page.evaluate(() => window.scrollTo({ top: 0 }));
      await page.screenshot({ path: testInfo.outputPath("runtime-sync-reviewed-viewport.png") });
      await page.screenshot({ path: testInfo.outputPath("runtime-sync-reviewed-page.png"), fullPage: true });
      await reviewed.evaluate(element => element.scrollIntoView({ block: "start" }));
      await page.evaluate(() => window.scrollBy(0, -150));
      await page.screenshot({ path: testInfo.outputPath("runtime-sync-reviewed-detail-viewport.png") });
    }
    if (scenario === "storage-unavailable") await page.evaluate(() => {
      const original = Storage.prototype.setItem;
      Storage.prototype.setItem = function(key, value) {
        if (key.startsWith("launchplane:runtime-sync-request:")) throw new DOMException("Blocked", "QuotaExceededError");
        return original.call(this, key, value);
      };
    });
    await apply.click();
    if (scenario === "storage-unavailable") {
      await expect(panel.getByRole("status")).toBeVisible(); expect(state.applies()).toHaveLength(0); return;
    }
    if (scenario === "terminal-unverified") {
      await expect(panel).toHaveAttribute("data-recovery", "false");
      await expect(panel).toHaveAttribute("data-outcome", "unverified");
      await expect(panel.getByRole("button", { name: "Review runtime sync", exact: true })).toBeEnabled();
      await expect(panel.getByRole("alert")).toContainText("target settings are unverified");
      await panel.evaluate(element => element.scrollIntoView({ block: "start" }));
      await page.evaluate(() => window.scrollBy(0, -80));
      await page.screenshot({ path: testInfo.outputPath("runtime-sync-terminal-unverified-viewport.png") });
      expect(state.applies()).toHaveLength(1); return;
    }
    if (scenario === "target-busy" || scenario === "key-conflict" || scenario === "lane-unqualified") {
      await expect(panel).toHaveAttribute("data-recovery", "false");
      await expect(panel.getByRole("alert")).toContainText("This request did not reserve the lane.");
      await expect(panel.getByRole("button", { name: "Review runtime sync", exact: true })).toBeEnabled();
      expect(state.applies()).toHaveLength(1); return;
    }
    if (scenario === "apply-denied") {
      await expect(panel.getByRole("alert")).toContainText("Runtime sync is denied");
      await expect(panel).toHaveAttribute("data-recovery", "false"); return;
    }
    if (scenario.startsWith("receipt-")) {
      await expect(panel).toHaveAttribute("data-recovery", "true");
      await expect(panel.getByRole("alert")).toBeVisible();
      await expect(panel.locator(".runtime-sync-receipt")).toHaveCount(0); return;
    }
    if (["uncertain", "unknown-auth", "unknown-config-error", "unknown-unavailable", "missing-request", "provider-unknown", "uncertain-stale"].includes(scenario)) {
      await expect(panel).toHaveAttribute("data-recovery", "true");
      await expect(inspect).toBeDisabled();
      if (scenario === "uncertain") {
        await panel.screenshot({ path: testInfo.outputPath("runtime-sync-unknown.png") });
        await panel.locator(".runtime-sync-recovery").scrollIntoViewIfNeeded();
        await page.screenshot({ path: testInfo.outputPath("runtime-sync-unknown-viewport.png") });
      }
      if (scenario === "missing-request") await page.evaluate(key => sessionStorage.removeItem(key), runtimeSyncStorageKey(state.scope));
      await page.reload(); panel = page.getByRole("region", { name: "Runtime sync", exact: true });
      if (scenario === "missing-request") {
        await expect(panel.getByRole("button", { name: "Resume original runtime sync" })).toHaveCount(0);
        await expect(panel.getByRole("button", { name: "Review runtime sync", exact: true })).toBeDisabled();
        expect(state.applies()).toHaveLength(1); return;
      }
      const resume = panel.getByRole("button", { name: "Resume original runtime sync", exact: true });
      await expect(resume).toBeEnabled(); await resume.click();
      await waitForState(() => state.applies().length).toBe(2);
      expect(state.applies()[1]).toEqual(state.applies()[0]);
      if (scenario === "unknown-auth") {
        await expect(panel).toHaveAttribute("data-recovery", "true");
        await expect(panel.getByRole("alert")).toContainText("Later authorization refusal");
        await page.reload(); panel = page.getByRole("region", { name: "Runtime sync", exact: true });
        await panel.getByRole("button", { name: "Resume original runtime sync", exact: true }).click();
        await waitForState(() => state.applies().length).toBe(3);
        expect(state.applies()[2]).toEqual(state.applies()[0]);
      }
    }
    if (scenario === "stale" || scenario === "uncertain-stale") {
      await expect(panel).toHaveAttribute("data-recovery", "false");
      await expect(panel.getByRole("status")).toBeVisible();
      await expect(panel.getByRole("button", { name: "Review runtime sync", exact: true })).toBeEnabled();
      await expect(panel.getByLabel("Runtime sync review", { exact: true })).toHaveCount(0); return;
    }
    await expect(panel).toHaveAttribute("data-operation-phase", "succeeded");
    await expect(panel).toHaveAttribute("data-outcome", state.applies().length > 1 ? "recovered" : "verified");
    await expect(panel).toHaveAttribute("data-readback", scenario === "readback-error" ? "unavailable" : scenario === "readback-different" ? "different" : "verified");
    await expect(panel).toHaveAttribute("data-recovery", "false");
    await expect(panel.locator(".runtime-sync-receipt")).toBeVisible();
    expect(state.applies()[0].key).toBeTruthy();
    expect(await page.evaluate(() => JSON.stringify(sessionStorage))).not.toContain("inert-secret-value");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    if (["saved", "readback-error", "uncertain"].includes(scenario)) {
      await panel.screenshot({ path: testInfo.outputPath(`runtime-sync-${scenario === "saved" ? "verified" : "readback-unavailable"}.png`) });
      await panel.locator(".runtime-sync-receipt").scrollIntoViewIfNeeded();
      await panel.evaluate(element => element.scrollIntoView({ block: "start" }));
      await page.evaluate(() => window.scrollBy(0, -80));
      await page.screenshot({ path: testInfo.outputPath(`runtime-sync-${scenario === "saved" ? "verified" : "readback-unavailable"}-viewport.png`) });
      if (scenario === "uncertain") await page.screenshot({ path: testInfo.outputPath("runtime-sync-recovered-viewport.png") });
    }
  });
}

for (const form of ["runtime-settings", "managed-secrets"] as const) {
for (const mode of ["supported", "odoo", "unavailable", "other-target", "fixture"] as const) {
test(`runtime sync next action ${form}: ${mode}`, { tag: ["supported", "odoo"].includes(mode) ? "@layout" : [] }, async ({ page }, testInfo) => {
  const state = await fixture(page, mode === "odoo" ? "odoo" : mode === "unavailable" ? "apply-unavailable" : "saved");
  await page.route("**/v1/products/atlas-commerce/environments/testing/config/apply", async route => {
    const body = route.request().postDataJSON();
    const response = await page.evaluate(async request => {
      const f = await import("/ui/src/dev-fixtures.ts");
      return f.applyProductEnvironmentConfigForFixture("products", "atlas-commerce", "testing", request);
    }, body);
    if (mode === "other-target") for (const action of response.result.next_actions) action.target.context = "another-context";
    await route.fulfill({ status: 202, json: response });
  });
  await page.goto(`/ui/products/atlas-commerce/environments/testing/${form}${mode === "fixture" ? "?fixture=products" : ""}`);
  const field = page.locator(".product-config-field").filter({ hasText: form === "runtime-settings" ? "PUBLIC_ORIGIN" : "SMTP_PASSWORD" });
  await field.getByRole("checkbox").check();
  await field.getByRole("textbox").fill(form === "runtime-settings" ? "https://review.example.invalid" : "inert-secret-value");
  await page.getByLabel("Change reason").fill("Review the recorded origin.");
  await page.getByRole("button", { name: "Run dry-run", exact: true }).click();
  const next = page.getByRole("link", { name: "Review runtime sync", exact: true });
  if (mode !== "supported") {
    await expect(next).toHaveCount(0);
    await expect(page.locator(".runtime-sync-inspect-only")).toBeVisible();
    if (mode === "odoo") await page.screenshot({ path: testInfo.outputPath(`runtime-sync-${form}-inspect-only.png`), fullPage: true });
    expect(state.calls).toHaveLength(0); return;
  }
  await expect(next).toHaveAttribute("href", "/ui/products/atlas-commerce/environments/testing/actions");
  await next.click();
  await expect(state.panel).toBeVisible();
  await expect(state.panel.getByRole("button", { name: "Review runtime sync", exact: true })).toBeEnabled();
  expect(state.calls).toHaveLength(0);
});
}
}
