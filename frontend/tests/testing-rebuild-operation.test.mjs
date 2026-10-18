import assert from "node:assert/strict";
import test from "node:test";
import { LaunchplaneApiError } from "../src/api.ts";
import { readTestingRebuildAttempt, runTestingRebuild, saveTestingRebuildAttempt } from "../src/testing-rebuild-operation.ts";

const original = { product: "example", key: "original-click", payload: {
  artifact_id: "verified-build", source_git_ref: "c".repeat(40), data_source_mode: "empty", reason: "Fresh testing copy",
} };
const receipt = (status, operationId = "native-original") => ({ trace_id: "trace", operation: {
  product: original.product, instance: "testing", operation_id: operationId, status, phase: status === "pass" ? "completed" : "running", updated_at: "2026-10-17T00:00:00Z",
} });

function dependencies(overrides = {}) {
  return { submit: async () => receipt("pending"), lookup: async () => receipt("pending"), read: async () => receipt("pass"), wait: async () => {}, persist: () => {}, progress: () => {}, ...overrides };
}

test("one click submits once, persists the accepted id and only polls that operation", async () => {
  let writes = 0;
  const saved = [];
  const result = await runTestingRebuild(original, dependencies({
    submit: async (product, payload, options) => { writes++; assert.equal(product, original.product); assert.deepEqual(payload, original.payload); assert.equal(options.idempotencyKey, original.key); return receipt("pending"); },
    read: async (product, id) => { assert.equal(product, original.product); assert.equal(id, "native-original"); return receipt("pass"); },
    persist: value => saved.push(value),
  }));
  assert.equal(result.operation.status, "pass");
  assert.equal(writes, 1);
  assert.equal(saved.at(-1).operationId, result.operation.operation_id);
});

test("resuming a known operation after reload performs no submission", async () => {
  const result = await runTestingRebuild({ ...original, operationId: "native-original" }, dependencies({ submit: async () => { assert.fail("No new write on reload"); } }));
  assert.equal(result.operation.status, "pass");
});

test("a lost enqueue reply retains the original payload and key without retrying", async () => {
  let writes = 0;
  let saved;
  await assert.rejects(runTestingRebuild(original, dependencies({
    submit: async () => { writes++; throw new Error("reply lost"); }, persist: value => { saved = value; },
  })));
  assert.equal(writes, 1);
  assert.deepEqual(saved, { ...original, dispatchStarted: true });
});

test("a denial is retained and cannot be resubmitted by recovery", async () => {
  let saved;
  let writes = 0;
  const denied = dependencies({ submit: async () => { writes++; throw new LaunchplaneApiError("Denied", 403, "denial-trace", "authorization_denied"); }, persist: value => { saved = value; } });
  await assert.rejects(runTestingRebuild(original, denied), error => error.traceId === "denial-trace");
  await assert.rejects(runTestingRebuild(saved, denied));
  assert.equal(writes, 1);
  assert.equal(saved.denied, true);
});

test("a changed-build admission refusal returns to inspection without another write", async () => {
  let saved;
  let writes = 0;
  await assert.rejects(runTestingRebuild(original, dependencies({
    submit: async () => { writes++; throw new LaunchplaneApiError("Inspect again", 409, "refusal-trace", "testing_rebuild_not_ready"); },
    persist: value => { saved = value; },
  })));
  assert.equal(saved, null);
  assert.equal(writes, 1);
});

test("an expired watch retains its admitted operation and can resume by reading", async () => {
  let saved;
  await assert.rejects(runTestingRebuild({ ...original, operationId: "native-original" }, dependencies({
    read: async () => { throw new LaunchplaneApiError("Sign in", 401, "read-trace", "authentication_required"); },
    submit: async () => assert.fail("A watch cannot write"), persist: value => { saved = value; },
  })));
  assert.equal(saved.denied, undefined);
  assert.equal(saved.operationId, "native-original");
  const result = await runTestingRebuild(saved, dependencies({ submit: async () => assert.fail("Resume remains read-only") }));
  assert.equal(result.operation.status, "pass");
});

test("lost-reply recovery uses the same key and switches to read-only lookup after a denial", async () => {
  let saved;
  let submits = 0;
  let lookups = 0;
  const deps = dependencies({
    persist: value => { saved = value; },
    submit: async (product, payload, options) => {
      submits++;
      assert.equal(options.idempotencyKey, original.key);
      assert.deepEqual(payload, original.payload);
      if (submits === 1) throw new Error("Original enqueue reply lost");
      throw new LaunchplaneApiError("Recovery refused", 403, "recovery-trace", "authorization_denied");
    },
    lookup: async (product, payload, options) => {
      lookups++;
      assert.equal(options.idempotencyKey, original.key);
      assert.deepEqual(payload, original.payload);
      return receipt("pass");
    },
  });
  await assert.rejects(runTestingRebuild(original, deps));
  await assert.rejects(runTestingRebuild(saved, deps));
  assert.equal(saved.denied, undefined);
  assert.equal(saved.readOnlyRecovery, true);
  assert.equal((await runTestingRebuild(saved, deps)).operation.status, "pass");
  assert.equal(submits, 2);
  assert.equal(lookups, 1);
});

test("a refusal during uncertain recovery cannot discard the earlier request", async () => {
  let saved;
  await assert.rejects(runTestingRebuild({ ...original, dispatchStarted: true }, dependencies({
    submit: async () => { throw new LaunchplaneApiError("Changed build", 409, "trace", "testing_rebuild_not_ready"); },
    persist: value => { saved = value; },
  })));
  assert.equal(saved.key, original.key);
  assert.deepEqual(saved.payload, original.payload);
});

test("another product or operation receipt cannot become the saved handle", async () => {
  for (const wrong of [{ ...receipt("pass"), operation: { ...receipt("pass").operation, product: "other" } }, receipt("pass", "other-operation")]) {
    let saved;
    await assert.rejects(runTestingRebuild({ ...original, operationId: "native-original" }, dependencies({ read: async () => wrong, persist: value => { saved = value; } })));
    assert.equal(saved.operationId, "native-original");
  }
});

test("the original attempt survives storage and does not cross product scope", () => {
  const values = new Map();
  const storage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
  saveTestingRebuildAttempt(original.product, original, storage);
  assert.deepEqual(readTestingRebuildAttempt(original.product, storage), { ...original, dispatchStarted: true, denied: false, readOnlyRecovery: false });
  assert.equal(readTestingRebuildAttempt("other", storage), null);
  saveTestingRebuildAttempt(original.product, null, storage);
  assert.equal(readTestingRebuildAttempt(original.product, storage), null);
});
