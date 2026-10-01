import assert from "node:assert/strict";
import test from "node:test";

import { LaunchplaneApiError } from "../src/api.ts";
import {
  ODOO_PROMOTION_BACKUP_ACTION,
  readOdooReleaseAttempt,
  readOdooRollbackAttempt,
  runOdooRelease,
  runOdooRollback,
  writeOdooReleaseAttempt,
  writeOdooRollbackAttempt,
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

function promotion(status, phase, overrides = {}) {
  return {
    operation: {
      attempt: 1,
      context: "site",
      created_at: "2026-09-30T00:00:00Z",
      error_code: "",
      error_message: "",
      finished_at: "",
      instance: "prod",
      operation_id: "odoo-prod-promotion-1",
      phase,
      product: "site-product",
      request_id: "ui-request",
      result:
        status === "pass"
          ? {
              artifact_id: "artifact-new",
              context: "site",
              deployment_record_id: "deployment-site-prod",
              from_instance: "testing",
              infrastructure_backup_record_id: "infrastructure-site-ui-request",
              input_status: "ready",
              request_id: "ui-request",
              run_status: "pass",
              to_instance: "prod",
            }
          : null,
      started_at: "",
      status,
      updated_at: "2026-09-30T00:00:00Z",
      ...overrides,
    },
    status: "accepted",
    trace_id: `trace-promotion-${status}`,
  };
}

function recordingDependencies({
  reviewResponse = review(),
  backups,
  enqueueError,
  promotions = [promotion("pending", "created"), promotion("pass", "completed")],
}) {
  const calls = [];
  const queue = [...backups];
  const promotionQueue = [...promotions];
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
      enqueuePromotion: async (payload, options) => {
        calls.push(["promote", payload, options.idempotencyKey]);
        return promotionQueue.shift();
      },
      readPromotion: async (operationId, promotionScope) => {
        calls.push(["watch", operationId, promotionScope]);
        return promotionQueue.shift();
      },
    },
  };
}

async function run(dependencies, releaseAttempt = attempt) {
  const progress = [];
  const queued = [];
  const outcome = await runOdooRelease({
    attempt: releaseAttempt,
    dependencies,
    onProgress: (update) => progress.push(`${update.step}:${update.state}`),
    onPromotionQueued: (operationId) => queued.push(operationId),
    pollIntervalMilliseconds: 0,
    scope,
  });
  return { outcome, progress, queued };
}

test("release runs review, then backup to a terminal state, then queues and watches the promotion", async () => {
  const { calls, dependencies } = recordingDependencies({
    backups: [backup("pending"), backup("running"), backup("pass")],
  });

  const { outcome, progress, queued } = await run(dependencies);

  assert.equal(outcome.status, "promoted");
  assert.equal(outcome.result.deployment_record_id, "deployment-site-prod");
  assert.deepEqual(queued, ["odoo-prod-promotion-1"]);
  assert.deepEqual(
    calls.map(([name]) => name),
    ["review", "enqueue", "wait", "poll", "wait", "poll", "promote", "wait", "watch"],
  );
  const [, backupPayload, backupKey] = calls[1];
  assert.equal(backupPayload.promotion_action, ODOO_PROMOTION_BACKUP_ACTION);
  assert.equal(backupPayload.backup_record_id, "infrastructure-site-ui-request");
  assert.equal(backupKey, "infrastructure-ui-odoo-release-key");
  const [, promotePayload, promoteKey] = calls.find(([name]) => name === "promote");
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
  assert.match(outcome.failure.message, /policy administrator, or to have exactly one managed rule/);
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

test("a server error while queueing the promotion leaves the attempt uncertain for a same-key retry", async () => {
  const { dependencies } = recordingDependencies({ backups: [backup("pass")] });
  dependencies.enqueuePromotion = async () => {
    throw new LaunchplaneApiError("Bad gateway", 502, "trace-502", "request_failed");
  };

  const { outcome } = await run(dependencies);

  assert.equal(outcome.status, "stopped");
  assert.equal(outcome.step, "promote");
  assert.equal(outcome.certainty, "uncertain");
});

test("a queued promotion resumes by watching it, without a new review, backup, or enqueue", async () => {
  const { calls, dependencies } = recordingDependencies({
    backups: [],
    promotions: [promotion("running", "promotion_started"), promotion("pass", "completed")],
  });

  const { outcome, progress } = await run(dependencies, {
    ...attempt,
    promotionOperationId: "odoo-prod-promotion-1",
  });

  assert.equal(outcome.status, "promoted");
  assert.deepEqual(calls.map(([name]) => name), ["watch", "wait", "watch"]);
  assert.deepEqual(calls[0], [
    "watch",
    "odoo-prod-promotion-1",
    { context: "site", product: "site-product" },
  ]);
  assert.ok(progress.includes("promote:running"));
});

test("a promotion the worker stopped mid-way asks the operator to check prod", async () => {
  const { dependencies } = recordingDependencies({
    backups: [backup("pass")],
    promotions: [
      promotion("running", "logical_backup_started"),
      promotion("reconciliation_required", "logical_backup_started", {
        error_code: "operation_reconciliation_required",
      }),
    ],
  });

  const { outcome } = await run(dependencies);

  assert.equal(outcome.status, "stopped");
  assert.equal(outcome.certainty, "definitive");
  assert.equal(outcome.failure.code, "operation_reconciliation_required");
  assert.match(outcome.failure.message, /Check the latest prod deployment/);
});

test("a failed promotion reports the server's reason", async () => {
  const { dependencies } = recordingDependencies({
    backups: [backup("pass")],
    promotions: [
      promotion("fail", "failed", {
        error_code: "promotion_blocked",
        error_message: "Release approval was withdrawn.",
      }),
    ],
  });

  const { outcome } = await run(dependencies);

  assert.equal(outcome.status, "stopped");
  assert.equal(outcome.failure.code, "promotion_blocked");
  assert.equal(outcome.failure.message, "Release approval was withdrawn.");
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
  const queued = { ...attempt, promotionOperationId: "odoo-prod-promotion-1" };
  writeOdooReleaseAttempt(scope, queued, storage);
  assert.deepEqual(readOdooReleaseAttempt(scope, storage), queued);
  writeOdooReleaseAttempt(scope, null, storage);
  assert.equal(readOdooReleaseAttempt(scope, storage), null);
});

function rollbackOperation(status, phase, overrides = {}) {
  return {
    operation: {
      attempt: 1,
      context: "site",
      created_at: "2026-09-30T00:00:00Z",
      error_code: "",
      error_message: "",
      finished_at: "",
      instance: "prod",
      operation_id: "odoo-prod-rollback-1",
      phase,
      product: "site-product",
      reason: "Drill",
      result: null,
      started_at: "",
      status,
      target_artifact_id: "artifact-previous",
      target_deployment_record_id: "deployment-previous",
      updated_at: "2026-09-30T00:00:00Z",
      ...overrides,
    },
    status: "accepted",
    trace_id: `trace-rollback-${status}`,
  };
}

function rollbackDependencies(responses, enqueueError) {
  const calls = [];
  const queue = [...responses];
  return {
    calls,
    dependencies: {
      enqueueRollback: async (payload, options) => {
        calls.push(["enqueue", payload, options.idempotencyKey]);
        if (enqueueError) throw enqueueError;
        return queue.shift();
      },
      readRollback: async (operationId, rollbackScope) => {
        calls.push(["watch", operationId, rollbackScope]);
        return queue.shift();
      },
      wait: async () => {
        calls.push(["wait"]);
      },
    },
  };
}

const rollbackAttempt = { idempotencyKey: "ui-odoo-rollback-key", reason: "Drill" };

async function rollBack(dependencies, attemptValue = rollbackAttempt) {
  const seen = [];
  const queued = [];
  const outcome = await runOdooRollback({
    attempt: attemptValue,
    dependencies,
    onOperation: (operation) => seen.push(`${operation.status}:${operation.target_artifact_id}`),
    onQueued: (operationId) => queued.push(operationId),
    pollIntervalMilliseconds: 0,
    scope,
  });
  return { outcome, queued, seen };
}

test("a rollback queues once, shows its fixed target, and watches it to the end", async () => {
  const { calls, dependencies } = rollbackDependencies([
    rollbackOperation("pending", "created"),
    rollbackOperation("running", "rollback_started"),
    rollbackOperation("pass", "completed"),
  ]);

  const { outcome, queued, seen } = await rollBack(dependencies);

  assert.equal(outcome.status, "rolled_back");
  assert.deepEqual(queued, ["odoo-prod-rollback-1"]);
  assert.deepEqual(seen, [
    "pending:artifact-previous",
    "running:artifact-previous",
    "pass:artifact-previous",
  ]);
  assert.deepEqual(calls.map(([name]) => name), ["enqueue", "wait", "watch", "wait", "watch"]);
  const [, payload, key] = calls[0];
  assert.equal(key, "ui-odoo-rollback-key");
  assert.equal(payload.rollback.reason, "Drill");
  assert.equal(payload.rollback.artifact_id, undefined);
});

test("a queued rollback resumes by watching it without a new enqueue", async () => {
  const { calls, dependencies } = rollbackDependencies([rollbackOperation("pass", "completed")]);

  const { outcome } = await rollBack(dependencies, {
    ...rollbackAttempt,
    operationId: "odoo-prod-rollback-1",
  });

  assert.equal(outcome.status, "rolled_back");
  assert.deepEqual(calls.map(([name]) => name), ["watch"]);
});

test("a rollback refusal before queueing is definitive and names the reason", async () => {
  const { dependencies } = rollbackDependencies(
    [],
    new LaunchplaneApiError(
      "Odoo prod rollback found no earlier passing site/prod deployment.",
      409,
      "trace-409",
      "rollback_target_missing",
    ),
  );

  const { outcome } = await rollBack(dependencies);

  assert.equal(outcome.status, "stopped");
  assert.equal(outcome.certainty, "definitive");
  assert.equal(outcome.failure.code, "rollback_target_missing");
});

test("a stored rollback attempt survives a reload and clears after a final answer", () => {
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    removeItem: (key) => values.delete(key),
    setItem: (key, value) => values.set(key, value),
  };
  const queued = { ...rollbackAttempt, operationId: "odoo-prod-rollback-1" };

  writeOdooRollbackAttempt(scope, queued, storage);
  assert.deepEqual(readOdooRollbackAttempt(scope, storage), queued);
  writeOdooRollbackAttempt(scope, null, storage);
  assert.equal(readOdooRollbackAttempt(scope, storage), null);
});
