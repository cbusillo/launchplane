import { waitForState, expect, test, type Page } from "./state";
import type { DokployDomainRepairReview, RouteBindingRepairReview, EnvironmentRouteBindingReadModel } from "../../src/generated/openapi.ts";
import { repairOperationScope, repairStorageKey, type RepairKind } from "../../src/route-record-repair";

const scope = { product: "atlas-commerce", context: "atlas-testing", instance: "testing" };
const proposedDomains = ["testing.example.invalid", "alias.example.invalid"];
const planDigest = "a".repeat(64);
function bindingRecord(): EnvironmentRouteBindingReadModel {
  return { schema_version: 1, ...scope, provider_target: { provider_id: "dokploy", target_category: "application", provider_target_type: "application", target_name: "Recorded testing destination" },
    ingress: { provider: "edge", endpoint_key: "recorded-testing-edge", termination_kind: "edge" },
    domains: proposedDomains.map((domain_name, index) => ({ domain_name, role: index === 0 ? "primary" : "alias" })), tls: { owner: "launchplane" },
    status: "active", source: { source_kind: "service", source_label: "recorded-audit", source_record_ids: ["fixture-audit"],
      source_versions: { "fixture-audit": "fixture-version" }, refreshed_at: "2026-10-19T10:00:00Z", freshness_status: "verified", stale_after: "" },
    updated_at: "2026-10-19T10:00:00Z", record_sha256: "b".repeat(64) };
}
function domainReview(applied: boolean, stored = false): DokployDomainRepairReview {
  return { ...scope, operation: "repair-domain-authority", status: stored ? "unchanged" : "ready", plan_sha256: stored ? "c".repeat(64) : planDigest,
    applied, expected_confirmation: "fixture-reviewed-domain-confirmation", target_type: "application", target_id: "fixture-target", target_name: "Recorded testing destination",
    current_domains: stored ? proposedDomains : ["https://testing.example.invalid/"], proposed_domains: proposedDomains,
    observed_domains: proposedDomains, proposed_primary_domain: proposedDomains[0], source_records: [{ identity: "dokploy-target:atlas-testing:testing", version: "fixture-source-version" }] };
}
function bindingReview(applied: boolean, stored = false): RouteBindingRepairReview {
  const record = bindingRecord();
  return { ...scope, operation: "reconcile-route-binding", status: stored ? "unchanged" : "ready", binding_operation: stored ? "none" : "create",
    plan_sha256: stored ? "c".repeat(64) : planDigest, applied,
    expected_confirmation: "fixture-reviewed-binding-confirmation", expected_current: stored ? { state: "present", record_sha256: record.record_sha256 } : { state: "absent", record_sha256: "" },
    current_record_sha256: stored ? record.record_sha256 : "", current_record: stored ? record : null, proposed_record: record,
    current_domains: stored ? proposedDomains : [], proposed_domains: proposedDomains, findings: [], source_records: [{ identity: "fixture-audit", version: "fixture-version" }] };
}
type Scenario = "saved" | "blocked" | "unchanged" | "denied" | "role-only" | "read-only" | "uncertain" | "stale" | "uncertain-stale" | "uncertain-conflict" | "readback-denied" | "readback-failed" |
  "storage-unavailable" | "missing-request" | "receipt-mismatch" | "receipt-digest-mismatch" | "current-drift" | "refresh" | "readback-refresh" | "edit" | "leave" |
  "persistent-refusal" | "changed-confirmation";

async function fixture(page: Page, kind: RepairKind, scenario: Scenario) {
  await page.goto("/ui/products?fixture=empty");
  const fixtures = await page.evaluate(async () => {
    const module = await import("/ui/src/dev-fixtures.ts");
    return { identity: module.fixtureIdentity, products: module.productsForFixture("products"),
      detail: module.environmentForFixture("products", "atlas-commerce", "testing")! };
  });
  scope.context = fixtures.detail.context;
  for (const product of fixtures.products) product.environments = product.environments.filter((item, index, items) =>
    items.findIndex(other => other.context === item.context && other.environment === item.environment) === index);
  const calls: Array<{ key: string; body: { mode: string; browser_review: { reviewed_plan_sha256: string } } }> = [];
  let environmentReads = 0;
  let stored = scenario === "unchanged";
  let activeDigest = planDigest;
  let activeConfirmation: string | undefined;
  let releaseReview: (() => Promise<void>) | undefined;
  await page.route("**/v1/**", route => route.fulfill({ status: 403, json: { status: "error", trace_id: "unused", error: { code: "fixture_unavailable", message: "Outside this isolated route-record journey." } } }));
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "fixture-csrf",
    is_administrator: !["role-only", "read-only"].includes(scenario), identity: { ...fixtures.identity, role: scenario === "read-only" ? "read_only" : "admin" } } }));
  await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: fixtures.products } }));
  await page.route("**/v1/products/atlas-commerce", route => route.fulfill({ json: { status: "ok", trace_id: "product", product: fixtures.products[0] } }));
  await page.route("**/v1/products/atlas-commerce/environments/testing", route => {
    environmentReads += 1;
    return route.fulfill({ json: { status: "ok", trace_id: "environment", environment: fixtures.detail } });
  });
  await page.route("**/v1/product-profiles/atlas-commerce", route => route.fulfill({ status: 404, json: { status: "error", trace_id: "profile", error: { code: "not_found", message: "No profile in this isolated fixture." } } }));
  const endpoint = kind === "domain" ? "dokploy-targets/setup" : "route-bindings/reconcile";
  await page.route(`**/v1/${endpoint}`, async route => {
    const body = route.request().postDataJSON();
    expect(route.request().headers()["x-csrf-token"]).toBe("fixture-csrf");
    expect(body).not.toHaveProperty("target_id"); expect(body).not.toHaveProperty("domains"); expect(body).not.toHaveProperty("provider_evidence");
    calls.push({ key: route.request().headers()["idempotency-key"], body });
    if (scenario === "denied") return route.fulfill({ status: 403, json: { trace_id: "denied-plan", error: { code: "authorization_denied", message: "The exact lane route-record review is denied." } } });
    const applying = body.mode === "apply";
    const applies = calls.filter(call => call.body.mode === "apply");
    if (!applying && stored && scenario !== "unchanged") {
      if (scenario === "readback-denied") return route.fulfill({ status: 403, json: { trace_id: "denied-readback", error: { code: "authorization_denied", message: "Current record readback denied." } } });
      if (scenario === "readback-failed") return route.abort();
    }
    let review = kind === "domain" ? domainReview(applying, !applying && stored) : bindingReview(applying, !applying && stored);
    if (activeConfirmation) review = { ...review, expected_confirmation: activeConfirmation };
    if (applying || !stored) review = { ...review, plan_sha256: activeDigest };
    if (applying) {
      if (scenario === "changed-confirmation" && applies.length > 1 && body.confirmation !== review.expected_confirmation) {
        return route.fulfill({ status: 400, json: { trace_id: "confirmation-refusal", error: { code: "confirmation_required", message: "Review the current confirmation." } } });
      }
      expect(body.confirmation).toBe(review.expected_confirmation);
      expect(body.browser_review.reviewed_plan_sha256).toBe(activeDigest);
      if (["persistent-refusal", "changed-confirmation"].includes(scenario) && applies.length === 1) {
        if (scenario === "changed-confirmation") activeConfirmation = "fixture-confirmation-after-deploy";
        return route.abort();
      }
      if (scenario === "persistent-refusal" && applies.at(-1)!.key === applies[0].key) {
        return route.fulfill({ status: 409, json: { trace_id: "qualification-refusal", error: {
          code: kind === "domain" ? "domain_repair_evidence_unqualified" : "route_repair_lane_unqualified", message: "The original evidence is no longer qualified." } } });
      }
      if (["uncertain-stale", "uncertain-conflict"].includes(scenario) && applies.length === 1) return route.abort();
      if (scenario === "uncertain-conflict" && applies.length === 2) return route.fulfill({ status: 409, json: { trace_id: "in-progress", error: { code: "mutation_in_progress", message: "Original mutation is still in progress." } } });
      if ((scenario === "stale" && applies.length === 1) || (scenario === "uncertain-stale" && applies.length === 2)) {
        activeDigest = "f".repeat(64);
        return route.fulfill({ status: 409, json: { trace_id: "stale-refusal", error: { code: "stale", message: "Reviewed source rows changed before commit." } } });
      }
      stored = true;
      if (["uncertain", "current-drift", "missing-request"].includes(scenario) && applies.length === 1) return route.abort();
      if (scenario === "receipt-mismatch") review = { ...review, product: "different-product" };
      if (scenario === "receipt-digest-mismatch") review = { ...review, plan_sha256: "e".repeat(64) };
    }
    if (scenario === "blocked" && review.operation === "reconcile-route-binding") review = { ...review, status: "blocked", binding_operation: "none", proposed_record: null,
      findings: [{ code: "missing_edge_join", detail: "The terminal audit does not join an active edge endpoint." }] };
    if (scenario === "current-drift" && !applying && stored) review = review.operation === "repair-domain-authority"
      ? { ...review, current_domains: ["other.example.invalid"] }
      : { ...review, current_record_sha256: "d".repeat(64), current_record: { ...bindingRecord(), record_sha256: "d".repeat(64) } };
    const response = { status: 202, json: { status: "accepted", trace_id: applying ? "apply-trace" : "review-trace", records: {},
      replayed: applying && applies.length > 1, original_trace_id: applying && applies.length > 1 ? "original-apply-trace" : null,
      result: { mode: body.mode }, review } };
    if (!applying && (scenario === "refresh" || (scenario === "readback-refresh" && stored))) { releaseReview = () => route.fulfill(response); return; }
    return route.fulfill(response);
  });
  await page.goto("/ui/products/atlas-commerce/environments/testing/actions");
  return { calls, environmentReads: () => environmentReads, releaseReview: () => releaseReview, panel: page.getByRole("region", { name: kind === "domain" ? "Domain authority" : "Route binding", exact: true }) };
}

for (const kind of ["domain", "binding"] as const) {
  const title = kind === "domain" ? "Domain authority" : "Route binding";
  const scenarios: Scenario[] = ["saved", "unchanged", "denied", "uncertain", "stale", "uncertain-stale", "readback-denied", "current-drift", "refresh", "readback-refresh", "edit", "leave", "persistent-refusal"];
  if (kind === "domain") scenarios.push("role-only", "read-only", "uncertain-conflict", "readback-failed", "storage-unavailable", "missing-request", "receipt-mismatch", "receipt-digest-mismatch", "changed-confirmation");
  if (kind === "binding") scenarios.push("blocked");
  for (const scenario of scenarios) {
    test(`${kind} repair: ${scenario}`, { tag: ["saved", "blocked", "readback-denied", "persistent-refusal"].includes(scenario) ? "@layout" : [] }, async ({ page }, testInfo) => {
      const state = await fixture(page, kind, scenario);
      let panel = state.panel;
      const review = panel.getByRole("button", { name: `Review ${title.toLowerCase()}`, exact: true });
      const reason = panel.getByLabel(`${title} reason`, { exact: true });
      if (["role-only", "read-only"].includes(scenario)) { await expect(review).toBeDisabled(); expect(state.calls).toHaveLength(0); return; }
      await expect(reason).toBeEnabled(); await reason.fill("Recognize the reviewed testing route records."); await review.click();
      if (scenario === "refresh") {
        await waitForState(() => Boolean(state.releaseReview())).toBe(true);
        const readsBeforeRefresh = state.environmentReads();
        await page.getByRole("button", { name: "Refresh current evidence" }).click();
        await waitForState(state.environmentReads).toBeGreaterThan(readsBeforeRefresh);
        await state.releaseReview()!();
        await expect(panel).toHaveAttribute("aria-busy", "false");
        await expect(panel.getByLabel(`${title} review`)).toHaveCount(0); return;
      }
      if (scenario === "denied") { await expect(panel.getByRole("alert")).toBeVisible(); return; }
      const reviewed = panel.getByLabel(`${title} review`, { exact: true });
      await expect(reviewed).toBeVisible();
      if (scenario === "saved") {
        await panel.screenshot({ path: testInfo.outputPath("route-repair-reviewed.png") });
        await panel.evaluate(element => element.scrollIntoView({ block: "start" }));
        await page.evaluate(() => window.scrollBy(0, -100));
        await page.screenshot({ path: testInfo.outputPath("route-repair-reviewed-viewport.png") });
        await page.screenshot({ path: testInfo.outputPath("route-repair-page-reviewed.png"), fullPage: true });
      }
      if (scenario === "blocked" || scenario === "unchanged") {
        await expect(panel.getByRole("button", { name: `Apply reviewed ${title.toLowerCase()}` })).toHaveCount(0);
        if (scenario === "blocked") { await panel.screenshot({ path: testInfo.outputPath("route-repair-blocked.png") }); }
        return;
      }
      if (scenario === "edit") { await reason.fill("A changed reason invalidates the old review."); await expect(reviewed).toHaveCount(0); return; }
      if (scenario === "leave") { await panel.getByRole("button", { name: "Leave records unchanged" }).click(); await expect(reviewed).toHaveCount(0); expect(state.calls.filter(call => call.body.mode === "apply")).toHaveLength(0); return; }
      if (scenario === "storage-unavailable") await page.evaluate(() => {
        const original = Storage.prototype.setItem;
        Storage.prototype.setItem = function(key, value) { if (key.startsWith("launchplane:route-repair-request:")) throw new DOMException("Blocked", "QuotaExceededError"); return original.call(this, key, value); };
      });
      await reviewed.getByRole("checkbox").check();
      await panel.getByRole("button", { name: `Apply reviewed ${title.toLowerCase()}`, exact: true }).click();
      const applies = () => state.calls.filter(call => call.body.mode === "apply");
      if (scenario === "readback-refresh") {
        await expect(panel).toHaveAttribute("data-operation-phase", "succeeded");
        await expect(panel).toHaveAttribute("data-readback", "pending");
        await waitForState(() => Boolean(state.releaseReview())).toBe(true);
        const readsBeforeRefresh = state.environmentReads();
        await page.getByRole("button", { name: "Refresh current evidence" }).click();
        await waitForState(state.environmentReads).toBeGreaterThan(readsBeforeRefresh);
        await state.releaseReview()!();
        await expect(panel).toHaveAttribute("data-operation-phase", "succeeded");
        await expect(panel).toHaveAttribute("data-readback", "unavailable");
        await expect(panel).toHaveAttribute("data-recovery", "false");
        await expect(review).toBeEnabled();
        expect(applies()).toHaveLength(1);
        return;
      }
      if (scenario === "stale" || scenario === "uncertain-stale") {
        if (scenario === "uncertain-stale") {
          await expect(panel).toHaveAttribute("data-recovery", "true");
          await panel.getByRole("button", { name: `Resume original ${title.toLowerCase()}` }).click();
          await expect(panel).toHaveAttribute("data-operation-phase", "failed");
          expect(applies()[1]).toEqual(applies()[0]);
        } else await expect(panel).toHaveAttribute("data-operation-phase", "failed");
        await expect(panel.getByRole("button", { name: `Resume original ${title.toLowerCase()}` })).toHaveCount(0);
        await expect(reviewed).toHaveCount(0); await expect(reason).toBeEnabled(); await review.click();
        await expect(reviewed).toBeVisible(); await reviewed.getByRole("checkbox").check();
        await panel.getByRole("button", { name: `Apply reviewed ${title.toLowerCase()}`, exact: true }).click();
        await expect(panel).toHaveAttribute("data-readback", "verified");
        expect(applies().at(-1)!.key).not.toBe(applies()[0].key);
        expect(applies().at(-1)!.body.browser_review.reviewed_plan_sha256).not.toBe(applies()[0].body.browser_review.reviewed_plan_sha256);
        return;
      }
      if (scenario === "storage-unavailable") { await expect(review).toBeEnabled(); expect(applies()).toHaveLength(0); return; }
      if (scenario === "saved") {
        await expect(panel).toHaveAttribute("data-readback", "verified");
        expect(applies()).toHaveLength(1);
        const readback = state.calls.at(-1)!; expect(readback.body.mode).toBe("dry-run"); expect(readback.key).not.toBe(applies()[0].key);
        expect(readback.body.browser_review.reviewed_plan_sha256).toBe("");
        await panel.getByText("Original apply receipt", { exact: true }).click();
        await expect(panel).toContainText("apply-trace");
        await panel.screenshot({ path: testInfo.outputPath("route-repair-verified.png") }); return;
      }
      if (["readback-denied", "readback-failed"].includes(scenario)) {
        await expect(panel).toHaveAttribute("data-operation-phase", "succeeded");
        await expect(panel).toHaveAttribute("data-readback", "unavailable");
        await expect(panel).toHaveAttribute("data-recovery", "false");
        await expect(review).toBeEnabled();
        const otherTitle = kind === "domain" ? "Route binding" : "Domain authority";
        await page.getByLabel(`${otherTitle} reason`, { exact: true }).fill("Review the independent decision after receipt settlement.");
        await expect(page.getByRole("button", { name: `Review ${otherTitle.toLowerCase()}`, exact: true })).toBeEnabled();
        expect(applies()).toHaveLength(1);
        await panel.screenshot({ path: testInfo.outputPath("route-repair-readback-unavailable.png") });
        return;
      }
      await expect(panel).toHaveAttribute("data-recovery", "true");
      await expect(review).toBeDisabled();
      if (["persistent-refusal", "changed-confirmation"].includes(scenario)) {
        const otherTitle = kind === "domain" ? "Route binding" : "Domain authority";
        await page.getByLabel(`${otherTitle} reason`, { exact: true }).fill("Review the independent decision while the earlier outcome is unknown.");
        await expect(page.getByRole("button", { name: `Review ${otherTitle.toLowerCase()}`, exact: true })).toBeEnabled();
        await panel.getByRole("button", { name: `Resume original ${title.toLowerCase()}` }).click();
        await waitForState(() => applies().length).toBe(2);
        await expect(panel).toHaveAttribute("aria-busy", "false");
        await expect(panel).toHaveAttribute("data-recovery", "true");
        expect(applies()[1]).toEqual(applies()[0]);
        await panel.evaluate(element => element.scrollIntoView({ block: "start" }));
        await page.evaluate(() => window.scrollBy(0, -100));
        await panel.screenshot({ path: testInfo.outputPath("route-repair-unproven.png") });
        await panel.getByRole("button", { name: "Start a separate review", exact: true }).click();
        await expect(panel).toHaveAttribute("data-recovery", "false");
        await expect(reviewed).toHaveCount(0);
        expect(applies()).toHaveLength(2);
        const earlier = panel.locator('[data-earlier-outcome="unknown"]');
        await earlier.locator("summary").click();
        await expect(earlier).toContainText(applies()[0].key);
        await expect(earlier).toContainText(scenario === "persistent-refusal" ? "qualification-refusal" : "confirmation-refusal");
        await review.click(); await expect(reviewed).toBeVisible();
        await expect(reviewed.getByRole("checkbox")).not.toBeChecked();
        await expect(panel.getByRole("button", { name: `Apply reviewed ${title.toLowerCase()}`, exact: true })).toBeDisabled();
        expect(applies()).toHaveLength(2);
        await reviewed.getByRole("checkbox").check();
        await panel.getByRole("button", { name: `Apply reviewed ${title.toLowerCase()}`, exact: true }).click();
        await expect(panel).toHaveAttribute("data-readback", "verified");
        expect(applies()).toHaveLength(3);
        expect(applies()[2].key).not.toBe(applies()[0].key);
        if (scenario === "changed-confirmation") expect(applies()[2].body).not.toEqual(applies()[0].body);
        await expect(earlier).toHaveAttribute("data-earlier-outcome", "unknown");
        await expect(earlier).toContainText(applies()[0].key);
        await expect(panel.getByText("Current apply receipt", { exact: true })).toBeVisible();
        await panel.evaluate(element => element.scrollIntoView({ block: "start" }));
        await page.evaluate(() => window.scrollBy(0, -100));
        await panel.screenshot({ path: testInfo.outputPath("route-repair-separate-review.png") });
        return;
      }
      if (["receipt-mismatch", "receipt-digest-mismatch"].includes(scenario)) { await expect(panel.getByRole("alert")).toBeVisible(); return; }
      if (scenario === "missing-request") {
        await page.evaluate(async ({ key, otherScope }) => {
          sessionStorage.removeItem(key);
          const operations = await import("/ui/src/browser-operation.ts");
          const prepared = await operations.prepareBrowserOperation(otherScope, { fixture: "independent interrupted request" });
          const submitting = operations.markBrowserOperationDispatched(operations.beginBrowserOperation(prepared));
          operations.persistBrowserOperationState(otherScope, submitting);
        }, { key: repairStorageKey(repairOperationScope(scope, kind)), otherScope: repairOperationScope(scope, "binding") });
      }
      await page.reload();
      panel = page.getByRole("region", { name: title, exact: true });
      await expect(panel).toHaveAttribute("data-recovery", "true");
      if (scenario === "missing-request") {
        const sibling = page.getByRole("region", { name: "Route binding", exact: true });
        await expect(sibling).toHaveAttribute("data-recovery", "true");
        await expect(panel.getByRole("button", { name: `Resume original ${title.toLowerCase()}` })).toHaveCount(0);
        const earlierFailureCode = await page.evaluate(async operationScope => {
          const operations = await import("/ui/src/browser-operation.ts");
          return operations.recoverBrowserOperationState(operationScope).failure!.code;
        }, repairOperationScope(scope, kind));
        await panel.getByRole("button", { name: "Start a separate review", exact: true }).click();
        await expect(panel).toHaveAttribute("data-recovery", "false");
        const earlier = panel.locator('[data-earlier-outcome="unknown"]');
        await earlier.locator("summary").click();
        await expect(earlier.locator("[data-earlier-failure-code]")).toHaveText(earlierFailureCode);
        await panel.getByLabel(`${title} reason`, { exact: true }).fill("Review current records without reconstructing the missing payload.");
        await panel.getByRole("button", { name: `Review ${title.toLowerCase()}`, exact: true }).click();
        await expect(panel.getByLabel(`${title} review`, { exact: true })).toBeVisible();
        expect(applies()).toHaveLength(1);
        await expect(sibling.getByRole("button", { name: "Start a separate review", exact: true })).toBeEnabled();
        await sibling.getByRole("button", { name: "Start a separate review", exact: true }).click();
        await expect(sibling).toHaveAttribute("data-recovery", "false");
        return;
      }
      await panel.getByRole("button", { name: `Resume original ${title.toLowerCase()}` }).click();
      if (scenario === "uncertain-conflict") {
        await waitForState(() => applies().length).toBe(2);
        await expect(panel).toHaveAttribute("aria-busy", "false");
        await expect(panel).toHaveAttribute("data-recovery", "true");
        await expect(panel.getByRole("button", { name: `Review ${title.toLowerCase()}`, exact: true })).toBeDisabled();
        expect(applies()[1]).toEqual(applies()[0]); return;
      }
      await expect(panel).toHaveAttribute("data-operation-phase", "succeeded");
      await expect(panel).toHaveAttribute("data-readback", scenario === "current-drift" ? "different" : "verified");
      await expect(panel).toHaveAttribute("data-recovery", "false");
      await expect(panel.getByRole("button", { name: `Review ${title.toLowerCase()}`, exact: true })).toBeEnabled();
      expect(applies()).toHaveLength(2); expect(applies()[1]).toEqual(applies()[0]);
      await panel.getByText("Original apply receipt", { exact: true }).click(); await expect(panel).toContainText("original-apply-trace");
      await panel.screenshot({ path: testInfo.outputPath("route-repair-recovered.png") });
    });
  }
}
