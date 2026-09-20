import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { applyProductOwner, readProductProfile } from "../src/api.ts";
import {
  ownerLoginInputError,
  productOwnerDraftKey,
  productOwnerLabel,
  productOwnerPlanFromResponse,
  productOwnerPlanSummary,
} from "../src/product-owner-operation.ts";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    headers: { "Content-Type": "application/json" },
    status,
  });
}

test("owner login input accepts a pasted mention and rejects text GitHub cannot resolve", () => {
  assert.equal(ownerLoginInputError(" @site-owner "), "");
  assert.notEqual(ownerLoginInputError("   "), "");
  assert.notEqual(ownerLoginInputError("site owner"), "");
  assert.notEqual(ownerLoginInputError("owner/../orgs"), "");
});

test("a preview stops matching once the typed login changes, but not for case or a mention", () => {
  const previewed = productOwnerDraftKey("Site-Owner", false);
  assert.equal(productOwnerDraftKey("@site-owner ", false), previewed);
  assert.notEqual(productOwnerDraftKey("site-owner-2", false), previewed);
  assert.equal(productOwnerDraftKey("anything", true), productOwnerDraftKey("", true));
});

test("the preview shows the resolved login and id the server returned", () => {
  const plan = productOwnerPlanFromResponse({
    result: {
      changed: true,
      applied: false,
      owner_before: { github_login: "", github_id: "" },
      owner_after: { github_login: "Site-Owner", github_id: "4242" },
    },
  });
  assert.ok(plan);
  assert.equal(productOwnerLabel(plan.before), "No Owner set");
  assert.equal(productOwnerPlanSummary(plan), "Set the Owner to Site-Owner (id 4242).");
});

test("clearing and unchanged previews read plainly, and an unreadable result is refused", () => {
  const owner = { github_login: "Site-Owner", github_id: "4242" };
  const none = { github_login: "", github_id: "" };
  const cleared = productOwnerPlanFromResponse({
    result: { changed: true, owner_before: owner, owner_after: none },
  });
  const unchanged = productOwnerPlanFromResponse({
    result: { changed: false, owner_before: owner, owner_after: owner },
  });
  assert.equal(productOwnerPlanSummary(cleared), "Remove the Owner Site-Owner (id 4242).");
  assert.equal(
    productOwnerPlanSummary(unchanged),
    "No change. The Owner is already Site-Owner (id 4242).",
  );
  assert.equal(productOwnerPlanFromResponse({ result: null }), null);
  assert.equal(productOwnerPlanFromResponse({ result: { status: "ok" } }), null);
});

test("owner requests address the product in the path and carry the browser write headers", async () => {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ input: String(input), init });
    if (String(input) === "/v1/auth/session") {
      return jsonResponse({ csrf_token: "csrf-current" });
    }
    if (init.method === "GET") {
      return jsonResponse({ status: "ok", trace_id: "trace-read", profile: { owner: {} } });
    }
    return jsonResponse({ status: "accepted", trace_id: "trace-owner", records: {}, result: {} }, 202);
  };

  await readProductProfile("demo product");
  await applyProductOwner(
    "demo product",
    { mode: "apply", github_login: "site-owner", reason: "Name the Owner." },
    { idempotencyKey: "owner-key" },
  );

  assert.equal(calls[0].input, "/v1/product-profiles/demo%20product");
  const write = calls.at(-1);
  assert.equal(write.input, "/v1/product-profiles/demo%20product/owner");
  const headers = new Headers(write.init.headers);
  assert.equal(headers.get("X-CSRF-Token"), "csrf-current");
  assert.equal(headers.get("Idempotency-Key"), "owner-key");
  assert.equal("github_id" in JSON.parse(write.init.body), false);
});
