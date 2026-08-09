import assert from "node:assert/strict";
import test from "node:test";

import {
  engineeringPath,
  engineeringViewLabel,
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
});
