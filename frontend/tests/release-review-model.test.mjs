import assert from "node:assert/strict";
import test from "node:test";

import { groupReleaseItems, untestedReason } from "../src/release-review-model.ts";

function item(number, notes) {
  return {
    already_reviewed: false,
    head_sha: "a".repeat(40),
    merge_commit: "b".repeat(40),
    owner_test_notes: notes,
    pull_request_number: number,
    title: `Change ${number}`,
    url: `https://github.com/example/site/pull/${number}`,
  };
}

test("groups identical test notes, keeps missing notes separate, and collapses nothing-to-test changes", () => {
  const framework = "Click around the testing site and confirm pages load.";
  const grouped = groupReleaseItems([
    item(1, "Nothing for the owner to test. CI only."),
    item(2, framework),
    item(3, ""),
    item(4, `${framework}\n`),
    item(5, "nothing for the owner to test"),
    item(6, ""),
  ]);

  assert.deepEqual(
    grouped.checks.map(check => [check.notes, check.items.map(entry => entry.pull_request_number)]),
    [[framework, [2, 4]], ["", [3]], ["", [6]]],
  );
  assert.deepEqual(grouped.nothingToTest.map(entry => entry.pull_request_number), [1, 5]);
});

test("shows only the reason after the nothing-to-test marker", () => {
  assert.equal(untestedReason("Nothing for the owner to test. Automated dependency update."), "Automated dependency update.");
  assert.equal(untestedReason("Nothing for the owner to test"), "");
});
