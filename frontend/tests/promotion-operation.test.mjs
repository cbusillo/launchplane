import assert from "node:assert/strict";
import test from "node:test";

import { LaunchplaneApiError } from "../src/api.ts";
import {
  clearPromotionDraft,
  promotionDeliveryNeedsRefresh,
  promotionFailureCertainty,
  promotionLiveConfirmation,
  promotionPlanMatchesStatus,
  readPromotionDraft,
  writePromotionDraft,
} from "../src/promotion-operation.ts";

function memoryStorage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    removeItem: (key) => values.delete(key),
    setItem: (key, value) => values.set(key, value),
  };
}

function status(overrides = {}) {
  return {
    schema_version: 1,
    product: "atlas-commerce",
    display_name: "Atlas Commerce",
    driver_id: "generic-web",
    base_driver_id: "generic-web",
    repository: "example/atlas-commerce",
    workflow_id: "promote-prod.yml",
    workflow_ref: "main",
    context: "atlas-commerce",
    source_environment: "testing",
    destination_environment: "prod",
    source: {
      environment: "testing",
      artifact_id: "ghcr.io/example/atlas-commerce@sha256:reviewed",
      source_git_ref: "reviewed-sha",
      deployment_record_id: "deployment-testing",
      inventory_updated_at: "2026-07-15T08:45:00Z",
      inventory_stale_after: "2026-07-15T09:15:00Z",
      deployment_status: "pass",
      health_status: "pass",
      runtime_identity_status: "match",
      runtime_identity_detail: "Runtime identity matches.",
      trust_state: "verified",
    },
    destination: {
      environment: "prod",
      artifact_id: "ghcr.io/example/atlas-commerce@sha256:prod",
      source_git_ref: "prod-sha",
      deployment_record_id: "deployment-prod",
      inventory_updated_at: "2026-07-15T08:40:00Z",
      inventory_stale_after: "2026-07-15T09:10:00Z",
      deployment_status: "pass",
      health_status: "pass",
      runtime_identity_status: "match",
      runtime_identity_detail: "Runtime identity matches.",
      trust_state: "verified",
    },
    evidence_fingerprint: "evidence-current",
    default_bump: "patch",
    bump_options: ["patch", "minor", "major"],
    direct_dry_run: availability("direct_dry_run"),
    workflow_dry_run: availability("workflow_dry_run"),
    workflow_live: availability("workflow_live"),
    live_confirmations: {
      patch:
        "PROMOTE atlas-commerce ghcr.io/example/atlas-commerce@sha256:reviewed reviewed-sha TO prod BUMP patch CREATE RELEASE TAG AND DEPLOY PRODUCTION",
      minor:
        "PROMOTE atlas-commerce ghcr.io/example/atlas-commerce@sha256:reviewed reviewed-sha TO prod BUMP minor CREATE RELEASE TAG AND DEPLOY PRODUCTION",
      major:
        "PROMOTE atlas-commerce ghcr.io/example/atlas-commerce@sha256:reviewed reviewed-sha TO prod BUMP major CREATE RELEASE TAG AND DEPLOY PRODUCTION",
    },
    trust_state: "verified",
    ...overrides,
  };
}

function availability(operation) {
  return {
    operation,
    authz_action: "promotion.execute",
    enabled: true,
    disabled_reasons: [],
    requires_reason: true,
    requires_idempotency_key: true,
    requires_matching_direct_dry_run: operation !== "direct_dry_run",
    requires_confirmation: operation === "workflow_live",
    consequences: [],
    trust_state: "verified",
  };
}

function response(overrides = {}) {
  return {
    status: "accepted",
    trace_id: "trace-direct",
    records: {
      backup_record_id: "",
      backup_status: "skipped",
      deployment_record_id: "",
      deployment_status: "skipped",
      destination_health_status: "pending",
      dry_run: "true",
      inventory_record_id: "",
      promotion_record_id: "promotion-review",
      promotion_status: "pending",
      release_status: "skipped",
      release_tag: "",
      release_url: "",
      source_health_status: "pending",
    },
    result: {
      product: "atlas-commerce",
      context: "atlas-commerce",
      from_instance: "testing",
      to_instance: "prod",
      artifact_id: "ghcr.io/example/atlas-commerce@sha256:reviewed",
      source_git_ref: "reviewed-sha",
      backup_record_id: "",
      promotion_record_id: "promotion-review",
      promotion_status: "pending",
      backup_status: "skipped",
      source_health_status: "pending",
      destination_health_status: "pending",
      release_status: "skipped",
      release_tag: "",
      release_url: "",
      deployment_record_id: "",
      deployment_status: "skipped",
      inventory_record_id: "",
      target_name: "",
      target_category: "unknown",
      provider_target_type: "",
      provider_id: "",
      target_id: "",
      dry_run: true,
      error_message: "",
      evidence_fingerprint: "evidence-current",
      bump: "patch",
      ...overrides,
    },
  };
}

test("workflow readiness requires the exact accepted evidence and bump", () => {
  const currentStatus = status();
  const accepted = response();

  assert.equal(promotionPlanMatchesStatus(accepted, currentStatus, "patch"), true);
  assert.equal(promotionPlanMatchesStatus(accepted, currentStatus, "minor"), false);
  assert.equal(
    promotionPlanMatchesStatus(
      accepted,
      status({ evidence_fingerprint: "evidence-new" }),
      "patch",
    ),
    false,
  );
  assert.equal(
    promotionPlanMatchesStatus(
      response({ artifact_id: "ghcr.io/example/atlas-commerce@sha256:other" }),
      currentStatus,
      "patch",
    ),
    false,
  );
});

test("live confirmation comes only from the server status contract", () => {
  assert.equal(
    promotionLiveConfirmation(status(), "minor"),
    "PROMOTE atlas-commerce ghcr.io/example/atlas-commerce@sha256:reviewed reviewed-sha TO prod BUMP minor CREATE RELEASE TAG AND DEPLOY PRODUCTION",
  );
});

test("delivery refresh distinguishes pending dispatch from observed run", () => {
  assert.equal(promotionDeliveryNeedsRefresh(null), false);
  assert.equal(promotionDeliveryNeedsRefresh({ state: "pending" }), true);
  assert.equal(promotionDeliveryNeedsRefresh({ state: "running" }), true);
  assert.equal(promotionDeliveryNeedsRefresh({ state: "delivered" }), false);
  assert.equal(promotionDeliveryNeedsRefresh({ state: "reconcile_required" }), true);
});

test("post-dispatch server and timeout failures remain uncertain", () => {
  assert.equal(
    promotionFailureCertainty(new LaunchplaneApiError("failed", 503), true),
    "uncertain",
  );
  assert.equal(
    promotionFailureCertainty(new LaunchplaneApiError("busy", 429), true),
    "uncertain",
  );
  assert.equal(
    promotionFailureCertainty(new LaunchplaneApiError("invalid", 409), true),
    "definitive",
  );
  assert.equal(
    promotionFailureCertainty(new LaunchplaneApiError("failed", 503), false),
    "definitive",
  );
});

test("promotion draft preserves exact uncertain direct and workflow requests", () => {
  const storage = memoryStorage();
  const acceptedWorkflow = {
    status: "accepted",
    trace_id: "trace-workflow",
    records: { outbox_delivery_id: "delivery-1" },
    replayed: false,
    result: {
      delivery_id: "delivery-1",
      dry_run: false,
      bump: "minor",
    },
  };
  const pendingDirect = {
    schema_version: 1,
    reason: "Verify the reviewed artifact.",
    evidence_fingerprint: "evidence-current",
    bump: "minor",
  };
  const pendingWorkflow = {
    schema_version: 1,
    dry_run: false,
    reason: "Ship the reviewed artifact.",
    evidence_fingerprint: "evidence-current",
    bump: "minor",
    confirmation: "confirmed-live-scope",
  };

  writePromotionDraft(
    "atlas-commerce",
    "prod",
    {
      acceptedWorkflow,
      bump: "minor",
      reason: pendingWorkflow.reason,
      pendingDirect,
      pendingWorkflow,
    },
    storage,
  );

  assert.deepEqual(readPromotionDraft("atlas-commerce", "prod", storage), {
    acceptedWorkflow,
    bump: "minor",
    reason: pendingWorkflow.reason,
    pendingDirect,
    pendingWorkflow,
  });
  clearPromotionDraft("atlas-commerce", "prod", storage);
  assert.equal(readPromotionDraft("atlas-commerce", "prod", storage), null);
});
