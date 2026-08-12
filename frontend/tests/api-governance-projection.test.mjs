import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { readGovernanceProjection } from "../src/api.ts";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test("Governance projection reads one bounded repository and PR scope", async () => {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ input: String(input), init });
    return new Response(
      JSON.stringify({ status: "ok", trace_id: "trace-governance", projection: {} }),
      { headers: { "Content-Type": "application/json" }, status: 200 },
    );
  };

  await readGovernanceProjection("example/tenant site", 42, "release/main");

  assert.equal(calls.length, 1);
  assert.equal(calls[0].init.method, "GET");
  assert.equal(
    calls[0].input,
    "/v1/governance/projection?repository=example%2Ftenant+site&pull_request_number=42&base_branch=release%2Fmain",
  );
});
