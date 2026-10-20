import assert from "node:assert/strict";
import test from "node:test";
import { readProviderHoldAttention, LaunchplaneApiError } from "../src/api.ts";
import { providerAttentionQualifier } from "../src/provider-hold-attention.ts";

const now = Date.parse("2026-10-01T00:00:00Z");
const page = { items: [], has_more: false, next_cursor: null, completeness: "single_page", observed_at: new Date(now).toISOString(), stale_after: new Date(now + 60_000).toISOString() };
function resource(data = page, status = "ready") { return { data, status, statusCode: 200, error: "", traceId: "" }; }

test("service attention GET encodes bounded continuation and preserves denial evidence", async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (input, init) => { calls.push({ input, init }); return new Response(JSON.stringify({ status: "ok", attention: page }), { status: 200 }); };
  try {
    const controller = new AbortController();
    await readProviderHoldAttention({ limit: 7, cursor: "v1:opaque:ref" }, controller.signal);
    assert.equal(calls[0].input, "/v1/operations/provider-hold-attention?limit=7&cursor=v1%3Aopaque%3Aref");
    assert.equal(calls[0].init.method, "GET");
    assert.equal(calls[0].init.credentials, "same-origin");
    assert.equal(calls[0].init.signal, controller.signal);
    assert.equal(calls[0].init.body, undefined);
    globalThis.fetch = async () => new Response(JSON.stringify({ error: { code: "authorization_denied", message: "denied" }, trace_id: "denied-trace" }), { status: 403 });
    await assert.rejects(readProviderHoldAttention(), error => error instanceof LaunchplaneApiError && error.statusCode === 403 && error.traceId === "denied-trace");
  } finally { globalThis.fetch = original; }
});

test("only a fresh first single page can describe an empty service read", () => {
  assert.match(providerAttentionQualifier(resource(), now), /No retained service operations/);
  for (const state of [resource(page, "loading"), resource(page, "error"), resource({ ...page, completeness: "partial" }), resource({ ...page, has_more: true }), resource({ ...page, stale_after: "invalid" }), resource({ ...page, stale_after: new Date(now).toISOString() })]) {
    assert.doesNotMatch(providerAttentionQualifier(state, now), /No retained service operations/);
  }
  assert.match(providerAttentionQualifier(resource({ ...page, items: [{ original_scope: null, attribution: "unavailable", state: "running" }] }), now), /unknown original ownership/);
  assert.match(providerAttentionQualifier(resource({ ...page, items: [{ original_scope: { product: "fixture" }, attribution: "retained", state: "running" }] }), now), /Retained operations/);
});
