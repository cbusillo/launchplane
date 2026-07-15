import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  applyProductEnvironmentConfig,
  dryRunGenericWebProdPromotion,
  LaunchplaneApiError,
} from "../src/api.ts";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    headers: { "Content-Type": "application/json" },
    status,
  });
}

function acceptedResponse(traceId) {
  return {
    records: {},
    result: {},
    status: "accepted",
    trace_id: traceId,
  };
}

test("generated mutation sends only the shared CSRF and idempotency headers", async () => {
  const calls = [];
  let dispatched = false;
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ input: String(input), init });
    if (String(input) === "/v1/auth/session") {
      return jsonResponse({ csrf_token: "csrf-current" });
    }
    assert.equal(dispatched, true);
    return jsonResponse(acceptedResponse("trace-apply"), 202);
  };

  await applyProductEnvironmentConfig(
    "demo-product",
    "prod",
    { mode: "dry-run", runtime_settings: { PUBLIC_ORIGIN: "https://example.invalid" } },
    {
      idempotencyKey: " stable-operation-key ",
      onDispatch: () => {
        dispatched = true;
      },
    },
  );

  assert.equal(calls.length, 2);
  assert.equal(calls[0].init.method, "GET");
  assert.equal(calls[1].init.method, "POST");
  assert.equal(calls[1].init.credentials, "same-origin");
  const headers = new Headers(calls[1].init.headers);
  assert.equal(headers.get("X-CSRF-Token"), "csrf-current");
  assert.equal(headers.get("Idempotency-Key"), "stable-operation-key");
  assert.equal(headers.get("Authorization"), null);
  assert.equal(headers.get("Cookie"), null);
  assert.equal(dispatched, true);
  assert.equal(
    calls[1].input,
    "/v1/products/demo-product/environments/prod/config/apply",
  );
  assert.equal("product" in JSON.parse(calls[1].init.body), false);
});

test("promotion dry-run transport overrides false and omitted input", async () => {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ input: String(input), init });
    if (String(input) === "/v1/auth/session") {
      return jsonResponse({ csrf_token: "csrf-current" });
    }
    return jsonResponse(acceptedResponse("trace-dry-run"), 202);
  };

  await dryRunGenericWebProdPromotion(
    {
      product: "demo-product",
      promotion: { dry_run: false, product: "demo-product" },
    },
    { idempotencyKey: "promotion-dry-run" },
  );
  await dryRunGenericWebProdPromotion(
    {
      product: "demo-product",
      promotion: { product: "demo-product" },
    },
    { idempotencyKey: "promotion-dry-run-omitted" },
  );

  assert.equal(JSON.parse(calls[1].init.body).promotion.dry_run, true);
  assert.equal(JSON.parse(calls[3].init.body).promotion.dry_run, true);
});

test("browser mutations serialize and refresh CSRF before every attempt", async () => {
  const calls = [];
  let sessionCount = 0;
  let postCount = 0;
  let releaseFirstPost;
  let markFirstPostStarted;
  const firstPostGate = new Promise((resolve) => {
    releaseFirstPost = resolve;
  });
  const firstPostStarted = new Promise((resolve) => {
    markFirstPostStarted = resolve;
  });

  globalThis.fetch = async (input, init = {}) => {
    const path = String(input);
    calls.push({ path, init });
    if (path === "/v1/auth/session") {
      sessionCount += 1;
      return jsonResponse({ csrf_token: `csrf-${sessionCount}` });
    }
    postCount += 1;
    if (postCount === 1) {
      markFirstPostStarted();
      await firstPostGate;
    }
    return jsonResponse(acceptedResponse(`trace-${postCount}`), 202);
  };

  const first = applyProductEnvironmentConfig(
    "first-product",
    "prod",
    { mode: "dry-run", runtime_settings: { PUBLIC_ORIGIN: "first" } },
    { idempotencyKey: "operation-first" },
  );
  await firstPostStarted;
  const second = applyProductEnvironmentConfig(
    "second-product",
    "testing",
    { mode: "dry-run", runtime_settings: { PUBLIC_ORIGIN: "second" } },
    { idempotencyKey: "operation-second" },
  );
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.deepEqual(
    calls.map(({ path, init }) => `${init.method}:${path}`),
    [
      "GET:/v1/auth/session",
      "POST:/v1/products/first-product/environments/prod/config/apply",
    ],
  );

  releaseFirstPost();
  await Promise.all([first, second]);

  assert.deepEqual(
    calls.map(({ path, init }) => `${init.method}:${path}`),
    [
      "GET:/v1/auth/session",
      "POST:/v1/products/first-product/environments/prod/config/apply",
      "GET:/v1/auth/session",
      "POST:/v1/products/second-product/environments/testing/config/apply",
    ],
  );
  assert.equal(
    new Headers(calls[3].init.headers).get("X-CSRF-Token"),
    "csrf-2",
  );
});

test("pre-dispatch cancellation does not send the mutation", async () => {
  const controller = new AbortController();
  controller.abort();
  const calls = [];
  let dispatched = false;
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ input: String(input), init });
    throw init.signal?.reason ?? new DOMException("Aborted", "AbortError");
  };

  await assert.rejects(
    applyProductEnvironmentConfig(
      "cancelled-product",
      "prod",
      { mode: "dry-run", runtime_settings: { PUBLIC_ORIGIN: "cancelled" } },
      {
        idempotencyKey: "operation-cancelled",
        onDispatch: () => {
          dispatched = true;
        },
        signal: controller.signal,
      },
    ),
  );

  assert.equal(calls.length, 1);
  assert.equal(calls[0].input, "/v1/auth/session");
  assert.equal(dispatched, false);
});

test("Launchplane errors retain code, trace, and status", async () => {
  globalThis.fetch = async (input) => {
    if (String(input) === "/v1/auth/session") {
      return jsonResponse({ csrf_token: "csrf-current" });
    }
    return jsonResponse(
      {
        error: {
          code: "idempotency_conflict",
          message: "The key was already used for another payload.",
        },
        trace_id: "trace-conflict",
      },
      409,
    );
  };

  await assert.rejects(
    applyProductEnvironmentConfig(
      "conflict-product",
      "prod",
      { mode: "dry-run", runtime_settings: { PUBLIC_ORIGIN: "conflict" } },
      { idempotencyKey: "operation-conflict" },
    ),
    (error) => {
      assert.ok(error instanceof LaunchplaneApiError);
      assert.equal(error.code, "idempotency_conflict");
      assert.equal(error.statusCode, 409);
      assert.equal(error.traceId, "trace-conflict");
      return true;
    },
  );
});
