import assert from "node:assert/strict";
import { test } from "node:test";
import { checkBrowserShardCoverage } from "../scripts/check-browser-smoke-shards.mjs";

function report(ids, current, total = 2) {
  return {
    config: { shard: { current, total } }, errors: [],
    suites: [{ specs: ids.map(id => ({ id, title: id, file: "journey.spec.ts", tests: [{
      projectId: "desktop", expectedStatus: "passed", results: [{ status: "passed", duration: 10, retry: 0 }],
    }] })) }],
  };
}

test("all discovered tests must pass exactly once across shards", () => {
  const inventory = report(["first", "second", "third"]);
  const result = checkBrowserShardCoverage(inventory, [report(["first"], 1), report(["second", "third"], 2)]);
  assert.equal(result.tests, 3);
  assert.equal(result.modules.get("journey.spec.ts"), 30);
});

test("a missing test cannot be replaced by another copy of a passing test", () => {
  const inventory = report(["first", "second"]);
  assert.throws(() => checkBrowserShardCoverage(inventory, [report(["first"], 1), report(["first"], 2)]), /repeated/);
  assert.throws(() => checkBrowserShardCoverage(inventory, [report(["first"], 1)]), /identity/);
  assert.throws(() => checkBrowserShardCoverage(inventory, [report(["first"], 1, 1)]), /Missing/);
});

test("failed, skipped, absent and retried results never qualify coverage", () => {
  for (const results of [[], [{ status: "failed" }], [{ status: "skipped" }], [{ status: "passed", retry: 1 }],
    [{ status: "failed" }, { status: "passed" }]]) {
    const shard = report(["first"], 1, 1);
    shard.suites[0].specs[0].tests[0].results = results;
    assert.throws(() => checkBrowserShardCoverage(report(["first"]), [shard]), /did not pass once/);
  }
});

test("test errors, expected failures and invalid shard identities fail closed", () => {
  const inventory = report(["first"]);
  const shard = report(["first"], 1, 1);
  assert.throws(() => checkBrowserShardCoverage(inventory, [report(["first"], 2, 1)]), /identity/);
  shard.errors.push({ message: "worker teardown failed" });
  assert.throws(() => checkBrowserShardCoverage(inventory, [shard]), /errors/);
  shard.errors = [];
  shard.suites[0].specs[0].tests[0].expectedStatus = "failed";
  assert.throws(() => checkBrowserShardCoverage(inventory, [shard]), /did not pass once/);
});

test("empty and unexpected inventories or duplicate tests fail closed", () => {
  assert.throws(() => checkBrowserShardCoverage(report([]), []), /empty/);
  assert.throws(() => checkBrowserShardCoverage(report(["first"]), [report([], 1, 1)]), /empty/);
  assert.throws(() => checkBrowserShardCoverage(report(["first"]), [report(["other"], 1, 1)]), /Unexpected/);
  assert.throws(() => checkBrowserShardCoverage(report(["first"]), [report(["first", "first"], 1, 1)]), /duplicate/);
});
