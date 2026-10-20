import assert from "node:assert/strict";
import test from "node:test";
import { LaunchplaneApiError } from "../src/api.ts";
import { beginBrowserOperation, completeBrowserOperation, failBrowserOperation, markBrowserOperationDispatched,
  prepareBrowserOperation, recoverBrowserOperationState, persistBrowserOperationState, retryBrowserOperation } from "../src/browser-operation.ts";
import { checkedRuntimeSyncReview, parsePendingRuntimeSync, runtimeSyncFailureCertainty, runtimeSyncReadbackMatches,
  runtimeSyncRequest, runtimeSyncScope, verifyRuntimeSyncReceipt } from "../src/runtime-sync-operation.ts";

const scope = { product: "sample-site", context: "sample-testing", instance: "testing" };
const review = { ...scope, operation: "runtime-sync", plan_sha256: "a".repeat(64), target_fingerprint: "opaque:fixture/live-target",
  target_id: "fixture-target", target_type: "application", target_name: "Recorded testing target", missing_keys: ["PUBLIC_ORIGIN"],
  different_keys: ["WORKER_TOKEN"], retired_keys_present: [], changed_keys: ["PUBLIC_ORIGIN", "WORKER_TOKEN"],
  desired_key_count: 3, live_key_count: 2, unchanged_key_count: 1, applied: false };
function response(value = review) { return { status: "accepted", trace_id: "fixture-trace", result: { mode: value.applied ? "apply" : "dry-run",
  apply: { applied: value.applied, verification: value.applied ? { status: "pass" } : null }, deploy: { requested: false, triggered: false } }, records: {}, review: value }; }

test("retained runtime sync preserves only the complete reviewed request and value-free evidence", () => {
  const pending = { request: runtimeSyncRequest(scope, review), review };
  assert.deepEqual(parsePendingRuntimeSync(JSON.stringify(pending), scope), pending);
  assert.deepEqual(runtimeSyncRequest(scope), { ...scope, mode: "dry-run", browser_review: {} });
  const invalid = [
    { ...pending, extra: true }, { ...pending, review: { ...review, applied: true } },
    { ...pending, review: { ...review, plan_sha256: "b".repeat(64) } }, { ...pending, review: { ...review, plaintext: "must never be retained" } },
    ...[{ ...pending.request, mode: "dry-run" }, { ...pending.request, deploy: true }, { ...pending.request, reason: "not a browser field" },
      { ...pending.request, context: "another-context" }, { ...pending.request, browser_review: {} },
      { ...pending.request, browser_review: { reviewed_plan_sha256: "invalid" } },
      { ...pending.request, browser_review: { reviewed_plan_sha256: review.plan_sha256, extra: true } }].map(request => ({ ...pending, request })),
  ];
  for (const value of invalid) assert.equal(parsePendingRuntimeSync(JSON.stringify(value), scope), null);
  for (const value of [null, "{}", "[]", "invalid"]) assert.equal(parsePendingRuntimeSync(value, scope), null);
  assert.equal(parsePendingRuntimeSync(JSON.stringify(pending), { ...scope, instance: "prod" }), null);
  assert.notEqual(runtimeSyncScope(scope), runtimeSyncScope({ ...scope, context: "sample:testing" }));
});

test("review and original receipt reject wrong scope, digest, target and malformed evidence", () => {
  const pending = { request: runtimeSyncRequest(scope, review), review };
  assert.deepEqual(checkedRuntimeSyncReview(response(), scope), review);
  assert.throws(() => checkedRuntimeSyncReview({ ...response(), trace_id: 42 }, scope));
  assert.throws(() => checkedRuntimeSyncReview({ ...response(), result: { mode: "apply" } }, scope));
  assert.deepEqual(verifyRuntimeSyncReceipt(response({ ...review, applied: true }), scope, pending), { ...review, applied: true });
  for (const changed of [{ product: "other" }, { context: "other" }, { instance: "prod" }, { operation: "other" },
    { target_fingerprint: "" }, { missing_keys: [3] }, { changed_keys: null }, { desired_key_count: -1 }, { live_key_count: 1.5 }, { plan_sha256: "invalid" }]) {
    assert.throws(() => checkedRuntimeSyncReview(response({ ...review, ...changed }), scope));
  }
  for (const changed of [{ applied: false }, { plan_sha256: "b".repeat(64) }, { target_id: "other" },
    { target_type: "compose" }, { target_name: "Other" }, { target_fingerprint: "another opaque fingerprint" }]) {
    assert.throws(() => verifyRuntimeSyncReceipt(response({ ...review, applied: true, ...changed }), scope, pending));
  }
});

test("current readback allows changed digest/fingerprint but cannot replace original target or health proof", () => {
  const applied = { ...review, applied: true };
  const current = { ...review, changed_keys: [], plan_sha256: "b".repeat(64), target_fingerprint: "new opaque target baseline" };
  assert.equal(runtimeSyncReadbackMatches(applied, current), true);
  assert.equal(runtimeSyncReadbackMatches(applied, { ...current, target_id: "new-target" }), false);
  assert.equal(runtimeSyncReadbackMatches(applied, { ...current, changed_keys: ["PUBLIC_ORIGIN"] }), false);
  assert.equal(runtimeSyncReadbackMatches(applied, { ...current, applied: true }), false);
  assert.equal(applied.plan_sha256, review.plan_sha256);
});

test("contradictory or missing apply evidence never settles a receipt", () => {
  const pending = { request: runtimeSyncRequest(scope, review), review };
  const accepted = response({ ...review, applied: true });
  for (const result of [null, {}, { ...accepted.result, mode: "dry-run" }, { ...accepted.result, apply: { applied: false, verification: { status: "pass" } } },
    { ...accepted.result, apply: { applied: true } }, { ...accepted.result, apply: { applied: true, verification: { status: "fail" } } },
    { ...accepted.result, deploy: { requested: true, triggered: false } }, { ...accepted.result, deploy: { requested: false, triggered: true } }]) {
    assert.throws(() => verifyRuntimeSyncReceipt({ ...accepted, result }, scope, pending));
  }
});

test("only the route's durable stale refusal settles an uncertain original key", async () => {
  const request = runtimeSyncRequest(scope, review);
  let state = markBrowserOperationDispatched(beginBrowserOperation(await prepareBrowserOperation(runtimeSyncScope(scope), request)));
  const original = state.identity;
  state = failBrowserOperation(state, { code: "request_failed", message: "lost", statusCode: 0, traceId: "" }, "uncertain");
  const values = new Map();
  const storage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
  persistBrowserOperationState(runtimeSyncScope(scope), state, storage);
  state = recoverBrowserOperationState(runtimeSyncScope(scope), storage);
  assert.deepEqual(state.identity, original);
  state = markBrowserOperationDispatched(beginBrowserOperation(await prepareBrowserOperation(runtimeSyncScope(scope), request, retryBrowserOperation(state))));
  const denied = new LaunchplaneApiError("Denied", 403, "denied-trace", "authorization_denied");
  state = failBrowserOperation(state, { code: denied.code, message: denied.message, statusCode: denied.statusCode, traceId: denied.traceId }, runtimeSyncFailureCertainty(denied, true));
  assert.equal(state.requiresIdempotencyContinuity, true);
  await assert.rejects(() => prepareBrowserOperation(runtimeSyncScope(scope), { ...request, instance: "prod" }, state));
  state = markBrowserOperationDispatched(beginBrowserOperation(await prepareBrowserOperation(runtimeSyncScope(scope), request, retryBrowserOperation(state))));
  const stale = new LaunchplaneApiError("Stale", 409, "stale-trace", "stale");
  state = failBrowserOperation(state, { code: stale.code, message: stale.message, statusCode: stale.statusCode, traceId: stale.traceId }, runtimeSyncFailureCertainty(stale, true));
  assert.equal(state.requiresIdempotencyContinuity, false);
  assert.deepEqual(state.identity, original);
});

test("provider, receipt and unknown post-dispatch failures preserve uncertainty", () => {
  for (const error of [new Error("bad receipt"), new DOMException("Aborted", "AbortError"),
    new LaunchplaneApiError("failed", 400, "", "dokploy_target_verification_failed"),
    new LaunchplaneApiError("unknown", 409, "", "mutation_reconciliation_required"),
    new LaunchplaneApiError("busy", 409, "", "mutation_in_progress"),
    new LaunchplaneApiError("unavailable", 503, "", "runtime_sync_unavailable"),
    new LaunchplaneApiError("key conflict", 409, "", "idempotency_key_reused")]) {
    assert.equal(runtimeSyncFailureCertainty(error, true), "uncertain");
    assert.equal(runtimeSyncFailureCertainty(error, false), "definitive");
  }
});
