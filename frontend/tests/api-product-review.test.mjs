import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  readProductReview,
  writeProductReviewDecision,
} from "../src/api.ts";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test("Owner decision is sent with the session CSRF token to the product-review route", async () => {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ input: String(input), init });
    if (String(input) === "/v1/auth/session") {
      return new Response(JSON.stringify({ csrf_token: "csrf-owner" }), {
        headers: { "Content-Type": "application/json" },
        status: 200,
      });
    }
    return new Response(JSON.stringify({ status: "ok", trace_id: "trace-review" }), {
      headers: { "Content-Type": "application/json" },
      status: 200,
    });
  };

  await readProductReview("example/tenant-site", 42);
  await writeProductReviewDecision({
    repository: "example/tenant-site",
    pull_request: 42,
    decision: "changes_requested",
    reason: "The price is wrong.",
  });

  assert.deepEqual(
    calls.map((call) => call.input),
    [
      "/v1/product-review?repository=example%2Ftenant-site&pull_request=42",
      "/v1/auth/session",
      "/v1/product-review/decisions",
    ],
  );
  assert.equal(calls[2].init.method, "POST");
  assert.equal(calls[2].init.headers["X-CSRF-Token"], "csrf-owner");
  assert.deepEqual(JSON.parse(calls[2].init.body), {
    repository: "example/tenant-site",
    pull_request: 42,
    decision: "changes_requested",
    reason: "The price is wrong.",
  });
});
