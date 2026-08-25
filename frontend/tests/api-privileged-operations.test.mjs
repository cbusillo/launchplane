import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  approvePrivilegedOperation,
  readPrivilegedOperationPlans,
  revokePrivilegedOperation,
} from "../src/api.ts";

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

test("privileged-operation UI scopes policy plan reads by descriptor", async () => {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ input: String(input), init });
    return new Response(
      JSON.stringify({
        status: "ok",
        trace_id: "trace-policy-operations",
        total: 0,
        records: [],
      }),
      { headers: { "Content-Type": "application/json" }, status: 200 },
    );
  };

  await readPrivilegedOperationPlans(undefined, "managed-authz-policy-set");

  assert.equal(
    calls[0].input,
    "/v1/privileged-operations/plans?descriptor_id=managed-authz-policy-set",
  );
  assert.equal(calls[0].init.method, "GET");
});

test("privileged-operation UI sends approve and revoke mutations without execute", async () => {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ input: String(input), init });
    return new Response(
      JSON.stringify({
        status: "ok",
        trace_id: "trace-privileged-operation-mutation",
        write_status: "written",
        record: {},
        events: [],
      }),
      { headers: { "Content-Type": "application/json" }, status: 200 },
    );
  };

  await approvePrivilegedOperation("operation-1", "Reviewed plan");
  await revokePrivilegedOperation("operation-1", "Approval withdrawn");

  const mutations = calls.filter(({ input }) =>
    input.includes("/v1/privileged-operations/"),
  );
  assert.equal(mutations.length, 2);
  assert.equal(
    mutations[0].input,
    "/v1/privileged-operations/plans/operation-1/approve",
  );
  assert.equal(
    mutations[1].input,
    "/v1/privileged-operations/plans/operation-1/revoke",
  );
  assert.equal(mutations[0].init.method, "POST");
  assert.equal(mutations[1].init.method, "POST");
  assert.ok(!calls.some(({ input }) => input.includes("/execute")));
});
