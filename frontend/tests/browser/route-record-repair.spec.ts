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
    applied, target_type: "application", target_id: "fixture-target", target_name: "Recorded testing destination",
    current_domains: stored ? proposedDomains : ["https://testing.example.invalid/"], proposed_domains: proposedDomains,
    observed_domains: proposedDomains, proposed_primary_domain: proposedDomains[0], source_records: [{ identity: "dokploy-target:atlas-testing:testing", version: "fixture-source-version" }] };
}
function bindingReview(applied: boolean, stored = false): RouteBindingRepairReview {
  const record = bindingRecord();
  return { ...scope, operation: "reconcile-route-binding", status: stored ? "unchanged" : "ready", binding_operation: stored ? "none" : "create",
    plan_sha256: stored ? "c".repeat(64) : planDigest, applied,
    expected_current: stored ? { state: "present", record_sha256: record.record_sha256 } : { state: "absent", record_sha256: "" },
    current_record_sha256: stored ? record.record_sha256 : "", current_record: stored ? record : null, proposed_record: record,
    current_domains: stored ? proposedDomains : [], proposed_domains: proposedDomains, findings: [], source_records: [{ identity: "fixture-audit", version: "fixture-version" }] };
}
type Scenario = "saved" | "blocked" | "unchanged" | "denied" | "role-only" | "read-only" | "uncertain" | "stale" | "uncertain-stale" | "uncertain-conflict" | "readback-denied" | "readback-failed" |
  "storage-unavailable" | "missing-request" | "corrupt-request" | "scope-mismatch" | "fingerprint-mismatch" | "receipt-mismatch" | "receipt-digest-mismatch" | "current-drift" | "refresh" | "edit" | "leave";

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
  let stored = scenario === "unchanged";
  let readbacks = 0;
  let activeDigest = planDigest;
  let releaseReview: (() => Promise<void>) | undefined;
  await page.route("**/v1/**", route => route.fulfill({ status: 403, json: { status: "error", trace_id: "unused", error: { code: "fixture_unavailable", message: "Outside this isolated route-record journey." } } }));
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "fixture-csrf",
    is_administrator: !["role-only", "read-only"].includes(scenario), identity: { ...fixtures.identity, role: scenario === "read-only" ? "read_only" : "admin" } } }));
  await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: fixtures.products } }));
  await page.route("**/v1/products/atlas-commerce", route => route.fulfill({ json: { status: "ok", trace_id: "product", product: fixtures.products[0] } }));
  await page.route("**/v1/products/atlas-commerce/environments/testing", route => route.fulfill({ json: { status: "ok", trace_id: "environment", environment: fixtures.detail } }));
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
      readbacks += 1;
      if (readbacks === 1 && scenario === "readback-denied") return route.fulfill({ status: 403, json: { trace_id: "denied-readback", error: { code: "authorization_denied", message: "Current record readback denied." } } });
      if (readbacks === 1 && scenario === "readback-failed") return route.abort();
    }
    let review = kind === "domain" ? domainReview(applying, !applying && stored) : bindingReview(applying, !applying && stored);
    if (applying || !stored) review = { ...review, plan_sha256: activeDigest };
    if (applying) {
      expect(body.browser_review.reviewed_plan_sha256).toBe(activeDigest);
      if (["uncertain-stale", "uncertain-conflict"].includes(scenario) && applies.length === 1) return route.abort();
      if (scenario === "uncertain-conflict" && applies.length === 2) return route.fulfill({ status: 409, json: { trace_id: "in-progress", error: { code: "mutation_in_progress", message: "Original mutation is still in progress." } } });
      if ((scenario === "stale" && applies.length === 1) || (scenario === "uncertain-stale" && applies.length === 2)) {
        activeDigest = "f".repeat(64);
        return route.fulfill({ status: 409, json: { trace_id: "stale-refusal", error: { code: "stale", message: "Reviewed source rows changed before commit." } } });
      }
      stored = true;
      if (["uncertain", "missing-request", "corrupt-request", "scope-mismatch", "fingerprint-mismatch"].includes(scenario) && applies.length === 1) return route.abort();
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
    if (scenario === "refresh" && !applying) { releaseReview = () => route.fulfill(response); return; }
    return route.fulfill(response);
  });
  await page.goto("/ui/products/atlas-commerce/environments/testing/actions");
  return { calls, releaseReview: () => releaseReview, panel: page.getByRole("region", { name: kind === "domain" ? "Domain authority" : "Route binding", exact: true }) };
}

for (const kind of ["domain", "binding"] as const) {
  const title = kind === "domain" ? "Domain authority" : "Route binding";
  const scenarios: Scenario[] = ["saved", "unchanged", "denied", "role-only", "read-only", "uncertain", "stale", "uncertain-stale", "uncertain-conflict", "readback-denied", "readback-failed", "storage-unavailable",
    "missing-request", "corrupt-request", "scope-mismatch", "fingerprint-mismatch", "receipt-mismatch", "receipt-digest-mismatch", "current-drift", "refresh", "edit", "leave"];
  if (kind === "binding") scenarios.push("blocked");
  for (const scenario of scenarios) {
    test(`${kind} repair: ${scenario}`, { tag: ["saved", "blocked", "readback-denied"].includes(scenario) ? "@layout" : [] }, async ({ page }, testInfo) => {
      const state = await fixture(page, kind, scenario);
      let panel = state.panel;
      const review = panel.getByRole("button", { name: `Review ${title.toLowerCase()}`, exact: true });
      const reason = panel.getByLabel(`${title} reason`, { exact: true });
      if (["role-only", "read-only"].includes(scenario)) { await expect(review).toBeDisabled(); expect(state.calls).toHaveLength(0); return; }
      await expect(reason).toBeEnabled(); await reason.fill("Recognize the reviewed testing route records."); await review.click();
      if (scenario === "refresh") {
        await waitForState(() => Boolean(state.releaseReview())).toBe(true);
        await page.getByRole("button", { name: "Refresh current evidence" }).click();
        await state.releaseReview()!();
        await expect(panel).toHaveAttribute("aria-busy", "false");
        await expect(panel.getByLabel(`${title} review`)).toHaveCount(0); return;
      }
      if (scenario === "denied") { await expect(panel.getByRole("alert")).toContainText("review is denied"); return; }
      const reviewed = panel.getByLabel(`${title} review`, { exact: true });
      await expect(reviewed).toBeVisible();
      await expect(reviewed.getByText("Current stored domains", { exact: true })).toBeVisible();
      if (scenario === "saved") {
        await panel.screenshot({ path: testInfo.outputPath("route-repair-reviewed.png") });
        await panel.evaluate(element => element.scrollIntoView({ block: "start" }));
        await page.evaluate(() => window.scrollBy(0, -100));
        await page.screenshot({ path: testInfo.outputPath("route-repair-reviewed-viewport.png") });
        await page.screenshot({ path: testInfo.outputPath("route-repair-page-reviewed.png"), fullPage: true });
      }
      if (scenario === "blocked" || scenario === "unchanged") {
        await expect(panel.getByRole("button", { name: `Apply reviewed ${title.toLowerCase()}` })).toHaveCount(0);
        if (scenario === "blocked") { await expect(panel).toContainText("does not join an active edge endpoint"); await panel.screenshot({ path: testInfo.outputPath("route-repair-blocked.png") }); }
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
      if (scenario === "stale" || scenario === "uncertain-stale") {
        if (scenario === "uncertain-stale") {
          await expect(panel).toContainText("result needs recovery");
          await panel.getByRole("button", { name: `Resume original ${title.toLowerCase()}` }).click();
          await expect(panel).toContainText("Review the current records again");
          expect(applies()[1]).toEqual(applies()[0]);
        } else await expect(panel).toContainText("Review the current records again");
        await expect(panel.getByRole("button", { name: `Resume original ${title.toLowerCase()}` })).toHaveCount(0);
        await expect(reviewed).toHaveCount(0); await expect(reason).toBeEnabled(); await review.click();
        await expect(reviewed).toBeVisible(); await reviewed.getByRole("checkbox").check();
        await panel.getByRole("button", { name: `Apply reviewed ${title.toLowerCase()}`, exact: true }).click();
        await expect(panel).toContainText("current record readback verified");
        expect(applies().at(-1)!.key).not.toBe(applies()[0].key);
        expect(applies().at(-1)!.body.browser_review.reviewed_plan_sha256).not.toBe(applies()[0].body.browser_review.reviewed_plan_sha256);
        return;
      }
      if (scenario === "storage-unavailable") { await expect(panel).toContainText("Nothing was sent"); expect(applies()).toHaveLength(0); return; }
      if (scenario === "saved") {
        await expect(panel).toContainText("current record readback verified");
        expect(applies()).toHaveLength(1);
        const readback = state.calls.at(-1)!; expect(readback.body.mode).toBe("dry-run"); expect(readback.key).not.toBe(applies()[0].key);
        expect(readback.body.browser_review.reviewed_plan_sha256).toBe("");
        await panel.getByText("Original apply receipt", { exact: true }).click();
        await expect(panel).toContainText("apply-trace");
        await panel.screenshot({ path: testInfo.outputPath("route-repair-verified.png") }); return;
      }
      await expect(panel).toContainText("result needs recovery");
      await expect(review).toBeDisabled();
      if (["readback-denied", "readback-failed", "current-drift"].includes(scenario)) {
        await expect(panel).toContainText("accepted but current readback is unverified");
        await panel.getByText("Original apply receipt", { exact: true }).click(); await expect(panel).toContainText("apply-trace");
      }
      if (scenario === "current-drift") { await expect(panel).toContainText("current authority differs"); return; }
      if (["receipt-mismatch", "receipt-digest-mismatch"].includes(scenario)) { await expect(panel.getByRole("alert")).toContainText("does not verify"); return; }
      if (["missing-request", "corrupt-request", "scope-mismatch", "fingerprint-mismatch"].includes(scenario)) await page.evaluate(({ key, scenario }) => {
        if (scenario === "missing-request") { sessionStorage.removeItem(key); return; }
        const request = JSON.parse(sessionStorage.getItem(key)!);
        if (scenario === "corrupt-request") request.extra = "unexpected";
        else if (scenario === "scope-mismatch") request.browser_review.product = "different-product";
        else request.reason = "A different syntactically valid request";
        sessionStorage.setItem(key, JSON.stringify(request));
      }, { key: repairStorageKey(repairOperationScope(scope, kind)), scenario });
      await page.reload();
      panel = page.getByRole("region", { name: title, exact: true });
      await expect(panel).toContainText("result needs recovery");
      if (["missing-request", "corrupt-request", "scope-mismatch"].includes(scenario)) {
        await expect(panel).toContainText("saved request is missing or invalid");
        await expect(panel.getByRole("button", { name: `Resume original ${title.toLowerCase()}` })).toHaveCount(0); expect(applies()).toHaveLength(1); return;
      }
      await panel.getByRole("button", { name: `Resume original ${title.toLowerCase()}` }).click();
      if (scenario === "fingerprint-mismatch") { await expect(panel).toContainText("previous result is uncertain"); expect(applies()).toHaveLength(1); return; }
      if (scenario === "uncertain-conflict") {
        await expect(panel).toContainText("Original mutation is still in progress");
        await expect(panel).toContainText("result needs recovery");
        await expect(panel.getByRole("button", { name: `Review ${title.toLowerCase()}`, exact: true })).toBeDisabled();
        expect(applies()[1]).toEqual(applies()[0]); return;
      }
      await expect(panel).toContainText("current record readback verified");
      expect(applies()).toHaveLength(2); expect(applies()[1]).toEqual(applies()[0]);
      await panel.getByText("Original apply receipt", { exact: true }).click(); await expect(panel).toContainText("original-apply-trace");
      await panel.screenshot({ path: testInfo.outputPath("route-repair-recovered.png") });
    });
  }
}
