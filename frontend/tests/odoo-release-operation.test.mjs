import assert from "node:assert/strict";
import test from "node:test";

import { LaunchplaneApiError } from "../src/api.ts";
import {
  ODOO_PROMOTION_BACKUP_ACTION,
  readOdooReleaseAttempt,
  runOdooRelease,
  writeOdooReleaseAttempt,
} from "../src/odoo-release-operation.ts";

const scope = { context: "site", environment: "prod", product: "site-product" };
const attempt = { idempotencyKey: "ui-odoo-release-key", requestId: "ui-request" };

function review(overrides = {}) {
  return {
    can_override: false,
    display_name: "Site",
    owner_github_login: "owner",
    product: "site-product",
    review: {
      approved: true,
      blockers: [],
      checklist: null,
      checklist_digest: "",
      latest_decision: null,
      required: true,
      unavailable_reason: null,
      ...overrides,
    },
    trace_id: "trace-review",
    viewer_is_owner: false,
  };
}

function backup(status, overrides = {}) {
  return {
    backup_record_id: "infrastructure-site-ui-request",
    error_code: "",
    evidence: {},
    operation_id: "production-backup-gate-1",
    operation_status: status,
    status: "accepted",
    trace_id: `trace-backup-${status}`,
    ...overrides,
  };
}

function recordingDependencies({ reviewResponse = review(), backups, enqueueError, promoteResult }) {
  const calls = [];
  const queue = [...backups];
  return {
    calls,
    dependencies: {
      readReleaseReview: async (product) => {
        calls.push(["review", product]);
        return reviewResponse;
      },
      enqueueBackup: async (payload, options) => {
        calls.push(["enqueue", payload, options.idempotencyKey]);
        if (enqueueError) throw enqueueError;
        return queue.shift();
      },
      readBackup: async (operationId, backupScope) => {
        calls.push(["poll", operationId, backupScope]);
        return queue.shift();
      },
      wait: async () => {
        calls.push(["wait"]);
      },
      promote: async (payload, options) => {
        calls.push(["promote", payload, options.idempotencyKey]);
        return {
          original_trace_id: null,
          records: {},
          replayed: false,
          result: promoteResult ?? { run_status: "pass", artifact_id: "artifact-new" },
          status: "accepted",
          trace_id: "trace-promote",
        };
      },
    },
  };
}

async function run(dependencies) {
  const progress = [];
  const outcome = await runOdooRelease({
    attempt,
    dependencies,
    onProgress: (update) => progress.push(`${update.step}:${update.state}`),
    pollIntervalMilliseconds: 0,
    scope,
  });
  return { outcome, progress };
}

test("release runs review, then backup to a terminal state, then promote with that record", async () => {
  const { calls, dependencies } = recordingDependencies({
    backups: [backup("pending"), backup("running"), backup("pass")],
  });

  const { outcome, progress } = await run(dependencies);

  assert.equal(outcome.status, "promoted");
  assert.deepEqual(
    calls.map(([name]) => name),
    ["review", "enqueue", "wait", "poll", "wait", "poll", "promote"],
  );
  const [, backupPayload, backupKey] = calls[1];
  assert.equal(backupPayload.promotion_action, ODOO_PROMOTION_BACKUP_ACTION);
  assert.equal(backupPayload.backup_record_id, "infrastructure-site-ui-request");
  assert.equal(backupKey, "infrastructure-ui-odoo-release-key");
  const [, promotePayload, promoteKey] = calls.at(-1);
  assert.equal(promotePayload.run.infrastructure_backup_record_id, "infrastructure-site-ui-request");
  assert.equal(promotePayload.run.request_id, "ui-request");
  assert.equal(promoteKey, "ui-odoo-release-key");
  assert.deepEqual(progress.filter((entry) => entry.endsWith(":passed")), [
    "review:passed",
    "backup:passed",
    "promote:passed",
  ]);
});

test("an unapproved release stops before any backup", async () => {
  const { calls, dependencies } = recordingDependencies({
    reviewResponse: review({ approved: false, blockers: ["Owner has not approved."] }),
    backups: [],
  });

  const { outcome } = await run(dependencies);

  assert.equal(outcome.status, "stopped");
  assert.equal(outcome.step, "review");
  assert.match(outcome.failure.message, /Owner has not approved/);
  assert.deepEqual(calls.map(([name]) => name), ["review"]);
});

test("a backup authorization refusal stops the release before promote", async () => {
  const { calls, dependencies } = recordingDependencies({
    backups: [],
    enqueueError: new LaunchplaneApiError(
      "Durable backup authorization is unavailable.",
      503,
      "trace-refused",
      "authorization_provenance_unavailable",
    ),
  });

  const { outcome } = await run(dependencies);

  assert.equal(outcome.status, "stopped");
  assert.equal(outcome.step, "backup");
  assert.equal(outcome.certainty, "definitive");
  assert.equal(outcome.failure.code, "authorization_provenance_unavailable");
  assert.match(outcome.failure.message, /managed authorization rule/);
  assert.ok(!calls.some(([name]) => name === "promote"));
});

test("a failed backup stops the release before promote", async () => {
  const { calls, dependencies } = recordingDependencies({
    backups: [backup("running"), backup("fail", { error_code: "backup_failed" })],
  });

  const { outcome } = await run(dependencies);

  assert.equal(outcome.status, "stopped");
  assert.equal(outcome.step, "backup");
  assert.equal(outcome.failure.code, "backup_failed");
  assert.ok(!calls.some(([name]) => name === "promote"));
});

test("a server error while promoting leaves the attempt uncertain for a same-key retry", async () => {
  const { dependencies } = recordingDependencies({ backups: [backup("pass")] });
  dependencies.promote = async () => {
    throw new LaunchplaneApiError("Bad gateway", 502, "trace-502", "request_failed");
  };

  const { outcome } = await run(dependencies);

  assert.equal(outcome.status, "stopped");
  assert.equal(outcome.step, "promote");
  assert.equal(outcome.certainty, "uncertain");
});

test("a stored attempt survives a reload and clears after a final answer", () => {
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    removeItem: (key) => values.delete(key),
    setItem: (key, value) => values.set(key, value),
  };

  writeOdooReleaseAttempt(scope, attempt, storage);
  assert.deepEqual(readOdooReleaseAttempt(scope, storage), attempt);
  writeOdooReleaseAttempt(scope, null, storage);
  assert.equal(readOdooReleaseAttempt(scope, storage), null);
});
