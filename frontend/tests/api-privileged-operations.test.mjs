import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  approvePrivilegedOperation,
  cancelPrivilegedOperation,
  prepareAuthorizationCandidate,
  readPrivilegedOperationRawDetail,
  readPrivilegedOperationPlans,
  readPrivilegedOperationReview,
  revokePrivilegedOperation,
} from "../src/api.ts";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test("cancellation uses browser CSRF and preserves the source event for replay", async () => {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ input: String(input), init });
    return new Response(JSON.stringify(String(input) === "/v1/auth/session"
      ? { csrf_token: "cancel-csrf" }
      : { status: "ok", record: { status: "cancelled" }, events: [] }), {
      headers: { "Content-Type": "application/json" }, status: 200,
    });
  };
  const result = await cancelPrivilegedOperation("operation-1", "Superseded", "ui:cancel:retry");
  assert.equal(result.record.status, "cancelled");
  assert.equal(calls[0].input, "/v1/auth/session");
  assert.equal(calls[1].input, "/v1/privileged-operations/plans/operation-1/cancel");
  assert.equal(calls[1].init.method, "POST");
  assert.equal(calls[1].init.headers["X-CSRF-Token"], "cancel-csrf");
  assert.deepEqual(JSON.parse(calls[1].init.body), { reason: "Superseded", source_event_id: "ui:cancel:retry" });
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
        reviews: [],
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
        reviews: [],
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

test("privileged-operation UI scopes merge-train policy reads by descriptor", async () => {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ input: String(input), init });
    return new Response(
      JSON.stringify({
        status: "ok",
        trace_id: "trace-merge-train-policy-operations",
        total: 0,
        reviews: [],
      }),
      { headers: { "Content-Type": "application/json" }, status: 200 },
    );
  };

  await readPrivilegedOperationPlans(
    undefined,
    "managed-merge-train-policy-import",
  );

  assert.equal(
    calls[0].input,
    "/v1/privileged-operations/plans?descriptor_id=managed-merge-train-policy-import",
  );
  assert.equal(calls[0].init.method, "GET");
});

test("privileged-operation UI keeps semantic review and raw detail reads distinct", async () => {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ input: String(input), init });
    return new Response(
      JSON.stringify({
        status: "ok",
        trace_id: "trace-privileged-operation-detail",
        review: { operation_id: "operation-1" },
        record: {
          operation_id: "operation-1",
          evidence: { active_key_id: "raw" },
        },
        events: [],
      }),
      { headers: { "Content-Type": "application/json" }, status: 200 },
    );
  };

  await readPrivilegedOperationReview("operation-1");
  await readPrivilegedOperationRawDetail("operation-1");

  assert.equal(
    calls[0].input,
    "/v1/privileged-operations/plans/operation-1/review",
  );
  assert.equal(calls[0].init.method, "GET");
  assert.equal(calls[1].input, "/v1/privileged-operations/plans/operation-1");
  assert.equal(calls[1].init.method, "GET");
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


test("access-policy composer submits only the closed candidate intent and retry key", async () => {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ input: String(input), init });
    const payload = String(input).endsWith("/v1/auth/session")
      ? { csrf_token: "csrf-access-policy" }
      : { trace_id: "trace-access-policy", state: "already_satisfied" };
    return new Response(JSON.stringify(payload), {
      headers: { "Content-Type": "application/json" },
      status: 200,
    });
  };

  const response = await prepareAuthorizationCandidate(
    "ordinary-agent-delivery-administration",
    "remove",
    "ui:stable-access-policy-retry",
  );

  assert.equal(response.state, "already_satisfied");
  assert.equal(calls.length, 2);
  assert.equal(calls[1].input, "/v1/privileged-operations/authorization-candidates/prepare");
  assert.equal(calls[1].init.method, "POST");
  assert.equal(calls[1].init.headers["X-CSRF-Token"], "csrf-access-policy");
  assert.deepEqual(JSON.parse(String(calls[1].init.body)), {
    candidate_id: "ordinary-agent-delivery-administration",
    intent: "remove",
    source_event_id: "ui:stable-access-policy-retry",
  });
});

test("access-policy preparation accepts the isolated product-evidence candidate without extra fields", async () => {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ input: String(input), init });
    const payload = String(input).endsWith("/v1/auth/session")
      ? { csrf_token: "csrf-product-evidence" }
      : {
          trace_id: "trace-product-evidence",
          state: "planned",
          operation_id: "operation-product-evidence",
        };
    return new Response(JSON.stringify(payload), {
      headers: { "Content-Type": "application/json" },
      status: 200,
    });
  };

  const response = await prepareAuthorizationCandidate(
    "administrator-product-evidence-read",
    "add",
    "ui:product-evidence-add",
  );

  assert.equal(response.state, "planned");
  assert.equal(calls.length, 2);
  const body = JSON.parse(String(calls[1].init.body));
  assert.deepEqual(body, {
    candidate_id: "administrator-product-evidence-read",
    intent: "add",
    source_event_id: "ui:product-evidence-add",
  });
  assert.equal(calls[1].init.headers["X-CSRF-Token"], "csrf-product-evidence");
});

test("agent product setup preparation sends only the selected product ids", async () => {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ input: String(input), init });
    const payload = String(input).endsWith("/v1/auth/session")
      ? { csrf_token: "csrf-agent-product-setup" }
      : {
          trace_id: "trace-agent-product-setup",
          state: "planned",
          operation_id: "operation-agent-product-setup",
        };
    return new Response(JSON.stringify(payload), {
      headers: { "Content-Type": "application/json" },
      status: 200,
    });
  };

  const response = await prepareAuthorizationCandidate(
    "agent-product-setup",
    "add",
    "ui:agent-product-setup-add",
    undefined,
    ["example-shop", "example-docs"],
  );

  assert.equal(response.state, "planned");
  const body = JSON.parse(String(calls[1].init.body));
  assert.deepEqual(body, {
    candidate_id: "agent-product-setup",
    intent: "add",
    source_event_id: "ui:agent-product-setup-add",
    products: ["example-shop", "example-docs"],
  });
  assert.equal(Object.hasOwn(body, "subject"), false);
  assert.equal(Object.hasOwn(body, "token_label"), false);
});
