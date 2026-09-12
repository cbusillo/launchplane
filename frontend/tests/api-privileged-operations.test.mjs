import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  approvePrivilegedOperation,
  planOrdinaryAgentDeliveryActivation,
  prepareAuthorizationCandidate,
  readOrdinaryAgentDeliveryAuthorizationCandidateInputs,
  readOrdinaryAgentDeliveryActivationOptions,
  readPrivilegedOperationRawDetail,
  readPrivilegedOperationPlans,
  readPrivilegedOperationReview,
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

test("setup-prerequisite check performs one parameterless read", async () => {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ input: String(input), init });
    return new Response(
      JSON.stringify({
        status: "ok",
        schema_version: 1,
        trace_id: "trace-preparation-inputs",
        observed_at: "2026-09-12T14:32:00Z",
        authorization_policy: {
          record_id: "authorization-policy-r7",
          revision: 7,
          schema_version: 2,
          policy_sha256: "1".repeat(64),
        },
        inventory_state: "complete",
        merge_policy_state: "available",
        merge_policy: {
          record_id: "merge-train-policy-r4",
          policy_sha256: "2".repeat(64),
          updated_at: "2026-09-12T14:25:00Z",
        },
        repositories: [],
        diagnostics: [],
      }),
      { headers: { "Content-Type": "application/json" }, status: 200 },
    );
  };

  const response =
    await readOrdinaryAgentDeliveryAuthorizationCandidateInputs();

  assert.equal(calls.length, 1);
  assert.equal(
    calls[0].input,
    "/v1/privileged-operations/authorization-candidates/ordinary-agent-delivery/inputs",
  );
  assert.equal(calls[0].init.method, "GET");
  assert.equal(calls[0].init.body, undefined);
  assert.equal(response.inventory_state, "complete");
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

test("activation UI reads server choices and submits their opaque references", async () => {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ input: String(input), init });
    return new Response(
      JSON.stringify({
        status: "ok",
        trace_id: "trace-activation",
        csrf_token: "csrf",
        duration_options: [],
        setup_options: [],
        revoke_options: [],
        write_status: "written",
        record: {},
        events: [],
      }),
      { headers: { "Content-Type": "application/json" }, status: 200 },
    );
  };

  await readOrdinaryAgentDeliveryActivationOptions();
  await planOrdinaryAgentDeliveryActivation({
    schema_version: 1,
    action: "setup",
    policy_operation_id: "server-policy-operation",
    repository_inventory_record_id: "server-inventory-record",
    predecessor: null,
    activation_expires_at: "2026-09-11T12:00:00Z",
    reason: "Prepare qualification-only delivery for the selected target.",
  });

  assert.equal(
    calls[0].input,
    "/v1/privileged-operations/ordinary-agent-delivery-activation/options",
  );
  const planCall = calls.find(({ input }) =>
    input.endsWith(
      "/v1/privileged-operations/ordinary-agent-delivery-activation/plans",
    ),
  );
  assert.ok(planCall);
  assert.equal(planCall.init.method, "POST");
  const body = JSON.parse(String(planCall.init.body));
  assert.equal(body.request.policy_operation_id, "server-policy-operation");
  assert.equal(
    body.request.repository_inventory_record_id,
    "server-inventory-record",
  );
  assert.equal(body.request.predecessor, null);
  assert.equal(body.request.activation_expires_at, "2026-09-11T12:00:00Z");
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
