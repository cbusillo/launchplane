import assert from "node:assert/strict";
import test from "node:test";

import {
  engineeringPath,
  engineeringViewLabel,
  ownerAcceptanceLookupFromSearch,
  parseAppRoute,
} from "../src/route-model.ts";

test("engineering hub and every child route deep-link exactly", () => {
  assert.deepEqual(parseAppRoute("/ui/engineering"), {
    kind: "engineering",
    view: "hub",
  });
  for (const view of [
    "work-graph",
    "issue-inbox",
    "every-code",
    "merge-train",
    "tenant-admission",
    "governance-projection",
    "owner-acceptance",
    "authorization-administration",
    "privileged-operations",
  ]) {
    assert.equal(engineeringPath(view), `/ui/engineering/${view}`);
    assert.deepEqual(parseAppRoute(engineeringPath(view)), {
      kind: "engineering",
      view,
    });
  }
});

test("unknown or nested engineering paths fail closed", () => {
  assert.deepEqual(parseAppRoute("/ui/engineering/run-once"), {
    kind: "not-found",
    path: "/ui/engineering/run-once",
  });
  assert.deepEqual(parseAppRoute("/ui/engineering/merge-train/run-once"), {
    kind: "not-found",
    path: "/ui/engineering/merge-train/run-once",
  });
});

test("engineering labels are route-specific", () => {
  assert.equal(engineeringViewLabel("hub"), "Engineering Ops");
  assert.equal(engineeringViewLabel("issue-inbox"), "Issue inbox");
  assert.equal(engineeringViewLabel("every-code"), "Every Code");
  assert.equal(engineeringViewLabel("tenant-admission"), "Tenant admission");
  assert.equal(engineeringViewLabel("owner-acceptance"), "Owner product review");
  assert.equal(engineeringViewLabel("authorization-administration"), "Authorization");
  assert.equal(engineeringViewLabel("governance-projection"), "Governance evidence");
  assert.equal(
    engineeringViewLabel("privileged-operations"),
    "Privileged operation plans",
  );
});

test("Owner acceptance deep-link query selects one exact lookup", () => {
  assert.deepEqual(
    ownerAcceptanceLookupFromSearch(
      "fixture=products&repository=example%2Fcontrol-plane&pull_request=308",
    ),
    {
      repository: "example/control-plane",
      pullRequest: "308",
      requested: true,
      valid: true,
    },
  );
  assert.equal(
    ownerAcceptanceLookupFromSearch("fixture=products&repository=example%2Fcontrol-plane").valid,
    false,
  );
});
