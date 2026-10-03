import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { applyProductImageRepository, applyProductProductionUse } from "../src/api.ts";
const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });
for (const [apply, suffix, body] of [
  [applyProductImageRepository, "image-repository", { mode: "apply", image_repository: "ghcr.io/example/site", expected_image_repository: "ghcr.io/example/old", reason: "Move package" }],
  [applyProductProductionUse, "production-use", { mode: "apply", production_use: "live", reviewed_plan_sha256: "a".repeat(64), reason: "Classify use" }],
]) {
  test(`${suffix} keeps reviewed binding, CSRF and operation key`, async () => {
    const calls = [];
    globalThis.fetch = async (url, init = {}) => {
      calls.push({ url, init });
      return new Response(JSON.stringify(String(url) === "/v1/auth/session" ? { csrf_token: "csrf" } : { status: "accepted", trace_id: "trace", result: {}, records: {} }), { headers: { "Content-Type": "application/json" } });
    };
    await apply("site name", body, { idempotencyKey: "operation-key" });
    const write = calls.find(call => call.init.method === "POST");
    assert.equal(write.url, `/v1/product-profiles/site%20name/${suffix}`);
    assert.deepEqual(JSON.parse(write.init.body), body);
    assert.equal(new Headers(write.init.headers).get("X-CSRF-Token"), "csrf");
    assert.equal(new Headers(write.init.headers).get("Idempotency-Key"), "operation-key");
    assert.equal(write.init.credentials, "same-origin");
  });
}
