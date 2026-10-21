import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { sweepPreviewLifecycle } from "../src/api.ts";

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

test("both sweep modes use the native session CSRF and original operation key", async () => {
  const calls = [];
  let csrf = 0;
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ path: String(input), init });
    return new Response(JSON.stringify(String(input) === "/v1/auth/session"
      ? { csrf_token: `csrf-${++csrf}` }
      : { trace_id: "sweep", status: "accepted", result: { status: "fail" } }),
      { headers: { "Content-Type": "application/json" } });
  };
  for (const apply of [false, true]) {
    const body = { source: "browser-fixture", apply };
    const result = await sweepPreviewLifecycle(body, { idempotencyKey: `sweep-${apply}` });
    assert.equal(result.result.status, "fail");
    const call = calls.at(-1);
    assert.equal(call.path, "/v1/previews/lifecycle-sweep");
    assert.equal(call.init.method, "POST");
    assert.equal(call.init.headers["X-CSRF-Token"], `csrf-${csrf}`);
    assert.equal(call.init.headers["Idempotency-Key"], `sweep-${apply}`);
    assert.deepEqual(JSON.parse(call.init.body), body);
  }
});
