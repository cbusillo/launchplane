import assert from "node:assert/strict";
import test from "node:test";
import { parseStoredRepairRequest, repairRequest, repairOperationScope } from "../src/route-record-repair.ts";

const scope = { product: "example-site", context: "example-testing", instance: "testing" };
for (const kind of ["domain", "binding"]) {
  const review = { operation: kind === "domain" ? "repair-domain-authority" : "reconcile-route-binding",
    plan_sha256: "a".repeat(64), expected_current: { state: "present", record_sha256: "b".repeat(64) } };
  test(`${kind} retained request validates its complete minimal shape`, () => {
    const request = repairRequest(scope, kind, "Approved route record repair", review);
    assert.deepEqual(parseStoredRepairRequest(JSON.stringify(request), scope, kind), request);
    const invalid = [
      { ...request, extra: "unexpected" }, { ...request, reason: "" }, { ...request, mode: "dry-run" },
      { ...request, confirmation: "WRONG" }, { ...request, instance: "prod" },
      { ...request, browser_review: { ...request.browser_review, extra: true } },
      { ...request, browser_review: { product: "other-product", reviewed_plan_sha256: review.plan_sha256 } },
      { ...request, browser_review: { product: scope.product, reviewed_plan_sha256: "invalid" } },
    ];
    if (kind === "domain") invalid.push({ ...request, operation: "create-compose" });
    else invalid.push({ ...request, expected_current: { state: "present", record_sha256: "" } },
      { ...request, expected_current: { state: "absent", record_sha256: review.expected_current.record_sha256 } },
      { ...request, expected_current: { state: "absent", record_sha256: "", extra: true } });
    for (const value of invalid) assert.equal(parseStoredRepairRequest(JSON.stringify(value), scope, kind), null);
    for (const value of [null, "{}", "[]", "not-json"]) assert.equal(parseStoredRepairRequest(value, scope, kind), null);
    assert.equal(parseStoredRepairRequest(JSON.stringify(request), { ...scope, context: "another-context" }, kind), null);
  });
}
test("punctuation-distinct lane scopes and operations cannot alias recovery", () => {
  assert.notEqual(repairOperationScope(scope, "domain"), repairOperationScope(scope, "binding"));
  assert.notEqual(repairOperationScope(scope, "domain"), repairOperationScope({ ...scope, context: "example:testing" }, "domain"));
});
