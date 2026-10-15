import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { LaunchplaneApiError, listProducts, logout, subscribeToSessionExpiry } from "../src/api.ts";

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

function failure(status, code) {
  return new Response(JSON.stringify({ error: { code, message: "Read failed" }, trace_id: "expiry-test" }), { status });
}

test("only an active authentication-required GET ends the browser session", async () => {
  let notifications = 0;
  const unsubscribe = subscribeToSessionExpiry(() => { notifications++; });
  try {
    for (const [status, code, expected] of [
      [403, "authorization_denied", 0],
      [503, "read_unavailable", 0],
      [401, "different_credential_required", 0],
      [401, "authentication_required", 1],
    ]) {
      globalThis.fetch = async () => failure(status, code);
      await assert.rejects(listProducts(), error => error instanceof LaunchplaneApiError &&
        error.statusCode === status && error.code === code && error.traceId === "expiry-test");
      assert.equal(notifications, expected);
    }
    const controller = new AbortController();
    globalThis.fetch = async () => {
      controller.abort();
      return failure(401, "authentication_required");
    };
    await assert.rejects(listProducts(controller.signal), LaunchplaneApiError);
    assert.equal(notifications, 1);
    globalThis.fetch = async () => { throw new TypeError("Connection lost"); };
    await assert.rejects(listProducts(), TypeError);
    assert.equal(notifications, 1);
  } finally {
    unsubscribe();
  }
  globalThis.fetch = async () => failure(401, "authentication_required");
  await assert.rejects(listProducts(), LaunchplaneApiError);
  assert.equal(notifications, 1);
});

test("a rejected mutation is not retried or broadcast as a background expiry", async () => {
  let notifications = 0;
  const methods = [];
  const unsubscribe = subscribeToSessionExpiry(() => { notifications++; });
  globalThis.fetch = async (path, options) => {
    methods.push(options.method);
    return String(path) === "/v1/auth/session"
      ? new Response(JSON.stringify({ csrf_token: "test-csrf" }))
      : failure(401, "authentication_required");
  };
  try {
    await assert.rejects(logout(), LaunchplaneApiError);
    assert.deepEqual(methods, ["GET", "POST"]);
    assert.equal(notifications, 0);
  } finally {
    unsubscribe();
  }
});
