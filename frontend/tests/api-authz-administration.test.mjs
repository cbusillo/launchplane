import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  buildAuthzManagedSetRollbackProposal,
  evaluateEffectiveAccess,
  explainAuthzDenial,
  exportActiveAuthzPolicy,
  planPrivilegedOperation,
  readAuthzPolicyAdministration,
  readAuthzPolicyRevisionHistory,
} from "../src/api.ts";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test("authorization administration reads use only bounded service routes", async () => {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    const path = String(input);
    calls.push({ input: path, init });
    const payload = path === "/v1/auth/session"
      ? { status: "ok", csrf_token: "csrf-read", identity: {}, trace_id: "trace-session" }
      : path.includes("/revisions")
      ? { status: "ok", trace_id: "trace-history", returned_count: 0, truncated: false, revisions: [] }
      : path.includes("/active/export")
        ? { status: "ok", trace_id: "trace-export", policy: {}, canonical_policy: {} }
        : path.includes("/denials/")
          ? { status: "ok", trace_id: "trace-denial" }
          : { status: "ok", trace_id: "trace-administration" };
    return new Response(JSON.stringify(payload), {
      headers: { "Content-Type": "application/json" },
      status: 200,
    });
  };

  await readAuthzPolicyAdministration();
  await readAuthzPolicyRevisionHistory();
  await exportActiveAuthzPolicy();
  await explainAuthzDenial("launchplane_req_example");

  assert.deepEqual(
    calls.map(({ input }) => input),
    [
      "/v1/auth/session",
      "/v1/authz-policies/administration",
      "/v1/auth/session",
      "/v1/authz-policies/revisions",
      "/v1/auth/session",
      "/v1/authz-policies/active/export",
      "/v1/authz-diagnostics/denials/launchplane_req_example",
    ],
  );
  assert.ok(calls.every(({ init }) => init.method === "GET"));
  const sensitiveReads = calls.filter(({ input }) => input.startsWith("/v1/authz-policies/"));
  assert.ok(
    sensitiveReads.every(({ init }) => init.headers["X-CSRF-Token"] === "csrf-read"),
  );
});

test("authorization administration mutations remain proposal-only", async () => {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    const path = String(input);
    calls.push({ input: path, init });
    if (path === "/v1/auth/session") {
      return new Response(
        JSON.stringify({ status: "ok", csrf_token: "csrf-test", identity: {}, trace_id: "trace-session" }),
        { headers: { "Content-Type": "application/json" }, status: 200 },
      );
    }
    return new Response(
      JSON.stringify({ status: "ok", trace_id: "trace-authz-operation", proposal: {}, record: {} }),
      { headers: { "Content-Type": "application/json" }, status: 200 },
    );
  };

  await evaluateEffectiveAccess({
    action: "example.read",
    product: "launchplane",
    context: "launchplane",
    target_scope: "context",
    principal: {
      principal_type: "github_human",
      login: "operator",
      github_id: 1,
      organizations: [],
      teams: [],
      role: "admin",
    },
  });
  await buildAuthzManagedSetRollbackProposal({
    target_revision: 1,
    managed_set_id: "owner.policy-admin",
    reason: "Reviewed rollback",
    source_event_id: "ui:test:rollback",
  });
  await planPrivilegedOperation({
    descriptor_id: "managed-authz-policy-set",
    source_event_id: "ui:test:proposal",
    request: {
      managed_set_id: "owner.policy-admin",
      desired_policy: {
        schema_version: 2,
        github_actions: [],
        github_humans: [],
        terminal_agents: [],
        local_operators: [],
        local_admins: [],
      },
      reason: "Reviewed proposal",
    },
  });

  const mutations = calls.filter(({ input }) => input !== "/v1/auth/session");
  assert.deepEqual(
    mutations.map(({ input }) => input),
    [
      "/v1/authz-diagnostics/effective-access/evaluate",
      "/v1/authz-policies/managed-rule-sets/rollback-proposal",
      "/v1/privileged-operations/plans",
    ],
  );
  assert.ok(mutations.every(({ init }) => init.method === "POST"));
  assert.ok(mutations.every(({ init }) => init.headers["X-CSRF-Token"] === "csrf-test"));
  assert.ok(!calls.some(({ input }) => /apply|execute/.test(input)));
});
