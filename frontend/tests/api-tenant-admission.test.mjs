import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { readTenantAdmissionEvaluation } from "../src/api.ts";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test("tenant admission read encodes the complete exact candidate without browser mutation headers", async () => {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ input: String(input), init });
    return new Response(
      JSON.stringify({ status: "ok", trace_id: "trace-tenant", read_model: {} }),
      { headers: { "Content-Type": "application/json" }, status: 200 },
    );
  };

  await readTenantAdmissionEvaluation({
    query: {
      product: "example product",
      context: "example/context",
      repository_id: "1001",
      repository_owner_id: "2001",
      repository: "example/tenant-site",
      pull_request_number: 69,
      head_sha: "a".repeat(40),
      base_branch: "release lane",
      merge_method: "squash",
    },
  });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].init.method, "GET");
  assert.equal(calls[0].init.credentials, "same-origin");
  assert.equal(calls[0].init.body, undefined);
  assert.equal(
    calls[0].input,
    "/v1/work-graph/tenant-admission/evaluation?product=example+product&context=example%2Fcontext&repository_id=1001&repository_owner_id=2001&repository=example%2Ftenant-site&pull_request_number=69&head_sha=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&base_branch=release+lane&merge_method=squash",
  );
  const headers = new Headers(calls[0].init.headers);
  assert.equal(headers.get("X-CSRF-Token"), null);
  assert.equal(headers.get("Idempotency-Key"), null);
});
