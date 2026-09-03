import { KeyRound, ShieldAlert } from "lucide-react";
import { useCallback, useState } from "react";

import {
  LaunchplaneApiError,
  approvePrivilegedOperation,
  readPrivilegedOperationPlans,
  readPrivilegedOperationRawDetail,
  revokePrivilegedOperation,
  type PrivilegedOperationDescriptorId,
  type PrivilegedOperationListResponse,
  type PrivilegedOperationSemanticReview,
} from "./api";
import type { DevFixtureMode } from "./dev-fixture-loader";
import {
  useEngineeringResource,
  type EngineeringLoadReason,
} from "./engineering-resource";
import {
  EngineeringBoundaryNote,
  EngineeringEmpty,
  EngineeringResourceControls,
  EngineeringResourceGate,
  EngineeringRouteFrame,
} from "./EngineeringRouteUi";
import { formatTime } from "./format";

export function EngineeringPrivilegedOperationsRoute({
  fixtureMode,
}: {
  fixtureMode: DevFixtureMode;
}) {
  const [descriptorId, setDescriptorId] =
    useState<PrivilegedOperationDescriptorId>("managed-secret-reencryption");
  const loader = useCallback(
    async (
      signal: AbortSignal,
      _reason: EngineeringLoadReason,
    ): Promise<PrivilegedOperationListResponse> => {
      if (fixtureMode) {
        await fixtureDelay(signal);
        return privilegedOperationFixture(fixtureMode, descriptorId);
      }
      return readPrivilegedOperationPlans(signal, descriptorId);
    },
    [descriptorId, fixtureMode],
  );
  const resource = useEngineeringResource(
    loader,
    `privileged-operations:${descriptorId}:${fixtureMode}`,
  );

  return (
    <EngineeringRouteFrame
      actions={
        <div className="privileged-operation-toolbar">
          <div
            className="privileged-operation-kind-switch"
            aria-label="Operation type"
          >
            <button
              aria-pressed={descriptorId === "managed-secret-reencryption"}
              onClick={() => setDescriptorId("managed-secret-reencryption")}
              type="button"
            >
              Secret rotation
            </button>
            <button
              aria-pressed={descriptorId === "managed-authz-policy-set"}
              onClick={() => setDescriptorId("managed-authz-policy-set")}
              type="button"
            >
              Access policy
            </button>
            <button
              aria-pressed={
                descriptorId === "managed-merge-train-policy-import"
              }
              onClick={() =>
                setDescriptorId("managed-merge-train-policy-import")
              }
              type="button"
            >
              Merge-train policy
            </button>
          </div>
          <EngineeringResourceControls
            cancel={resource.cancel}
            refresh={resource.refresh}
            refreshLabel="Refresh plans"
            state={resource.state}
          />
        </div>
      }
      description="Review typed, redacted privileged-operation plans and record human approvals or revocations without exposing credentials."
      icon={KeyRound}
      title="Privileged operation plans"
      view="privileged-operations"
    >
      <EngineeringBoundaryNote title="Human-governed approval — internal execution only">
        GitHub humans may approve or revoke a current plan. Execution remains a
        service-internal worker action with fresh policy and plan revalidation;
        this UI has no execute control. Agents may submit inert policy proposals
        and receive only their own bounded summaries. The new actions ship with
        no production grants.
      </EngineeringBoundaryNote>

      <EngineeringResourceGate
        noun="Privileged-operation plans"
        refresh={resource.refresh}
        state={resource.state}
      >
        {(data) => (
          <PrivilegedOperationPlanList data={data} refresh={resource.refresh} />
        )}
      </EngineeringResourceGate>
    </EngineeringRouteFrame>
  );
}

function PrivilegedOperationPlanList({
  data,
  refresh,
}: {
  data: PrivilegedOperationListResponse;
  refresh: () => void;
}) {
  if (!data.reviews.length) {
    return (
      <EngineeringEmpty
        detail="No typed privileged-operation plan has been recorded. Planning routes remain unavailable until an explicit managed rule is activated through the separate authorization process."
        icon={KeyRound}
        title="No privileged-operation plans"
      />
    );
  }
  return (
    <div
      className="privileged-operation-list"
      aria-label="Privileged-operation plans"
    >
      {data.reviews.map((review) => (
        <PrivilegedOperationPlanCard
          key={review.operation_id}
          review={review}
          refresh={refresh}
        />
      ))}
    </div>
  );
}

function PrivilegedOperationPlanCard({
  review,
  refresh,
}: {
  review: PrivilegedOperationSemanticReview;
  refresh: () => void;
}) {
  const [mutationMessage, setMutationMessage] = useState("");
  const [detailMessage, setDetailMessage] = useState("");
  const [rawDetail, setRawDetail] = useState("");

  async function mutate(action: "approve" | "revoke") {
    const reason = window
      .prompt(action === "approve" ? "Approval reason" : "Revocation reason")
      ?.trim();
    if (!reason) return;
    setMutationMessage("");
    try {
      if (action === "approve") {
        await approvePrivilegedOperation(review.operation_id, reason);
      } else {
        await revokePrivilegedOperation(review.operation_id, reason);
      }
      setMutationMessage(
        action === "approve"
          ? "Approval recorded. The service worker will revalidate before execution."
          : "Approval revoked.",
      );
      refresh();
    } catch (error) {
      setMutationMessage(
        error instanceof LaunchplaneApiError
          ? error.message
          : "The operation could not be updated.",
      );
    }
  }

  async function loadRawDetail() {
    setDetailMessage("");
    setRawDetail("");
    try {
      const detail = await readPrivilegedOperationRawDetail(
        review.operation_id,
      );
      setRawDetail(JSON.stringify(detail, null, 2));
    } catch (error) {
      setDetailMessage(
        error instanceof LaunchplaneApiError
          ? error.message
          : "The operation detail could not be loaded.",
      );
    }
  }

  const operationLabel = {
    managed_secret_reencryption: "Managed-secret re-encryption",
    managed_authz_policy_set: "Managed authorization policy",
    managed_merge_train_policy_import: "Managed merge-train policy",
  }[review.operation_class];

  return (
    <article className="privileged-operation-card">
      <header>
        <div>
          <span className="engineering-kicker">{operationLabel}</span>
          <h2>{review.title}</h2>
          <p>
            Requested by {review.requested_by_kind.replace("_", " ")} · created{" "}
            {formatTime(review.lifecycle.created_at)}
          </p>
        </div>
        <span
          className={`privileged-operation-status ${review.lifecycle.status}`}
        >
          {review.lifecycle.status}
        </span>
      </header>

      {review.blockers.state !== "clear" ? (
        <div className="privileged-operation-warning" role="status">
          <ShieldAlert size={18} aria-hidden="true" />
          <span>
            {review.blockers.state === "error"
              ? "The persisted evidence reports an error state."
              : review.lifecycle.expiry_state === "past_expiry_unreconciled"
                ? "The persisted plan is past expiry. This read remains non-mutating, so approval is unavailable until the lifecycle is reconciled."
                : "The persisted evidence reports blocker state."}
          </span>
        </div>
      ) : null}

      <dl className="privileged-operation-metrics">
        {review.change.metrics.slice(0, 8).map((metric) => (
          <div key={metric.kind}>
            <dt>{metric.label}</dt>
            <dd>{metric.value}</dd>
          </div>
        ))}
      </dl>

      <dl className="privileged-operation-details">
        <div>
          <dt>Plan expires</dt>
          <dd>{formatTime(review.lifecycle.expires_at)}</dd>
        </div>
        <div>
          <dt>Expiry state</dt>
          <dd>{review.lifecycle.expiry_state.replaceAll("_", " ")}</dd>
        </div>
        <div>
          <dt>Blast radius</dt>
          <dd>{review.blast_radius.summary}</dd>
        </div>
        <div>
          <dt>Rollback</dt>
          <dd>{review.rollback.summary}</dd>
        </div>
        <div>
          <dt>Result</dt>
          <dd>{review.evidence.result_status}</dd>
        </div>
      </dl>

      <details className="privileged-operation-policy-review">
        <summary>Digest evidence</summary>
        <dl className="privileged-operation-details">
          {review.evidence.digests.map((digest) => (
            <div key={`${digest.kind}:${digest.sha256}`}>
              <dt>{digest.label}</dt>
              <dd>
                <code className="privileged-operation-digest">
                  {digest.sha256}
                </code>
              </dd>
            </div>
          ))}
        </dl>
      </details>

      {review.activity.length ? (
        <ol className="privileged-operation-activity">
          {review.activity.map((entry) => (
            <li key={entry.event_id}>
              <span>{formatTime(entry.occurred_at)}</span>
              <strong>{entry.action}</strong>
              <span>
                {entry.actor_type.replace("_", " ")} via{" "}
                {entry.source_kind.replace("_", " ")}
              </span>
              <code className="privileged-operation-digest">
                {entry.resulting_record_digest}
              </code>
            </li>
          ))}
        </ol>
      ) : null}

      {review.can_approve || review.can_revoke ? (
        <div className="privileged-operation-actions">
          {review.can_approve ? (
            <button type="button" onClick={() => void mutate("approve")}>
              Approve plan
            </button>
          ) : null}
          {review.can_revoke ? (
            <button type="button" onClick={() => void mutate("revoke")}>
              Revoke approval
            </button>
          ) : null}
        </div>
      ) : null}

      {review.evidence.raw_detail_available ? (
        <details
          className="privileged-operation-policy-review"
          onToggle={(event) => {
            if (event.currentTarget.open && !rawDetail && !detailMessage) {
              void loadRawDetail();
            }
          }}
        >
          <summary>Authorized detail response</summary>
          {rawDetail ? (
            <pre>{rawDetail}</pre>
          ) : (
            <p>{detailMessage || "Loading"}</p>
          )}
        </details>
      ) : null}

      {mutationMessage ? (
        <p className="privileged-operation-terminal-reason">
          {mutationMessage}
        </p>
      ) : null}

      {review.lifecycle.terminal_reason_available ? (
        <p className="privileged-operation-terminal-reason">
          Terminal reason is available in the authorized detail response.
        </p>
      ) : null}
    </article>
  );
}

async function fixtureDelay(signal: AbortSignal): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const timeout = window.setTimeout(resolve, 60);
    signal.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timeout);
        reject(new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
  });
}

function privilegedOperationFixture(
  fixtureMode: Exclude<DevFixtureMode, "">,
  descriptorId: PrivilegedOperationDescriptorId,
): PrivilegedOperationListResponse {
  if (fixtureMode === "error") {
    throw new LaunchplaneApiError(
      "Privileged-operation evidence is unavailable.",
      503,
    );
  }
  if (fixtureMode === "denied") {
    throw new LaunchplaneApiError(
      "This GitHub human does not have privileged-operation read authority.",
      403,
      "fixture-privileged-operation-denied",
      "authorization_denied",
    );
  }
  if (fixtureMode === "empty" || fixtureMode === "missing") {
    return {
      status: "ok",
      trace_id: `fixture-privileged-operation-${fixtureMode}`,
      total: 0,
      reviews: [],
    };
  }
  return {
    status: "ok",
    trace_id: "fixture-privileged-operation-products",
    total: 1,
    reviews:
      descriptorId === "managed-authz-policy-set"
        ? [policyFixtureReview()]
        : descriptorId === "managed-merge-train-policy-import"
          ? [mergeTrainPolicyFixtureReview()]
          : [secretFixtureReview()],
  };
}

function secretFixtureReview(): PrivilegedOperationSemanticReview {
  return semanticReviewFixture({
    operationClass: "managed_secret_reencryption",
    descriptorId: "managed-secret-reencryption",
    safetyClass: "secret_backed",
    title: "Managed-secret re-encryption review",
    requestedByKind: "github_human",
    createdAt: "2026-09-03T10:00:00+00:00",
    expiresAt: "2026-09-03T23:00:00+00:00",
    scope: "managed_secret_store",
    blastRadius: "Bounded to configured managed-secret records.",
    rollbackClass: "key_retained",
    rollback: "Rollback depends on retained managed-secret key material.",
    metrics: [
      { kind: "configured_secrets", label: "Configured secrets", value: 18 },
      { kind: "rotation_candidates", label: "Would rotate", value: 18 },
      { kind: "unchanged_secrets", label: "Unchanged", value: 0 },
      { kind: "unreadable_secrets", label: "Unreadable", value: 0 },
    ],
  });
}

function policyFixtureReview(): PrivilegedOperationSemanticReview {
  return semanticReviewFixture({
    operationClass: "managed_authz_policy_set",
    descriptorId: "managed-authz-policy-set",
    safetyClass: "policy_admin",
    title: "Managed authorization policy review",
    requestedByKind: "terminal_agent",
    createdAt: "2026-09-03T10:01:00+00:00",
    expiresAt: "2026-09-03T23:01:00+00:00",
    scope: "authorization_policy",
    blastRadius: "Bounded to one managed authorization rule set.",
    rollbackClass: "policy_cas",
    rollback:
      "Rollback is bounded by authorization policy CAS and record digest evidence.",
    metrics: [
      { kind: "policy_rules_added", label: "Added", value: 1 },
      { kind: "policy_rules_updated", label: "Updated", value: 0 },
      { kind: "policy_rules_removed", label: "Removed", value: 0 },
      { kind: "policy_safety_blockers", label: "Safety blockers", value: 0 },
    ],
  });
}

function mergeTrainPolicyFixtureReview(): PrivilegedOperationSemanticReview {
  return semanticReviewFixture({
    operationClass: "managed_merge_train_policy_import",
    descriptorId: "managed-merge-train-policy-import",
    safetyClass: "policy_admin",
    title: "Managed merge-train policy review",
    requestedByKind: "terminal_agent",
    createdAt: "2026-09-03T10:02:00+00:00",
    expiresAt: "2026-09-03T23:02:00+00:00",
    scope: "merge_train_policy",
    blastRadius:
      "Bounded to merge-train policy target counts; target identities are redacted.",
    rollbackClass: "policy_cas",
    rollback:
      "Rollback is bounded by merge-train policy CAS and record digest evidence.",
    metrics: [
      { kind: "active_policy_targets", label: "Active targets", value: 1 },
      {
        kind: "candidate_policy_targets",
        label: "Candidate targets",
        value: 2,
      },
      { kind: "policy_targets_added", label: "Added", value: 1 },
      { kind: "policy_targets_changed", label: "Changed", value: 0 },
    ],
  });
}

function semanticReviewFixture({
  operationClass,
  descriptorId,
  safetyClass,
  title,
  requestedByKind,
  createdAt,
  expiresAt,
  scope,
  blastRadius,
  rollbackClass,
  rollback,
  metrics,
}: {
  operationClass: PrivilegedOperationSemanticReview["operation_class"];
  descriptorId: PrivilegedOperationSemanticReview["descriptor_id"];
  safetyClass: PrivilegedOperationSemanticReview["safety_class"];
  title: PrivilegedOperationSemanticReview["title"];
  requestedByKind: PrivilegedOperationSemanticReview["requested_by_kind"];
  createdAt: string;
  expiresAt: string;
  scope: PrivilegedOperationSemanticReview["blast_radius"]["scope"];
  blastRadius: string;
  rollbackClass: PrivilegedOperationSemanticReview["rollback"]["rollback_class"];
  rollback: string;
  metrics: PrivilegedOperationSemanticReview["change"]["metrics"];
}): PrivilegedOperationSemanticReview {
  return {
    schema_version: 1,
    operation_id: "privileged-operation-0123456789abcdef0123456789abcdef",
    descriptor_id: descriptorId,
    descriptor_version: 1,
    operation_class: operationClass,
    safety_class: safetyClass,
    title,
    requested_by_kind: requestedByKind,
    lifecycle: {
      status: "planned",
      generated_at: createdAt,
      expiry_state: "active",
      created_at: createdAt,
      updated_at: createdAt,
      expires_at: expiresAt,
      terminal_at: "",
      terminal_reason_available: false,
      approval_recorded: false,
      execution_recorded: false,
    },
    blockers: {
      state: "clear",
      policy_safety_blocker_count: 0,
      operational_readiness_blocker_count: 0,
      unreadable_secret_count: 0,
      codes: [],
    },
    change: {
      summary: "Server-computed semantic review fixture.",
      changed: true,
      metrics,
    },
    blast_radius: {
      scope,
      summary: blastRadius,
      affected_count: Math.max(...metrics.map((metric) => metric.value), 0),
    },
    rollback: {
      rollback_class: rollbackClass,
      summary: rollback,
    },
    evidence: {
      result_status: "ok",
      raw_detail_available: true,
      redaction: "semantic_only",
      digests: [
        { kind: "request", label: "Request digest", sha256: "1".repeat(64) },
        {
          kind: "human_evidence",
          label: "Human evidence digest",
          sha256: "2".repeat(64),
        },
        { kind: "plan", label: "Plan digest", sha256: "3".repeat(64) },
        {
          kind: "pre_state",
          label: "Pre-state digest",
          sha256: "4".repeat(64),
        },
      ],
    },
    activity: [
      {
        sequence: 1,
        action: "planned",
        occurred_at: createdAt,
        source_kind:
          requestedByKind === "terminal_agent" ? "agent_api" : "browser_api",
        actor_type: requestedByKind,
        reason_available: false,
        event_id: "privileged-operation-event-0123456789abcdef0123456789abcdef",
        resulting_record_digest: "5".repeat(64),
      },
    ],
    can_approve: true,
    can_revoke: false,
    authorizes_approval: false,
    authorizes_execution: false,
    persists_state: false,
  };
}
