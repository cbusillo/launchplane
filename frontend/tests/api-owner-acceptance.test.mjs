import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { readOwnerAcceptanceQueue } from "../src/api.ts";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test("Owner acceptance queue sends repository and status filters to the server", async () => {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ input: String(input), init });
    return new Response(
      JSON.stringify({ status: "ok", trace_id: "trace-owner-acceptance", entries: [] }),
      { headers: { "Content-Type": "application/json" }, status: 200 },
    );
  };

  await readOwnerAcceptanceQueue({
    repository: "example/tenant site",
    status: "changes_requested",
  });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].init.method, "GET");
  assert.equal(
    calls[0].input,
    "/v1/owner-acceptance/queue?repository=example%2Ftenant+site&status=changes_requested",
  );
});
