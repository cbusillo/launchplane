import assert from "node:assert/strict";
import { test } from "node:test";

import { LaunchplaneApiError } from "../src/api.ts";
import {
  ownerAcceptanceFailureCertainty,
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
    resolution: null,
  });
});

test("Owner acceptance resolution stays structured and binding scoped", () => {
  const resolution = {
    schema_version: 1,
    summary: "The requested behavior is covered.",
    resolved_evidence_references: ["test:owner-flow"],
  };
  assert.deepEqual(ownerAcceptanceRequest(binding, "accepted", "", resolution), {
    schema_version: 1,
    target: { repository: "example/site", pull_request_number: 42 },
    action: "accepted",
    expected_binding_sha256: "a".repeat(64),
    reason: "",
    resolution,
  });
});

test("Owner acceptance operation scope changes with the binding digest", () => {
  assert.notEqual(
    ownerAcceptanceOperationScope(binding),
    ownerAcceptanceOperationScope({ ...binding, binding_sha256: "b".repeat(64) }),
  );
});

test("Owner and Engineering bindings keep the deployed operation scope", () => {
  const ownerBinding = {
    repository: binding.repository,
    pull_request_number: binding.pull_request_number,
    product: binding.product,
    system: binding.system,
    action: binding.action,
    environment: binding.environment,
    binding_sha256: binding.binding_sha256,
  };

  assert.equal(
    ownerAcceptanceOperationScope(ownerBinding),
    ownerAcceptanceOperationScope(binding),
  );
  assert.equal(
    ownerAcceptanceOperationScope(ownerBinding),
    `owner-acceptance:example/site:42:example-product:website:review:preview:${"a".repeat(64)}`,
  );
});

test("Owner acceptance keeps post-dispatch server failures uncertain", () => {
  assert.equal(
    ownerAcceptanceFailureCertainty(
      new LaunchplaneApiError("projection unavailable", 503),
      true,
    ),
    "uncertain",
  );
  assert.equal(
    ownerAcceptanceFailureCertainty(
      new LaunchplaneApiError("binding changed", 409),
      true,
    ),
    "definitive",
  );
});
