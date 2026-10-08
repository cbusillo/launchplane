import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { configureServiceGitHubDelivery, readServiceGitHubDelivery, retireServiceGitHubTokens } from "../src/api.ts";

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

test("service metadata reads and reviewed writes use session CSRF and the retained operation key", async () => {
  const calls = [];
  let csrf = 0;
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ path: String(input), init });
    return new Response(JSON.stringify(String(input) === "/v1/auth/session" ? { csrf_token: `csrf-${++csrf}` } : { status: "ok", trace_id: "trace" }), { headers: { "Content-Type": "application/json" } });
  };
  await readServiceGitHubDelivery();
  const selection = { mode: "apply", app_id: 76, integration: "existing-key", reason: "Select existing key", expected_plan_digest: "a".repeat(64) };
  await configureServiceGitHubDelivery(selection, { idempotencyKey: "selection-once" });
  const retirement = { mode: "apply", secret_ids: ["token-global"], reason: "Retire", expected_plan_digest: "b".repeat(64), director_confirmed: true,
    advisory_check_url: "https://github.com/example/site/actions/runs/11", delivery_comment_url: "https://github.com/example/site/pull/13#issuecomment-14",
    delivery_release_issue_url: "https://github.com/example/site/issues/15", consumer_check_evidence: "Service consumers checked" };
  await retireServiceGitHubTokens(retirement, { idempotencyKey: "retirement-once" });
  assert.deepEqual(calls.map(call => call.path), ["/v1/service/github-delivery", "/v1/auth/session", "/v1/service/github-delivery/configuration", "/v1/auth/session", "/v1/service/github-delivery/token-retirement"]);
  assert.equal(calls[0].init.method, "GET");
  for (const [index, body, key, token] of [[2, selection, "selection-once", "csrf-1"], [4, retirement, "retirement-once", "csrf-2"]]) {
    assert.equal(calls[index].init.method, "POST");
    assert.equal(calls[index].init.headers["X-CSRF-Token"], token);
    assert.equal(calls[index].init.headers["Idempotency-Key"], key);
    assert.deepEqual(JSON.parse(calls[index].init.body), body);
  }
});
