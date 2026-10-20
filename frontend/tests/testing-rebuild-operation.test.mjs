import assert from "node:assert/strict";
import test from "node:test";
import { LaunchplaneApiError } from "../src/api.ts";
import { readTestingRebuildAttempt, runTestingRebuild, resumeTestingRebuildAttempt, settleTestingRebuildAttempt, saveTestingRebuildAttempt } from "../src/testing-rebuild-operation.ts";

const original = { product: "example", key: "original-click", payload: {
  artifact_id: "verified-build", source_git_ref: "c".repeat(40), data_source_mode: "empty", reason: "Fresh testing copy", original_scope: "fixture-original-administrator",
} };
const receipt = (status, operationId = "native-original") => ({ trace_id: "trace", operation: {
  product: original.product, instance: "testing", operation_id: operationId, status, phase: status === "pass" ? "completed" : "running", updated_at: "2026-10-17T00:00:00Z", attempt: 1,
} });

function dependencies(overrides = {}) {
  return { submit: async () => receipt("pending"), lookup: async () => receipt("pending"), read: async () => receipt("pass"), wait: async () => {}, persist: () => {}, progress: () => {}, ...overrides };
}

const settlement = (outcome = "excluded", operation = null) => ({
  trace_id: "settlement", product: original.product, original_key: original.key,
  original_request: original.payload, outcome, operation,
});

test("explicit exclusion clears an uncertain original only after its correlated receipt", async () => {
  let saved = { ...original, dispatchStarted: true, readOnlyRecovery: true };
  const result = await settleTestingRebuildAttempt(saved, {
    persist: value => { saved = value; },
    settle: async (product, payload, options) => {
      assert.equal(product, original.product);
      assert.deepEqual(payload, original.payload);
      assert.equal(options.idempotencyKey, original.key);
      assert.equal(saved.key, original.key);
      return settlement();
    },
  });
  assert.equal(result.outcome, "excluded");
  assert.equal(saved, null);
});

test("settlement keeps an observed active or held rebuild and clears only terminal operations", async () => {
  for (const status of ["pending", "running", "reconciliation_required", "pass", "fail", "cancelled", "superseded"]) {
    let saved = original;
    await settleTestingRebuildAttempt(saved, {
      settle: async () => settlement("observed", receipt(status).operation),
      persist: value => { saved = value; },
    });
    if (["pending", "running", "reconciliation_required"].includes(status)) {
      assert.equal(saved.operationId, "native-original");
      assert.equal(saved.key, original.key);
      assert.equal(saved.readOnlyRecovery, true);
    } else assert.equal(saved, null);
  }
});

test("lost or denied settlement retains original intent and never turns recovery into submission", async () => {
  for (const error of [new Error("Reply lost"), new LaunchplaneApiError("Denied", 403, "trace", "authorization_denied")]) {
    let saved = { ...original, dispatchStarted: true };
    let settlements = 0;
    await assert.rejects(settleTestingRebuildAttempt(saved, {
      settle: async () => { settlements++; throw error; }, persist: value => { saved = value; },
    }));
    assert.equal(saved.key, original.key);
    assert.deepEqual(saved.payload, original.payload);
    assert.equal(saved.readOnlyRecovery, true);
    await assert.rejects(runTestingRebuild(saved, dependencies({
      submit: async () => assert.fail("Recovery cannot submit after settlement uncertainty"),
      lookup: async () => { throw new LaunchplaneApiError("Absent", 404, "trace", "testing_rebuild_not_observed"); },
    })));
    assert.equal(settlements, 1);
    await settleTestingRebuildAttempt(saved, {
      settle: async () => settlement(), persist: value => { saved = value; },
    });
    assert.equal(saved, null);
  }
});

test("foreign or contradictory settlement evidence cannot clear the saved original", async () => {
  for (const wrong of [
    { ...settlement(), product: "another-product" }, { ...settlement(), original_key: "another-key" },
    ...["original_scope", "artifact_id", "source_git_ref", "reason", "data_source_mode"].map(field => ({
      ...settlement(), original_request: { ...original.payload, [field]: "foreign" },
    })),
    settlement("excluded", receipt("pending").operation), settlement("observed", null),
    settlement("observed", { ...receipt("pass").operation, product: "another-product" }),
  ]) {
    let saved = original;
    await assert.rejects(settleTestingRebuildAttempt(saved, {
      settle: async () => wrong, persist: value => { saved = value; },
    }));
    assert.equal(saved.key, original.key);
    assert.deepEqual(saved.payload, original.payload);
  }
});

test("legacy attempts without an original actor binding remain observation-only", async () => {
  const { original_scope, ...payload } = original.payload;
  const storage = { getItem: () => JSON.stringify({ ...original, payload, dispatchStarted: true }) };
  const legacy = readTestingRebuildAttempt(original.product, storage);
  assert.equal(legacy.readOnlyRecovery, true);
  assert.equal(legacy.payload.original_scope, undefined);
  await assert.rejects(settleTestingRebuildAttempt(legacy, {
    settle: async () => assert.fail("The current actor cannot adopt an unbound original"),
    persist: () => assert.fail("Preserve the original legacy state"),
  }));
});

test("explicit resume uses the held revision, original operation and original request key", async () => {
  const held = receipt("reconciliation_required").operation;
  const saved = { ...original, operationId: held.operation_id };
  let writes = 0;
  const result = await resumeTestingRebuildAttempt(saved, held, {
    resume: async (product, id, payload, options) => {
      writes++;
      assert.equal(product, original.product);
      assert.equal(id, saved.operationId);
      assert.equal(options.idempotencyKey, original.key);
      assert.deepEqual(payload, { expected_updated_at: held.updated_at, expected_attempt: held.attempt });
      return receipt("pending");
    }, persist: () => assert.fail("A successful resume does not replace the original request"),
  });
  assert.equal(result.operation.operation_id, saved.operationId);
  assert.equal(writes, 1);
});

test("denied resume retains the original and needs a fresh read before another explicit action", async () => {
  const held = receipt("reconciliation_required").operation;
  let saved = { ...original, operationId: held.operation_id };
  let writes = 0;
  const resume = {
    resume: async () => {
      writes++;
      if (writes === 1) throw new LaunchplaneApiError("Resume denied", 403, "resume-denial", "authorization_denied");
      return receipt("pending");
    }, persist: value => { saved = value; },
  };
  await assert.rejects(resumeTestingRebuildAttempt(saved, held, resume));
  assert.equal(saved.resumeNeedsRead, true);
  assert.equal(saved.key, original.key);
  assert.equal(saved.operationId, held.operation_id);
  assert.deepEqual(saved.payload, original.payload);
  await assert.rejects(resumeTestingRebuildAttempt(saved, held, resume));
  assert.equal(writes, 1);
  await runTestingRebuild(saved, dependencies({
    submit: async () => assert.fail("A fresh read cannot submit"),
    read: async () => receipt("reconciliation_required"),
    persist: value => { saved = value; },
  }));
  assert.equal(saved.resumeNeedsRead, false);
  assert.equal(writes, 1);
  await resumeTestingRebuildAttempt(saved, held, resume);
  assert.equal(writes, 2);
});

test("a lost or foreign resume reply retains the original handle and only allows observation", async () => {
  const held = receipt("reconciliation_required").operation;
  for (const response of [new Error("Resume reply lost"), receipt("pending", "foreign")]) {
    let saved = { ...original, operationId: held.operation_id };
    let writes = 0;
    await assert.rejects(resumeTestingRebuildAttempt(saved, held, {
      resume: async () => { writes++; if (response instanceof Error) throw response; return response; },
      persist: value => { saved = value; },
    }));
    assert.equal(saved.operationId, held.operation_id);
    assert.equal(saved.resumeNeedsRead, true);
    await runTestingRebuild(saved, dependencies({
      submit: async () => assert.fail("Recovery is read-only"),
      persist: value => { saved = value; },
    }));
    assert.equal(writes, 1);
    assert.equal(saved.operationId, held.operation_id);
  }
});

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
    submit: async (_product, _payload, options) => { options.onDispatch(); writes++; throw new Error("reply lost"); }, persist: value => { saved = value; },
  })));
  assert.equal(writes, 1);
  assert.deepEqual(saved, { ...original, dispatchStarted: true });
});

test("a denial is retained and cannot be resubmitted by recovery", async () => {
  let saved;
  let writes = 0;
  const denied = dependencies({ submit: async (_product, _payload, options) => { options.onDispatch(); writes++; throw new LaunchplaneApiError("Denied", 403, "denial-trace", "authorization_denied"); }, persist: value => { saved = value; } });
  await assert.rejects(runTestingRebuild(original, denied), error => error.traceId === "denial-trace");
  await assert.rejects(runTestingRebuild(saved, denied));
  assert.equal(writes, 1);
  assert.equal(saved.denied, true);
});

test("a changed-build admission refusal returns to inspection without another write", async () => {
  let saved;
  let writes = 0;
  await assert.rejects(runTestingRebuild(original, dependencies({
    submit: async (_product, _payload, options) => { options.onDispatch(); writes++; throw new LaunchplaneApiError("Inspect again", 409, "refusal-trace", "testing_rebuild_not_ready"); },
    persist: value => { saved = value; },
  })));
  assert.equal(saved, null);
  assert.equal(writes, 1);
});

test("a dispatched key collision retains the original request for observation", async () => {
  let saved;
  await assert.rejects(runTestingRebuild(original, dependencies({
    submit: async (_product, _payload, options) => { options.onDispatch(); throw new LaunchplaneApiError("Key already used", 409, "collision-trace", "idempotency_key_reused"); },
    persist: value => { saved = value; },
  })));
  assert.equal(saved.key, original.key);
  assert.deepEqual(saved.payload, original.payload);
  assert.equal(saved.dispatchStarted, true);
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
      options.onDispatch();
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

test("a session failure before POST dispatch returns to fresh inspection", async () => {
  for (const error of [new Error("Session request failed before dispatch"),
    new LaunchplaneApiError("Sign in", 401, "session-trace", "authentication_required"),
    new LaunchplaneApiError("Session refused", 403, "session-trace", "authorization_denied")]) {
    let saved;
    await assert.rejects(runTestingRebuild(original, dependencies({
      submit: async () => { throw error; },
      persist: value => { saved = value; },
    })), observed => observed === error);
    assert.equal(saved, null);
  }
});

test("an absent read-only lookup cannot exclude an original request still in flight", async () => {
  let saved;
  let recorded = false;
  const deps = dependencies({
    submit: async () => assert.fail("A denied recovery cannot submit"),
    persist: value => { saved = value; },
    lookup: async () => {
      if (!recorded) throw new LaunchplaneApiError("Not observed yet", 404, "trace", "testing_rebuild_not_observed");
      return receipt("pass");
    },
  });
  await assert.rejects(runTestingRebuild({ ...original, dispatchStarted: true, readOnlyRecovery: true }, deps));
  assert.equal(saved.key, original.key);
  assert.equal(saved.readOnlyRecovery, true);
  recorded = true;
  assert.equal((await runTestingRebuild(saved, deps)).operation.operation_id, "native-original");
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
