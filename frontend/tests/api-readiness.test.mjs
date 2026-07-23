import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  LaunchplaneApiError,
  readProductOperationalReadiness,
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

test("operational readiness read encodes exact path and query authority", async () => {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ input: String(input), init });
    return jsonResponse({ status: "ok", trace_id: "trace-ready", readiness: {} });
  };

  await readProductOperationalReadiness({
    path: {
      product: "demo/product",
      context: "stable lane",
      instance: "testing",
    },
    query: {
      action: "odoo_target_replacement_plan.read",
      artifact_id: "artifact/current",
      expected_current_artifact_id: "artifact expected",
    },
  });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].init.method, "GET");
  assert.equal(calls[0].init.credentials, "same-origin");
  assert.equal(calls[0].init.body, undefined);
  assert.equal(
    calls[0].input,
    "/v1/products/demo%2Fproduct/contexts/stable%20lane/instances/testing/operational-readiness?action=odoo_target_replacement_plan.read&artifact_id=artifact%2Fcurrent&expected_current_artifact_id=artifact+expected",
  );
  const headers = new Headers(calls[0].init.headers);
  assert.equal(headers.get("X-CSRF-Token"), null);
  assert.equal(headers.get("Idempotency-Key"), null);
});

test("operational readiness read omits absent artifact selectors", async () => {
  let requestedPath = "";
  globalThis.fetch = async (input) => {
    requestedPath = String(input);
    return jsonResponse({ status: "ok", trace_id: "trace-missing", readiness: {} });
  };

  await readProductOperationalReadiness({
    path: { product: "demo", context: "demo", instance: "prod" },
    query: { action: "fixture.inspect" },
  });

  assert.equal(
    requestedPath,
    "/v1/products/demo/contexts/demo/instances/prod/operational-readiness?action=fixture.inspect",
  );
});

test("operational readiness read preserves API failure evidence", async () => {
  globalThis.fetch = async () =>
    jsonResponse(
      {
        status: "rejected",
        trace_id: "trace-readiness-denied",
        error: {
          code: "authorization_denied",
          message: "Readiness evidence is unavailable to this browser session.",
        },
      },
      403,
    );

  await assert.rejects(
    readProductOperationalReadiness({
      path: { product: "demo", context: "demo", instance: "testing" },
      query: { action: "fixture.inspect" },
    }),
    (error) => {
      assert.ok(error instanceof LaunchplaneApiError);
      assert.equal(error.statusCode, 403);
      assert.equal(error.code, "authorization_denied");
      assert.equal(error.traceId, "trace-readiness-denied");
      return true;
    },
  );
});
