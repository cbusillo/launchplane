import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { readPrivilegedOperationPlans } from "../src/api.ts";


const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test("privileged-operation UI performs one read-only list request", async () => {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ input: String(input), init });
    return new Response(
      JSON.stringify({
        status: "ok",
        trace_id: "trace-privileged-operations",
        total: 0,
        records: [],
      }),
      { headers: { "Content-Type": "application/json" }, status: 200 },
    );
  };

  const response = await readPrivilegedOperationPlans();

  assert.equal(calls.length, 1);
  assert.equal(calls[0].input, "/v1/privileged-operations/plans");
  assert.equal(calls[0].init.method, "GET");
  assert.equal(response.total, 0);
});
