import assert from "node:assert/strict";
import test from "node:test";
import { liftRequestsReconcile, testingHoldFromResponse, testingHoldIssueLink, testingHoldLiftRequest, testingHoldPlanFromResponse,
  restoredTestingHoldRequest } from "../src/testing-hold.ts";

const scope = { product: "example-site", context: "example", instance: "testing" };
const hold = { reason: "Diagnosis: https://issues.example/42", recorded_by: "agent", recorded_at: "2026-10-20T20:00:00Z" };

test("hold reads preserve evidence and reject missing or mismatched lane evidence", () => {
  assert.deepEqual(testingHoldFromResponse({ result: { ...scope, hold } }, scope), hold);
  assert.equal(testingHoldFromResponse({ result: { ...scope, hold: null } }, scope), null);
  for (const result of [{ ...scope }, { ...scope, product: "other", hold }, { ...scope, hold: { reason: "missing actor/time" } }]) {
    assert.throws(() => testingHoldFromResponse({ result }, scope));
  }
});

test("linked evidence accepts ordinary web links and excludes credentials and executable URLs", () => {
  assert.equal(testingHoldIssueLink(hold), "https://issues.example/42");
  for (const reason of ["No issue recorded", "javascript:alert(1)", "https://user:password@issues.example/42"]) {
    assert.equal(testingHoldIssueLink({ ...hold, reason }), null);
  }
});

test("review must be a scoped hold clear with a digest", () => {
  const result = { ...scope, action: "clear", before: hold, plan_sha256: "a".repeat(64), changed: true, applied: false };
  assert.equal(testingHoldPlanFromResponse({ result }, scope).plan_sha256, result.plan_sha256);
  const staging = { ...hold, purpose: "isolation_staging" };
  assert.deepEqual(testingHoldPlanFromResponse({ result: { ...result, before: staging } }, scope).before, staging);
  for (const invalid of [{ ...result, action: "set" }, { ...result, plan_sha256: "" }, { ...result, applied: true }]) {
    assert.throws(() => testingHoldPlanFromResponse({ result: invalid }, scope));
  }
});

test("a lift names the reviewed hold's purpose and expects a reconcile only for a staff hold", () => {
  assert.deepEqual(testingHoldLiftRequest(scope, hold, "Diagnosis complete"),
    { ...scope, mode: "dry-run", hold: false, reason: "Diagnosis complete" });
  assert.deepEqual(testingHoldLiftRequest(scope, { ...hold, purpose: "isolation_staging" }, "Separation proven"),
    { ...scope, mode: "dry-run", hold: false, reason: "Separation proven", purpose: "isolation_staging" });
  assert.equal(liftRequestsReconcile({ purpose: "staff_testing" }), true);
  assert.equal(liftRequestsReconcile({}), true);
  assert.equal(liftRequestsReconcile({ purpose: "isolation_staging" }), false);
});

test("recovery retains only the exact lane's reviewed lift", () => {
  const request = { ...scope, mode: "apply", hold: false, reason: "Diagnosis complete", reviewed_plan_sha256: "a".repeat(64) };
  assert.deepEqual(restoredTestingHoldRequest(JSON.stringify(request), scope), request);
  const staging = { ...request, purpose: "isolation_staging" };
  assert.deepEqual(restoredTestingHoldRequest(JSON.stringify(staging), scope), staging);
  for (const invalid of [{ ...request, product: "other" }, { ...request, hold: true }, { ...request, rehearsal: true }, { ...request, activation_plan_sha256: "b".repeat(64) },
    { ...staging, activation_plan_sha256: "b".repeat(64) }, { ...request, purpose: "other" }]) {
    assert.equal(restoredTestingHoldRequest(JSON.stringify(invalid), scope), null);
  }
});
