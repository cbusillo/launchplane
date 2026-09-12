import { KeyRound, ShieldAlert } from "lucide-react";
import { useCallback, useRef, useState } from "react";

import {
  LaunchplaneApiError,
  approvePrivilegedOperation,
  planOrdinaryAgentDeliveryActivation,
  prepareAuthorizationCandidate,
  readOrdinaryAgentDeliveryActivationOptions,
  readPrivilegedOperationPlans,
  readPrivilegedOperationReview,
  readPrivilegedOperationRawDetail,
  revokePrivilegedOperation,
  type PrivilegedOperationDescriptorId,
  type PrivilegedOperationListResponse,
  type PrivilegedOperationSemanticReview,
  type OrdinaryAgentDeliveryActivationOptionsResponse,
  type AuthorizationCandidateIntent,
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
import { EngineeringOrdinaryAgentJobRoute } from "./EngineeringOrdinaryAgentJobRoute";
import { EngineeringOrdinaryAgentPreparationInputs } from "./EngineeringOrdinaryAgentPreparationInputs";
import { EngineeringOrdinaryAgentRoute } from "./EngineeringOrdinaryAgentRoute";

export function EngineeringPrivilegedOperationsRoute({ fixtureMode }: { fixtureMode: DevFixtureMode }) {
  const query = new URLSearchParams(window.location.search);
  const principalId = query.get("principal_id");
  const requestId = query.get("request_id");
  if (principalId !== null && requestId !== null) {
    return <EngineeringOrdinaryAgentJobRoute principalId={principalId} requestId={requestId} />;
  }
  if (principalId !== null) {
    return <EngineeringOrdinaryAgentRoute principalId={principalId} operationId={query.get("operation_id") ?? ""} />;
  }
  return <DefaultPrivilegedOperationsRoute fixtureMode={fixtureMode} />;
}

function DefaultPrivilegedOperationsRoute({
  fixtureMode,
}: {
  fixtureMode: DevFixtureMode;
}) {
  const operationId = new URLSearchParams(window.location.search).get("operation_id");
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
      if (operationId !== null) {
        const result = await readPrivilegedOperationReview(operationId, signal);
        return {
          status: result.status,
          trace_id: result.trace_id,
          total: 1,
          reviews: [result.review],
        };
      }
      return readPrivilegedOperationPlans(signal, descriptorId);
    },
    [descriptorId, fixtureMode, operationId],
  );
  const resource = useEngineeringResource(
    loader,
    `privileged-operations:${operationId ?? descriptorId}:${fixtureMode}`,
  );

  return (
    <EngineeringRouteFrame
      actions={
        <div className="privileged-operation-toolbar">
          {operationId !== null ? (
            <a href="/ui/engineering/privileged-operations">All operation plans</a>
          ) : (
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
            <button
              aria-pressed={
                descriptorId === "ordinary-agent-delivery-activation"
              }
              onClick={() =>
                setDescriptorId("ordinary-agent-delivery-activation")
              }
              type="button"
            >
              Agent delivery
            </button>
          </div>
          )}
          <EngineeringResourceControls
            cancel={resource.cancel}
            refresh={resource.refresh}
            refreshLabel="Refresh plans"
            state={resource.state}
          />
        </div>
      }
      description="Review access, delivery, and secret changes before approving them."
      icon={KeyRound}
      title="Privileged operation plans"
      view="privileged-operations"
    >
      <EngineeringBoundaryNote title="Review each change before approving it">
        Launchplane applies approved changes after required checks pass.
      </EngineeringBoundaryNote>

      <EngineeringResourceGate
        noun="Privileged-operation plans"
        refresh={resource.refresh}
        state={resource.state}
      >
        {(data) => (
          <>
            {descriptorId === "ordinary-agent-delivery-activation" &&
            operationId === null ? (
              <OrdinaryAgentDeliveryActivationComposer
                fixtureMode={fixtureMode}
                refresh={resource.refresh}
              />
            ) : null}
            {descriptorId === "managed-authz-policy-set" &&
            operationId === null ? (
              <AccessPolicyComposer refresh={resource.refresh} />
            ) : null}
            <PrivilegedOperationPlanList data={data} refresh={resource.refresh} />
          </>
        )}
      </EngineeringResourceGate>
    </EngineeringRouteFrame>
  );
}

function AccessPolicyComposer({ refresh }: { refresh: () => void }) {
  const [pendingIntent, setPendingIntent] =
    useState<AuthorizationCandidateIntent | null>(null);
  const [message, setMessage] = useState("");
  const [traceId, setTraceId] = useState("");
  const retryKeys =
    useRef<Partial<Record<AuthorizationCandidateIntent, string>>>({});

  async function prepare(intent: AuthorizationCandidateIntent) {
    setPendingIntent(intent);
    setMessage("");
    setTraceId("");
    const sourceEventId =
      retryKeys.current[intent] ??
      `ui:authorization-candidate:${intent}:${crypto.randomUUID()}`;
    retryKeys.current[intent] = sourceEventId;
    try {
      const response = await prepareAuthorizationCandidate(
        intent,
        sourceEventId,
      );
      setTraceId(response.trace_id);
      if (response.state === "planned" && response.operation_id) {
        delete retryKeys.current[intent];
        window.location.assign(
          `/ui/engineering/privileged-operations?operation_id=${encodeURIComponent(response.operation_id)}`,
        );
        return;
      }
      if (response.state === "planned") {
        setMessage(
          "The plan was prepared, but its review is not available yet.",
        );
      } else {
        delete retryKeys.current[intent];
        setMessage(
          intent === "add"
            ? "Setup access is already installed."
            : "Administrator access is already removed.",
        );
        refresh();
      }
    } catch (error) {
      if (error instanceof LaunchplaneApiError) {
        setMessage(error.message);
        setTraceId(error.traceId);
      } else {
        setMessage("The access policy plan could not be prepared.");
      }
    } finally {
      setPendingIntent(null);
    }
  }

  return (
    <section className="privileged-operation-card">
      <header>
        <div>
          <span className="engineering-kicker">Access policy</span>
          <h2>Prepare delivery administrator access</h2>
          <p>
            Prepare access to administer ordinary-agent delivery, or prepare
            removal of access that was already installed. This creates a plan
            for review; it does not start delivery. Installed access remains
            until a removal plan is approved and applied.
          </p>
        </div>
      </header>
      <div className="privileged-operation-warning" role="note">
        <ShieldAlert size={18} aria-hidden="true" />
        <span>
          Stop ordinary-agent delivery before preparing removal. The service
          will refuse removal while an active delivery could lose its stop
          controls.
        </span>
      </div>
      <div className="privileged-operation-actions activation-plan-actions">
        <button
          disabled={pendingIntent !== null}
          onClick={() => void prepare("add")}
          type="button"
        >
          {pendingIntent === "add"
            ? "Preparing access…"
            : "Prepare setup access"}
        </button>
        <button
          disabled={pendingIntent !== null}
          onClick={() => void prepare("remove")}
          type="button"
        >
          {pendingIntent === "remove"
            ? "Preparing removal…"
            : "Prepare removal"}
        </button>
      </div>
      {message ? (
        <p className="privileged-operation-terminal-reason" role="status">
          {message}
          {traceId ? (
            <>
              <br />
              Trace: {traceId}
            </>
          ) : null}
        </p>
      ) : null}
    </section>
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
        detail="Prepared changes will appear here."
        icon={KeyRound}
        title="No changes are waiting for review"
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

function OrdinaryAgentDeliveryActivationComposer({
  fixtureMode,
  refresh,
}: {
  fixtureMode: DevFixtureMode;
  refresh: () => void;
}) {
  const [intent, setIntent] = useState<"setup" | "revoke_activation">(
    "setup",
  );
  const [selection, setSelection] = useState("");
  const [durationSeconds, setDurationSeconds] = useState(24 * 60 * 60);
  const [message, setMessage] = useState("");
  const loader = useCallback(
    async (signal: AbortSignal): Promise<OrdinaryAgentDeliveryActivationOptionsResponse> => {
      if (fixtureMode) {
        await fixtureDelay(signal);
        return activationOptionsFixture(fixtureMode);
      }
      return readOrdinaryAgentDeliveryActivationOptions(signal);
    },
    [fixtureMode],
  );
  const options = useEngineeringResource(
    loader,
    `ordinary-agent-delivery-activation-options:${fixtureMode}`,
  );

  async function submit(data: OrdinaryAgentDeliveryActivationOptionsResponse) {
    setMessage("");
    try {
      if (intent === "setup") {
        const option = data.setup_options.find(
          (candidate) => candidate.policy_operation_id === selection,
        );
        const duration = data.duration_options.find(
          (candidate) => candidate.duration_seconds === durationSeconds,
        );
        if (!option || !duration) return;
        await planOrdinaryAgentDeliveryActivation({
          schema_version: 1,
          action: "setup",
          policy_operation_id: option.policy_operation_id,
          repository_inventory_record_id:
            option.repository_inventory_record_id,
          predecessor: option.predecessor,
          activation_expires_at: duration.activation_expires_at,
          reason: `Prepare qualification-only delivery for ${option.label}.`,
        });
        setMessage("Review the setup below. It starts with checks only.");
      } else {
        const option = data.revoke_options.find(
          (candidate) => candidate.activation.activation_id === selection,
        );
        if (!option) return;
        await planOrdinaryAgentDeliveryActivation({
          schema_version: 1,
          action: "revoke_activation",
          activation_id: option.activation.activation_id,
          expected_revision: option.activation.revision,
          expected_activation_sha256: option.activation.activation_sha256,
          reason: `Stop ordinary-agent delivery for ${option.label}.`,
        });
        setMessage(
          "Stop plan recorded. Review the permanent revocation behavior below.",
        );
      }
      setSelection("");
      refresh();
      options.refresh();
    } catch (error) {
      setMessage(
        error instanceof LaunchplaneApiError
          ? error.message
          : "The activation plan could not be recorded.",
      );
    }
  }

  return (
    <section className="privileged-operation-card">
      <header>
        <div>
          <span className="engineering-kicker">Agent delivery</span>
          <h2>Set up or stop agent delivery</h2>
          <p>
            {intent === "setup"
              ? "Choose a project and branch, then choose how long to allow agent delivery. Required checks must pass before delivery starts."
              : "Stops new work. Work already sent may still finish; Launchplane will check its outcome."}
          </p>
        </div>
      </header>
      <div
        className="privileged-operation-kind-switch"
        aria-label="Activation intent"
      >
        <button
          aria-pressed={intent === "setup"}
          onClick={() => {
            setIntent("setup");
            setSelection("");
          }}
          type="button"
        >
          Prepare delivery
        </button>
        <button
          aria-pressed={intent === "revoke_activation"}
          onClick={() => {
            setIntent("revoke_activation");
            setSelection("");
          }}
          type="button"
        >
          Stop delivery
        </button>
      </div>
      <EngineeringResourceGate
        noun="Activation choices"
        refresh={options.refresh}
        state={options.state}
      >
        {(data) => {
          const choices =
            intent === "setup" ? data.setup_options : data.revoke_options;
          return choices.length ? (
            <div className="privileged-operation-actions activation-plan-actions">
              <label>
                Project and branch
                <select
                  value={selection}
                  onChange={(event) => setSelection(event.target.value)}
                >
                  <option value="">Choose a target</option>
                  {choices.map((option) => {
                    const value =
                      "policy_operation_id" in option
                        ? option.policy_operation_id
                        : option.activation.activation_id;
                    return (
                      <option key={value} value={value}>
                        {option.label}
                      </option>
                    );
                  })}
                </select>
              </label>
              {intent === "setup" ? (
                <label>
                  Allow delivery for
                  <select
                    value={durationSeconds}
                    onChange={(event) =>
                      setDurationSeconds(Number(event.target.value))
                    }
                  >
                    {data.duration_options.map((option) => (
                      <option
                        key={option.duration_seconds}
                        value={option.duration_seconds}
                      >
                        {option.label}
                      </option>
                    ))}
                  </select>
                </label>
              ) : null}
              <button
                type="button"
                disabled={!selection}
                onClick={() => void submit(data)}
              >
                {intent === "setup" ? "Review setup" : "Review stop"}
              </button>
            </div>
          ) : (
            <EngineeringEmpty
              detail={
                intent === "setup"
                  ? "No reviewed policy and current inventory pair is eligible for setup."
                  : "No current activation is eligible for a reviewed stop operation."
              }
              icon={ShieldAlert}
              title="No eligible activation choice"
            />
          );
        }}
      </EngineeringResourceGate>
      {intent === "setup" ? (
        <EngineeringOrdinaryAgentPreparationInputs fixtureMode={fixtureMode} />
      ) : null}
      {message ? (
        <p className="privileged-operation-terminal-reason">{message}</p>
      ) : null}
    </section>
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
    const reason =
      action === "approve"
        ? `Approved ${review.title} after reviewing the server-computed evidence.`
        : `Revoked approval for ${review.title}.`;
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
    ordinary_agent_delivery_activation: "Agent delivery",
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

      <p>{review.change.summary}</p>

      <dl className="privileged-operation-details">
        <div>
          <dt>Approve by</dt>
          <dd>{formatTime(review.lifecycle.expires_at)}</dd>
        </div>
        <div>
          <dt>Expiry state</dt>
          <dd>{review.lifecycle.expiry_state.replaceAll("_", " ")}</dd>
        </div>
        <div>
          <dt>Scope</dt>
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
        <summary>Technical details</summary>
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
      </details>

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
            if (event.currentTarget.open && !rawDetail) {
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
          : descriptorId === "ordinary-agent-delivery-activation"
            ? [activationFixtureReview()]
          : [secretFixtureReview()],
  };
}

function activationOptionsFixture(
  fixtureMode: Exclude<DevFixtureMode, "">,
): OrdinaryAgentDeliveryActivationOptionsResponse {
  const scope = {
    target: {
      repository_id: 1001,
      repository: "example/launchplane",
      base_branch: "main",
    },
    managed_set_id: "ordinary-agent.pilot",
    managed_rule_id: "delivery-agent",
  };
  return {
    status: "ok",
    trace_id: "fixture-activation-options",
    duration_options: [
      {
        duration_seconds: 3600,
        activation_expires_at: "2026-08-22T17:00:00+00:00",
        label: "1 hour",
      },
      {
        duration_seconds: 86400,
        activation_expires_at: "2026-08-23T16:00:00+00:00",
        label: "1 day",
      },
      {
        duration_seconds: 604800,
        activation_expires_at: "2026-08-29T16:00:00+00:00",
        label: "7 days",
      },
      {
        duration_seconds: 2592000,
        activation_expires_at: "2026-09-21T16:00:00+00:00",
        label: "30 days",
      },
    ],
    setup_options: fixtureMode === "empty" ? [] : [
      {
        policy_operation_id:
          "privileged-operation-11111111111111111111111111111111",
        repository_inventory_record_id: "repository-inventory-1001-r3",
        scope,
        predecessor: null,
        label:
          "example/launchplane · main · prepared Aug 22, 2026 at 16:00:00 UTC",
      },
    ],
    revoke_options: fixtureMode === "empty" ? [] : [
      {
        activation: {
          activation_id:
            "ordinary-agent-delivery-activation-22222222222222222222222222222222",
          revision: 1,
          activation_sha256: "3".repeat(64),
        },
        scope,
        label:
          "example/launchplane · main · set up Aug 22, 2026 at 16:00:00 UTC · allowed until Aug 23, 2026 at 16:00:00 UTC",
      },
    ],
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

function activationFixtureReview(): PrivilegedOperationSemanticReview {
  return semanticReviewFixture({
    operationClass: "ordinary_agent_delivery_activation",
    descriptorId: "ordinary-agent-delivery-activation",
    safetyClass: "policy_admin",
    title: "Review agent delivery setup",
    requestedByKind: "github_human",
    createdAt: "2026-09-03T10:03:00+00:00",
    expiresAt: "2026-09-03T10:33:00+00:00",
    scope: "ordinary_agent_delivery_activation",
    blastRadius: "example/launchplane on main; one agent delivery setup.",
    rollbackClass: "activation_revoke",
    rollback: "Stopping later requires a separate review.",
    summary:
      "Set up agent delivery for example/launchplane on main until Sep 04, 2026 at 10:03 UTC (1 day remaining). Delivery starts with checks only; new agent work stays blocked until every required check passes. Stopping delivery blocks new work. Work already sent may still finish while Launchplane checks its outcome.",
    metrics: [
      {
        kind: "activation_scope_targets",
        label: "Projects and branches",
        value: 1,
      },
      {
        kind: "activation_setup_blockers",
        label: "Checks blocking setup",
        value: 0,
      },
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
  summary,
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
  summary?: string;
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
      summary: summary ?? "Server-computed semantic review fixture.",
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
