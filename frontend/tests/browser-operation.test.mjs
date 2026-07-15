import assert from "node:assert/strict";
import test from "node:test";

import {
  beginBrowserOperation,
  browserOperationIdentity,
  cancelBrowserOperation,
  completeBrowserOperation,
  createBrowserOperationState,
  failBrowserOperation,
  markBrowserOperationDispatched,
  persistBrowserOperationState,
  prepareBrowserOperation,
  recoverBrowserOperationState,
  resetBrowserOperation,
  retryBrowserOperation,
  stableRequestFingerprint,
} from "../src/browser-operation.ts";

function memoryStorage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    removeItem: (key) => values.delete(key),
    setItem: (key, value) => values.set(key, value),
  };
}

test("stable fingerprints ignore object key order", async () => {
  assert.equal(
    await stableRequestFingerprint({ alpha: 1, nested: { beta: 2, gamma: 3 } }),
    await stableRequestFingerprint({ nested: { gamma: 3, beta: 2 }, alpha: 1 }),
  );
});

test("operation identity survives retries but changes with scope or payload", async () => {
  const first = await browserOperationIdentity("product-config", { product: "demo" });
  const retry = await browserOperationIdentity(
    "product-config",
    { product: "demo" },
    first,
  );
  const changedPayload = await browserOperationIdentity(
    "product-config",
    { product: "demo", reason: "updated" },
    first,
  );
  const changedScope = await browserOperationIdentity(
    "promotion",
    { product: "demo" },
    first,
  );

  assert.equal(retry.idempotencyKey, first.idempotencyKey);
  assert.notEqual(changedPayload.idempotencyKey, first.idempotencyKey);
  assert.notEqual(changedScope.idempotencyKey, first.idempotencyKey);
});

test("dispatched operation identity survives navigation as uncertain continuity", async () => {
  const storage = memoryStorage();
  const prepared = await prepareBrowserOperation(
    "promotion:workflow",
    { product: "demo", dry_run: false },
    createBrowserOperationState(),
  );
  const submitting = markBrowserOperationDispatched(beginBrowserOperation(prepared));

  persistBrowserOperationState("promotion:workflow", submitting, storage);
  const recovered = recoverBrowserOperationState("promotion:workflow", storage);

  assert.equal(recovered.phase, "uncertain");
  assert.equal(recovered.requiresIdempotencyContinuity, true);
  assert.equal(
    recovered.identity?.idempotencyKey,
    submitting.identity?.idempotencyKey,
  );
  assert.equal(recovered.failure?.code, "request_interrupted");

  persistBrowserOperationState(
    "promotion:workflow",
    completeBrowserOperation(submitting, { trace_id: "trace-complete" }),
    storage,
  );
  assert.equal(
    recoverBrowserOperationState("promotion:workflow", storage).phase,
    "idle",
  );
});

test("uncertain cancellation can only retry with the existing key", async () => {
  const prepared = await prepareBrowserOperation(
    "product-config",
    { mode: "apply", product: "demo" },
    createBrowserOperationState(),
  );
  const queued = beginBrowserOperation(prepared);
  const submitting = markBrowserOperationDispatched(queued);
  const uncertain = cancelBrowserOperation(submitting);

  assert.equal(uncertain.phase, "uncertain");
  assert.equal(uncertain.identity?.idempotencyKey, prepared.identity?.idempotencyKey);
  assert.throws(() => resetBrowserOperation(uncertain), /Cannot discard/);
  await assert.rejects(
    prepareBrowserOperation(
      "product-config",
      { mode: "apply", product: "other" },
      uncertain,
    ),
    /previous result is uncertain/,
  );

  const retry = retryBrowserOperation(uncertain);
  assert.equal(retry.phase, "ready");
  assert.equal(retry.identity?.idempotencyKey, prepared.identity?.idempotencyKey);
  assert.throws(() => resetBrowserOperation(retry), /Cannot discard/);
  await assert.rejects(
    prepareBrowserOperation(
      "product-config",
      { mode: "apply", product: "changed-after-retry" },
      retry,
    ),
    /previous result is uncertain/,
  );
});

test("cancellation before dispatch is safe to reset", async () => {
  const prepared = await prepareBrowserOperation("product-config", {
    mode: "dry-run",
    product: "demo",
  });
  const cancelled = cancelBrowserOperation(beginBrowserOperation(prepared));

  assert.equal(cancelled.phase, "cancelled");
  assert.equal(resetBrowserOperation(cancelled).phase, "idle");
});

test("replay receipts preserve original trace evidence", async () => {
  const prepared = await prepareBrowserOperation("promotion", { product: "demo" });
  const succeeded = completeBrowserOperation(
    markBrowserOperationDispatched(beginBrowserOperation(prepared)),
    {
      original_trace_id: "trace-original",
      replayed: true,
      trace_id: "trace-replay",
    },
  );

  assert.deepEqual(succeeded.receipt, {
    originalTraceId: "trace-original",
    replayed: true,
    traceId: "trace-replay",
  });
  assert.equal(succeeded.phase, "succeeded");

  const reset = resetBrowserOperation(succeeded);
  const next = await prepareBrowserOperation("promotion", { product: "demo" }, reset);
  assert.notEqual(next.identity?.idempotencyKey, prepared.identity?.idempotencyKey);
});

test("definitive failures retry without replacing the operation identity", async () => {
  const prepared = await prepareBrowserOperation("promotion", { product: "demo" });
  const failed = failBrowserOperation(
    markBrowserOperationDispatched(beginBrowserOperation(prepared)),
    {
      code: "authorization_denied",
      message: "Caller is not authorized.",
      statusCode: 403,
      traceId: "trace-denied",
    },
    "definitive",
  );
  const retry = retryBrowserOperation(failed);

  assert.equal(failed.phase, "failed");
  assert.equal(retry.identity?.idempotencyKey, prepared.identity?.idempotencyKey);
});
