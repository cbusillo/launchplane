import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  LaunchplaneApiError,
  listProductEnvironmentIncidents,
  readProductEnvironmentIncident,
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

test("incident reads encode product environment and incident identity", async () => {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ input: String(input), init });
    return jsonResponse({ status: "ok", trace_id: "trace-incident", incident_list: {} });
  };

  await listProductEnvironmentIncidents("demo/product", "stable lane");
  await readProductEnvironmentIncident(
    "demo/product",
    "stable lane",
    "incident/value",
  );

  assert.deepEqual(
    calls.map((call) => call.input),
    [
      "/v1/products/demo%2Fproduct/environments/stable%20lane/public-ingress/incidents",
      "/v1/products/demo%2Fproduct/environments/stable%20lane/public-ingress/incidents/incident%2Fvalue",
    ],
  );
  for (const call of calls) {
    assert.equal(call.init.method, "GET");
    assert.equal(call.init.credentials, "same-origin");
    assert.equal(call.init.body, undefined);
    const headers = new Headers(call.init.headers);
    assert.equal(headers.get("X-CSRF-Token"), null);
    assert.equal(headers.get("Idempotency-Key"), null);
  }
});

test("incident reads preserve authorization failure evidence", async () => {
  globalThis.fetch = async () =>
    jsonResponse(
      {
        status: "rejected",
        trace_id: "trace-incident-denied",
        error: {
          code: "authorization_denied",
          message: "Incident evidence is unavailable to this browser session.",
        },
      },
      403,
    );

  await assert.rejects(
    listProductEnvironmentIncidents("demo", "prod"),
    (error) => {
      assert.ok(error instanceof LaunchplaneApiError);
      assert.equal(error.statusCode, 403);
      assert.equal(error.code, "authorization_denied");
      assert.equal(error.traceId, "trace-incident-denied");
      return true;
    },
  );
});
