import assert from "node:assert/strict";
import test from "node:test";
import { parseStoredRepairRequest, repairRequest, repairOperationScope } from "../src/route-record-repair.ts";

const scope = { product: "example-site", context: "example-testing", instance: "testing" };
for (const kind of ["domain", "binding"]) {
  const review = { operation: kind === "domain" ? "repair-domain-authority" : "reconcile-route-binding",
    expected_confirmation: "fixture-reviewed-confirmation", plan_sha256: "a".repeat(64), expected_current: { state: "present", record_sha256: "b".repeat(64) } };
  test(`${kind} retained request validates its complete minimal shape`, () => {
    const request = repairRequest(scope, kind, "Approved route record repair", review);
    assert.deepEqual(parseStoredRepairRequest(JSON.stringify(request), scope, kind), request);
    const invalid = [
      { ...request, extra: "unexpected" }, { ...request, reason: "" }, { ...request, mode: "dry-run" },
      { ...request, confirmation: "" }, { ...request, instance: "prod" },
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

test("an unchanged binding verdict requires the same current and proposed record", async () => {
  const { checkedRepairReview } = await import("../src/route-record-repair.ts");
  const record = { ...scope, record_sha256: "b".repeat(64), provider_target: { target_name: "Testing destination" },
    ingress: { provider: "edge", endpoint_key: "testing-edge" }, tls: { owner: "launchplane" } };
  const review = { ...scope, operation: "reconcile-route-binding", status: "unchanged", binding_operation: "none",
    plan_sha256: "a".repeat(64), applied: false, expected_confirmation: "reviewed-binding",
    current_domains: [], proposed_domains: [], source_records: [], findings: [],
    expected_current: { state: "present", record_sha256: record.record_sha256 },
    current_record_sha256: record.record_sha256, current_record: record, proposed_record: record };
  const response = value => ({ status: "accepted", trace_id: "binding-review", review: value });
  assert.equal(checkedRepairReview(response(review), scope, "binding"), review);
  for (const inconsistent of [
    { current_record: null, current_record_sha256: "", expected_current: { state: "absent", record_sha256: "" } },
    { expected_current: { state: "present", record_sha256: "c".repeat(64) } },
    { proposed_record: { ...record, record_sha256: "c".repeat(64) } },
    { proposed_record: null }, { binding_operation: "create" },
  ]) assert.throws(() => checkedRepairReview(response({ ...review, ...inconsistent }), scope, "binding"));
});
