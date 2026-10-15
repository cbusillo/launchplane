import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { LaunchplaneApiError, listProducts, logout, readAuthSession, subscribeToSessionExpiry } from "../src/api.ts";

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
      [401, "reauthentication_required", 0],
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

test("an already-ended logout response is not replayed or broadcast as a GET expiry", async () => {
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
    assert.equal((await logout()).status, "ok");
    assert.equal(methods.filter(method => method === "POST").length, 1);
    assert.equal(notifications, 0);
  } finally {
    unsubscribe();
  }
});

test("an ended session in CSRF preflight completes sign-out without dispatching a mutation", async () => {
  let notifications = 0;
  let posts = 0;
  const unsubscribe = subscribeToSessionExpiry(() => { notifications++; });
  globalThis.fetch = async (_path, options) => {
    if (options.method === "POST") posts++;
    return failure(401, "authentication_required");
  };
  try {
    assert.equal((await logout()).status, "ok");
    assert.equal(notifications, 1);
    assert.equal(posts, 0);
  } finally {
    unsubscribe();
  }
});

const originalSetTimeout = globalThis.setTimeout;
afterEach(() => { globalThis.setTimeout = originalSetTimeout; });

test("pending verification retries the read before one logout dispatch", async () => {
  globalThis.setTimeout = callback => { queueMicrotask(callback); return 0; };
  const methods = [];
  let reads = 0;
  globalThis.fetch = async (_path, options) => {
    methods.push(options.method);
    if (options.method === "GET") {
      return ++reads === 1 ? failure(503, "identity_verification_pending")
        : new Response(JSON.stringify({ csrf_token: "current" }));
    }
    return new Response(JSON.stringify({ status: "ok" }));
  };
  await logout();
  assert.equal(reads, 2);
  assert.equal(methods.filter(method => method === "POST").length, 1);
});

test("pending verification is bounded and cancellation never dispatches a write", { timeout: 5000 }, async () => {
  globalThis.setTimeout = callback => originalSetTimeout(callback, 0);
  let posts = 0;
  globalThis.fetch = async (_path, options) => {
    if (options.method === "POST") posts++;
    return failure(503, "identity_verification_pending");
  };
  await assert.rejects(logout(), error => error instanceof LaunchplaneApiError && error.statusCode === 503);
  const controller = new AbortController();
  let cancelledReads = 0;
  globalThis.fetch = async () => {
    cancelledReads++;
    controller.abort();
    return failure(503, "identity_verification_pending");
  };
  await assert.rejects(readAuthSession(controller.signal));
  assert.equal(cancelledReads, 1);
  const waitingController = new AbortController();
  cancelledReads = 0;
  globalThis.fetch = async () => {
    cancelledReads++;
    return failure(503, "identity_verification_pending");
  };
  globalThis.setTimeout = callback => {
    waitingController.abort();
    return originalSetTimeout(callback, 0);
  };
  await assert.rejects(readAuthSession(waitingController.signal), error => error.name === "AbortError");
  assert.equal(cancelledReads, 1);
  assert.equal(posts, 0);
});

test("a dispatched pending-verification mutation response is never retried", async () => {
  let posts = 0;
  globalThis.fetch = async (_path, options) => {
    if (options.method === "GET") return new Response(JSON.stringify({ csrf_token: "current" }));
    posts++;
    return failure(503, "identity_verification_pending");
  };
  await assert.rejects(logout(), LaunchplaneApiError);
  assert.equal(posts, 1);
});
