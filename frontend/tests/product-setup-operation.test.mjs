import assert from "node:assert/strict";
import { test, afterEach } from "node:test";
import { declarationSetupRequest, environmentSetupRequest, reviewedSetupApply, verifySetupReadback } from "../src/product-setup-operation.ts";
import { applyProductExpectedConfig, applyProductStableLaneRepair } from "../src/api.ts";
import { BROWSER_WRITE_ROUTES } from "../src/browser-write-contract.ts";
const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

test("setup confines declarations to the selected non-production lane and rejects production", () => {
  const lane = { context: "demo", instance: "testing" };
  const request = declarationSetupRequest("demo", lane, [{ binding_key: "BOT_TOKEN", integration: "runtime_environment" }], "Isolated testing");
  assert.equal(request.managed_secret_bindings[0].context, lane.context);
  assert.equal(request.managed_secret_bindings[0].instance, lane.instance);
  assert.throws(() => declarationSetupRequest("demo", { ...lane, instance: "prod" }, [], "reason"));
  assert.throws(() => environmentSetupRequest("demo", "demo", "production", "https://testing.example.com", "reason"));
  assert.throws(() => environmentSetupRequest("demo", "demo", "testing", "https://password@testing.example.com", "reason"));
});

test("environment apply carries the service-reviewed digest and rejects malformed or mismatched plans", () => {
  const request = environmentSetupRequest("demo", "demo", "testing", "https://testing.example.com/", "reason");
  const result = { status: "ok", mode: "dry-run", product: "demo", context: "demo", instance: "testing", changed: true, base_url: request.base_url, plan_sha256: "a".repeat(64) };
  assert.equal(reviewedSetupApply("environment", request, { result }).reviewed_plan_sha256, result.plan_sha256);
  for (const change of [{ instance: "prod" }, { plan_sha256: "" }, { mode: "apply" }, { product: "other" }]) assert.throws(() => reviewedSetupApply("environment", request, { result: { ...result, ...change } }));
});

test("setup API uses current CSRF and caller operation keys without bearer credentials", async () => {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return new Response(JSON.stringify(url === "/v1/auth/session" ? { csrf_token: "current-csrf" } : { status: "accepted", trace_id: "setup", result: {} }), { status: url === "/v1/auth/session" ? 200 : 202 });
  };
  const environment = environmentSetupRequest("demo", "demo", "testing", "https://testing.example.com", "reason");
  await applyProductStableLaneRepair(environment, { idempotencyKey: "environment-key" });
  const declarations = declarationSetupRequest("demo", { context: "demo", instance: "testing" }, [{ binding_key: "BOT_TOKEN", integration: "runtime_environment" }], "reason");
  await applyProductExpectedConfig(declarations, { idempotencyKey: "declaration-key" });
  const writes = calls.filter(call => call.init.method === "POST");
  assert.equal(writes.length, 2);
  assert.equal(writes[0].url, BROWSER_WRITE_ROUTES.productStableLaneRepairApply);
  assert.equal(writes[1].url, BROWSER_WRITE_ROUTES.productExpectedConfigApply);
  for (const [index, key] of ["environment-key", "declaration-key"].entries()) {
    const headers = new Headers(writes[index].init.headers);
    assert.equal(headers.get("X-CSRF-Token"), "current-csrf");
    assert.equal(headers.get("Idempotency-Key"), key);
    assert.equal(headers.has("Authorization"), false);
  }
});


test("a no-website product plans and verifies a private environment without a public origin", () => {
  const request = environmentSetupRequest("demo", "demo", "testing", "", "Isolated private runtime", "none");
  assert.equal(request.base_url, "");
  const result = { status: "ok", mode: "dry-run", product: request.product, context: request.context, instance: request.instance, changed: true, base_url: "", plan_sha256: "b".repeat(64) };
  const apply = reviewedSetupApply("environment", request, { result });
  assert.equal(apply.base_url, "");
  assert.doesNotThrow(() => verifySetupReadback("environment", apply, { product: request.product, lanes: [{ context: request.context, instance: request.instance, base_url: "" }] }));
  assert.throws(() => environmentSetupRequest("demo", "demo", "testing", "https://testing.example.com", "reason", "none"));
  assert.throws(() => environmentSetupRequest("demo", "demo", "testing", "", "reason"));
});
