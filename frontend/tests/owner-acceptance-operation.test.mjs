import assert from "node:assert/strict";
import { test } from "node:test";

import {
  ownerAcceptanceOperationScope,
  ownerAcceptanceRequest,
} from "../src/owner-acceptance-operation.ts";

const binding = {
  repository: "example/site",
  pull_request_number: 42,
  product: "example-product",
  system: "website",
  action: "review",
  environment: "preview",
  binding_sha256: "a".repeat(64),
};

test("Owner acceptance request contains only the exact reviewed binding target", () => {
  assert.deepEqual(ownerAcceptanceRequest(binding, "changes_requested", "  Fix this.  "), {
    schema_version: 1,
    target: { repository: "example/site", pull_request_number: 42 },
    action: "changes_requested",
    expected_binding_sha256: "a".repeat(64),
    reason: "Fix this.",
  });
});

test("Owner acceptance operation scope changes with the binding digest", () => {
  assert.notEqual(
    ownerAcceptanceOperationScope(binding),
    ownerAcceptanceOperationScope({ ...binding, binding_sha256: "b".repeat(64) }),
  );
});
