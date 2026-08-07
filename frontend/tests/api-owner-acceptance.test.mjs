import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { readOwnerAcceptanceQueue, writeOwnerAcceptanceEvent } from "../src/api.ts";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test("Owner acceptance write uses session CSRF and the binding-scoped idempotency key", async () => {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ input: String(input), init });
    if (String(input) === "/v1/auth/session") {
      return new Response(JSON.stringify({ csrf_token: "csrf-owner" }), {
        headers: { "Content-Type": "application/json" },
        status: 200,
      });
    }
    return new Response(JSON.stringify({ status: "ok", trace_id: "trace-write", write_status: "replayed" }), {
      headers: { "Content-Type": "application/json" },
      status: 202,
    });
  };

  const response = await writeOwnerAcceptanceEvent(
    {
      schema_version: 1,
      target: { repository: "example/site", pull_request_number: 42 },
      action: "accepted",
      expected_binding_sha256: "a".repeat(64),
      reason: "",
    },
    { idempotencyKey: "owner-binding-key" },
  );

  assert.equal(calls.length, 2);
  assert.equal(calls[1].input, "/v1/owner-acceptance/events");
  const headers = new Headers(calls[1].init.headers);
  assert.equal(headers.get("X-CSRF-Token"), "csrf-owner");
  assert.equal(headers.get("Idempotency-Key"), "owner-binding-key");
  assert.equal(response.replayed, true);
  assert.deepEqual(JSON.parse(calls[1].init.body), {
    schema_version: 1,
    target: { repository: "example/site", pull_request_number: 42 },
    action: "accepted",
    expected_binding_sha256: "a".repeat(64),
    reason: "",
  });
});

test("Owner acceptance queue sends repository and status filters to the server", async () => {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ input: String(input), init });
    return new Response(
      JSON.stringify({ status: "ok", trace_id: "trace-owner-acceptance", entries: [] }),
      { headers: { "Content-Type": "application/json" }, status: 200 },
    );
  };

  await readOwnerAcceptanceQueue({
    repository: "example/tenant site",
    status: "changes_requested",
  });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].init.method, "GET");
  assert.equal(
    calls[0].input,
    "/v1/owner-acceptance/queue?repository=example%2Ftenant+site&status=changes_requested",
  );
});
