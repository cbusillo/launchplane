import assert from "node:assert/strict";
import { test } from "node:test";
import { LaunchplaneApiError } from "../src/api.ts";
import { loadSelectedOperationPlans, selectedPlanType } from "../src/privileged-operation-selection.ts";

const plans = { status: "ok", trace_id: "empty", total: 0, reviews: [] };
const denied = () => new LaunchplaneApiError("Denied", 403, "denied-trace", "authorization_denied");

test("initial view skips denied types and accepts an empty readable list", async () => {
  const calls = [];
  const result = await loadSelectedOperationPlans(null, new AbortController().signal, async type => {
    calls.push(type);
    if (type !== "managed-merge-train-policy-import") throw denied();
    return plans;
  });
  assert.deepEqual(calls, ["managed-secret-reencryption", "managed-authz-policy-set", "managed-merge-train-policy-import"]);
  assert.equal(result.descriptorId, "managed-merge-train-policy-import");
  assert.equal(result.plans, plans);
});

test("an explicit denied selection does not switch tabs and preserves error evidence", async () => {
  const calls = [];
  await assert.rejects(loadSelectedOperationPlans("managed-secret-reencryption", new AbortController().signal, async type => {
    calls.push(type);
    throw denied();
  }), error => error.statusCode === 403 && error.traceId === "denied-trace" && error.code === "authorization_denied" && error.message.includes("secret rotation plans"));
  assert.deepEqual(calls, ["managed-secret-reencryption"]);
  assert.equal(selectedPlanType("managed-merge-train-policy-import"), "managed-merge-train-policy-import");
  assert.equal(selectedPlanType("unknown"), null);
});

test("no readable supported type produces an honest empty selection", async () => {
  const result = await loadSelectedOperationPlans(null, new AbortController().signal, async () => { throw denied(); });
  assert.deepEqual(result, { descriptorId: null, plans: null });
});

test("authentication and service errors never select another plan type", async () => {
  for (const error of [new LaunchplaneApiError("Sign in", 401), new LaunchplaneApiError("Unavailable", 503), new LaunchplaneApiError("Forbidden", 403, "", "other")]) {
    let reads = 0;
    await assert.rejects(loadSelectedOperationPlans(null, new AbortController().signal, async () => { reads++; throw error; }), caught => caught === error);
    assert.equal(reads, 1);
  }
});

test("cancellation during discovery prevents further requests", async () => {
  const controller = new AbortController();
  let reads = 0;
  await assert.rejects(loadSelectedOperationPlans(null, controller.signal, async () => {
    reads++;
    controller.abort();
    throw denied();
  }), { name: "AbortError" });
  assert.equal(reads, 1);
});
