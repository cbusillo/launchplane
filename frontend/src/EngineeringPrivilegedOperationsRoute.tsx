import { KeyRound, ShieldAlert } from "lucide-react";
import { useCallback, useRef, useState } from "react";

import {
  LaunchplaneApiError,
  approvePrivilegedOperation,
  cancelPrivilegedOperation,
  listProductProfiles,
  prepareAuthorizationCandidate,
  readPrivilegedOperationPlans,
  readPrivilegedOperationReview,
  readPrivilegedOperationRawDetail,
  revokePrivilegedOperation,
  type AuthorizationCandidateId,
  type PrivilegedOperationListResponse,
  type PrivilegedOperationSemanticReview,
  type PrivilegedOperationHumanResponse,
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
import {
  loadSelectedOperationPlans,
  selectedPlanType,
  type SelectedOperationPlans,
  type PrivilegedOperationDescriptorId,
} from "./privileged-operation-selection";

export function EngineeringPrivilegedOperationsRoute({ admin, fixtureMode }: { admin: boolean; fixtureMode: DevFixtureMode }) {
  return <DefaultPrivilegedOperationsRoute admin={admin} fixtureMode={fixtureMode} />;
}

function DefaultPrivilegedOperationsRoute({
  admin,
  fixtureMode,
}: {
  admin: boolean;
  fixtureMode: DevFixtureMode;
}) {
  const query = new URLSearchParams(window.location.search);
  const operationId = query.get("operation_id");
  const reviewFixture = query.get("review") === "product-evidence"
    ? "product-evidence"
    : "default";
  const [selectedDescriptorId, setDescriptorId] =
    useState<PrivilegedOperationDescriptorId | null>(
      selectedPlanType(query.get("descriptor_id")),
    );
  const openedDescriptorId = useRef<PrivilegedOperationDescriptorId | null>(null);
  const loader = useCallback(
    async (
      signal: AbortSignal,
      reason: EngineeringLoadReason,
    ): Promise<SelectedOperationPlans> => {
      if (fixtureMode && operationId !== null) {
        await fixtureDelay(signal);
        const descriptorId = selectedDescriptorId ?? "managed-secret-reencryption";
        return {
          descriptorId,
          plans: privilegedOperationFixture(fixtureMode, descriptorId, reviewFixture),
        };
      }
      if (operationId !== null) {
        const result = await readPrivilegedOperationReview(operationId, signal);
        return {
          descriptorId: selectedPlanType(result.review.descriptor_id),
          plans: {
            status: result.status,
            trace_id: result.trace_id,
            total: 1,
            reviews: [result.review],
          },
        };
      }
      const selection = selectedDescriptorId ??
        (reason === "refresh" ? openedDescriptorId.current : null);
      const result = await loadSelectedOperationPlans(selection, signal, async descriptorId => {
        if (fixtureMode) {
          await fixtureDelay(signal);
          return privilegedOperationFixture(fixtureMode, descriptorId, reviewFixture);
        }
        return readPrivilegedOperationPlans(signal, descriptorId);
      });
      signal.throwIfAborted();
      openedDescriptorId.current = result.descriptorId;
      return result;
    },
    [selectedDescriptorId, fixtureMode, operationId, reviewFixture],
  );
  const resource = useEngineeringResource(
    loader,
    `privileged-operations:${operationId ?? selectedDescriptorId ?? "initial"}:${fixtureMode}:${reviewFixture}`,
  );

  const descriptorId = selectedDescriptorId ?? resource.state.data?.descriptorId ?? null;
  function selectDescriptorId(next: PrivilegedOperationDescriptorId) {
    if (next !== descriptorId) setDescriptorId(next);
  }

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
              onClick={() => selectDescriptorId("managed-secret-reencryption")}
              type="button"
            >
              Secret rotation
            </button>
            <button
              aria-pressed={descriptorId === "managed-authz-policy-set"}
              onClick={() => selectDescriptorId("managed-authz-policy-set")}
              type="button"
            >
              Access policy
            </button>
            <button
              aria-pressed={
                descriptorId === "managed-merge-train-policy-import"
              }
              onClick={() =>
                selectDescriptorId("managed-merge-train-policy-import")
              }
              type="button"
            >
              Merge-train policy
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
      description="Review access and secret changes before approving them."
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
        {(data) => data.plans === null ? (
          <EngineeringEmpty
            title="No readable plan types"
            icon={ShieldAlert}
            detail="You do not have read access to secret rotation, access policy, or merge-train policy plans. Choose a tab to see its access details."
          />
        ) : (
          <>
            {descriptorId === "managed-authz-policy-set" &&
            operationId === null ? (
              <AccessPolicyComposer
                fixtureMode={fixtureMode}
                refresh={resource.refresh}
              />
            ) : null}
            <PrivilegedOperationPlanList admin={admin} data={data.plans} refresh={resource.refresh} />
          </>
        )}
      </EngineeringResourceGate>
    </EngineeringRouteFrame>
  );
}

function AccessPolicyComposer({
  fixtureMode,
  refresh,
}: {
  fixtureMode: DevFixtureMode;
  refresh: () => void;
}) {
  return (
    <div className="privileged-operation-access-policy-options">
      <AuthorizationCandidateCard
        candidateId="administrator-product-evidence-read"
        headingId="administrator-product-evidence-read-heading"
        title="Prepare project evidence access"
        description="Prepare standing read access for this administrator to project and environment evidence across all current and future projects. If installed, access remains until a separately governed removal. This creates a plan for review; it does not approve or apply the access."
        addLabel="Prepare evidence access"
        removeLabel="Prepare evidence removal"
        alreadyAddedMessage="Project evidence access is already installed."
        alreadyRemovedMessage="Project evidence access is already removed."
        refresh={refresh}
      />
      <AuthorizationCandidateCard
        candidateId="agent-policy-proposer"
        headingId="agent-policy-proposer-heading"
        title="Prepare agent proposal access"
        description="Let your configured agent prepare access-policy and merge-train policy plans for your review. Every plan remains pending until you approve it while signed in. The agent cannot approve or apply a plan. This access remains until a separately approved removal."
        addLabel="Prepare proposal access"
        removeLabel="Prepare proposal removal"
        alreadyAddedMessage="Agent proposal access is already installed."
        alreadyRemovedMessage="Agent proposal access is already removed."
        refresh={refresh}
      />
      <AgentProductSetupCandidateCard fixtureMode={fixtureMode} refresh={refresh} />
    </div>
  );
}

type AgentProductSetupChoice = { product: string; displayName: string };

const AGENT_PRODUCT_SETUP_CANDIDATE_ID: AuthorizationCandidateId =
  "agent-product-setup";

function AgentProductSetupCandidateCard({
  fixtureMode,
  refresh,
}: {
  fixtureMode: DevFixtureMode;
  refresh: () => void;
}) {
  const loader = useCallback(
    async (signal: AbortSignal): Promise<AgentProductSetupChoice[]> => {
      if (fixtureMode) await fixtureDelay(signal);
      const profiles = fixtureMode
        ? agentProductSetupProfilesFixture()
        : (await listProductProfiles(signal)).profiles;
      return profiles
        .map((profile) => ({
          product: profile.product,
          displayName: profile.display_name || profile.product,
        }))
        .sort((left, right) => left.product.localeCompare(right.product));
    },
    [fixtureMode],
  );
  const products = useEngineeringResource(
    loader,
    `agent-product-setup-products:${fixtureMode}`,
  );
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [pendingIntent, setPendingIntent] =
    useState<AuthorizationCandidateIntent | null>(null);
  const [message, setMessage] = useState("");
  const [traceId, setTraceId] = useState("");
  const retryKeys = useRef<Partial<Record<string, string>>>({});

  function toggle(product: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(product)) next.delete(product);
      else next.add(product);
      return next;
    });
  }

  async function prepare(
    intent: AuthorizationCandidateIntent,
    available: readonly AgentProductSetupChoice[],
  ) {
    const chosen =
      intent === "add"
        ? available
            .map((choice) => choice.product)
            .filter((product) => selected.has(product))
        : [];
    if (intent === "add" && chosen.length === 0) {
      setMessage("Choose at least one product.");
      setTraceId("");
      return;
    }
    setPendingIntent(intent);
    setMessage("");
    setTraceId("");
    const retryKey = `${intent}:${chosen.join(",")}`;
    const sourceEventId =
      retryKeys.current[retryKey] ??
      `ui:authorization-candidate:${AGENT_PRODUCT_SETUP_CANDIDATE_ID}:${intent}:${crypto.randomUUID()}`;
    retryKeys.current[retryKey] = sourceEventId;
    try {
      const response = await prepareAuthorizationCandidate(
        AGENT_PRODUCT_SETUP_CANDIDATE_ID,
        intent,
        sourceEventId,
        undefined,
        intent === "add" ? chosen : undefined,
      );
      setTraceId(response.trace_id);
      if (response.state === "planned" && response.operation_id) {
        delete retryKeys.current[retryKey];
        window.location.assign(
          `/ui/engineering/privileged-operations?operation_id=${encodeURIComponent(response.operation_id)}`,
        );
        return;
      }
      if (response.state === "planned") {
        setMessage("The plan was prepared, but its review is not available yet.");
      } else {
        delete retryKeys.current[retryKey];
        setMessage(
          intent === "add"
            ? "Agent product setup already covers exactly these products."
            : "Agent product setup is already removed.",
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
    <section
      aria-labelledby="agent-product-setup-heading"
      className="privileged-operation-card"
    >
      <header>
        <div>
          <span className="engineering-kicker">Access policy</span>
          <h2 id="agent-product-setup-heading">Prepare agent product setup</h2>
          <p>
            On each selected product, the Director's agent may plan and apply
            settings and secrets on the testing lane, create the testing lane's
            Dokploy compose target in a new provider project (create only), and
            write the production
            backup policy that promotion's backup gate relies on. It cannot
            change the Client or release review, deploy, promote, roll back, run
            a backup, or reach any other product. The selection replaces the
            products already covered. This creates a plan for review and changes
            nothing until it is approved; installed access stays until a separate
            removal plan is approved.
          </p>
        </div>
      </header>
      <EngineeringResourceGate
        noun="Products"
        refresh={products.refresh}
        state={products.state}
      >
        {(choices) => (
          <>
            {choices.length ? (
              <fieldset className="agent-product-setup-products">
                <legend>Products</legend>
                {choices.map((choice) => (
                  <label key={choice.product}>
                    <input
                      checked={selected.has(choice.product)}
                      disabled={pendingIntent !== null}
                      onChange={() => toggle(choice.product)}
                      type="checkbox"
                    />{" "}
                    {choice.displayName}
                    {choice.displayName !== choice.product ? (
                      <> ({choice.product})</>
                    ) : null}
                  </label>
                ))}
              </fieldset>
            ) : (
              <p role="status">No products are recorded.</p>
            )}
            <div className="privileged-operation-actions activation-plan-actions">
              <button
                disabled={pendingIntent !== null || selected.size === 0}
                onClick={() => void prepare("add", choices)}
                type="button"
              >
                {pendingIntent === "add"
                  ? "Preparing access…"
                  : "Prepare product setup access"}
              </button>
              <button
                disabled={pendingIntent !== null}
                onClick={() => void prepare("remove", choices)}
                type="button"
              >
                {pendingIntent === "remove"
                  ? "Preparing removal…"
                  : "Prepare product setup removal"}
              </button>
            </div>
          </>
        )}
      </EngineeringResourceGate>
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

function agentProductSetupProfilesFixture(): Array<{
  product: string;
  display_name: string;
  production_use: "unknown" | "prelaunch" | "live";
}> {
  return [
    { product: "example-shop", display_name: "Example Shop", production_use: "prelaunch" },
    { product: "example-docs", display_name: "Example Docs", production_use: "unknown" },
    { product: "example-live", display_name: "Example Live", production_use: "live" },
  ];
}

function AuthorizationCandidateCard({
  addLabel,
  alreadyAddedMessage,
  alreadyRemovedMessage,
  candidateId,
  description,
  headingId,
  refresh,
  removeLabel,
  title,
  warning,
}: {
  addLabel: string;
  alreadyAddedMessage: string;
  alreadyRemovedMessage: string;
  candidateId: AuthorizationCandidateId;
  description: string;
  headingId: string;
  refresh: () => void;
  removeLabel: string;
  title: string;
  warning?: string;
}) {
  const [pendingIntent, setPendingIntent] =
    useState<AuthorizationCandidateIntent | null>(null);
  const [message, setMessage] = useState("");
  const [traceId, setTraceId] = useState("");
  const retryKeys = useRef<Partial<Record<string, string>>>({});

  async function prepare(intent: AuthorizationCandidateIntent) {
    setPendingIntent(intent);
    setMessage("");
    setTraceId("");
    const retryKey = `${candidateId}:${intent}`;
    const sourceEventId =
      retryKeys.current[retryKey] ??
      `ui:authorization-candidate:${candidateId}:${intent}:${crypto.randomUUID()}`;
    retryKeys.current[retryKey] = sourceEventId;
    try {
      const response = await prepareAuthorizationCandidate(
        candidateId,
        intent,
        sourceEventId,
      );
      setTraceId(response.trace_id);
      if (response.state === "planned" && response.operation_id) {
        delete retryKeys.current[retryKey];
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
        delete retryKeys.current[retryKey];
        setMessage(intent === "add" ? alreadyAddedMessage : alreadyRemovedMessage);
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
    <section
      aria-labelledby={headingId}
      className="privileged-operation-card"
    >
      <header>
        <div>
          <span className="engineering-kicker">Access policy</span>
          <h2 id={headingId}>{title}</h2>
          <p>{description}</p>
        </div>
      </header>
      {warning ? (
        <div className="privileged-operation-warning" role="note">
          <ShieldAlert size={18} aria-hidden="true" />
          <span>{warning}</span>
        </div>
      ) : null}
      <div className="privileged-operation-actions activation-plan-actions">
        <button
          disabled={pendingIntent !== null}
          onClick={() => void prepare("add")}
          type="button"
        >
          {pendingIntent === "add"
            ? "Preparing access…"
            : addLabel}
        </button>
        <button
          disabled={pendingIntent !== null}
          onClick={() => void prepare("remove")}
          type="button"
        >
          {pendingIntent === "remove"
            ? "Preparing removal…"
            : removeLabel}
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
  admin,
  data,
  refresh,
}: {
  admin: boolean;
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
          admin={admin}
          key={review.operation_id}
          review={review}
          refresh={refresh}
        />
      ))}
    </div>
  );
}


function PrivilegedOperationPlanCard({
  admin,
  review,
  refresh,
}: {
  admin: boolean;
  review: PrivilegedOperationSemanticReview;
  refresh: () => void;
}) {
  const [mutationMessage, setMutationMessage] = useState("");
  const [detailMessage, setDetailMessage] = useState("");
  const [rawDetail, setRawDetail] = useState("");
  const [cancelReason, setCancelReason] = useState("");
  const [pendingMutation, setPendingMutation] = useState(false);
  const mutationInFlight = useRef(false);
  const cancelEventId = useRef<string | null>(null);
  const [cancellation, setCancellation] = useState<PrivilegedOperationHumanResponse | null>(null);
  const isAgentPolicyProposal = review.requested_by_kind === "local_operator";
  const needsDetail = isAgentPolicyProposal || review.lifecycle.status === "cancelled";
  const proposalLoader = useCallback(async (signal: AbortSignal) => {
    if (!needsDetail) return null;
    return readPrivilegedOperationRawDetail(review.operation_id, signal);
  }, [review.operation_id, needsDetail]);
  const proposalResource = useEngineeringResource(proposalLoader, `proposal:${review.operation_id}:${review.lifecycle.status}:${needsDetail}`);
  const proposal = proposalResource.state.data;
  const proposalMatches = proposalResource.state.phase === "ready"
    && !proposalResource.state.stale
    && proposal?.record.operation_id === review.operation_id;
  const cancellationResult = cancellation ?? (proposalMatches && proposal?.record.status === "cancelled" ? proposal : null);
  const cancelled = cancellationResult?.record.status === "cancelled" || review.lifecycle.status === "cancelled";
  const canCancel = admin && !cancelled && review.lifecycle.status === "planned"
    && review.descriptor_id !== "ordinary-agent-delivery-activation"
    && !review.lifecycle.approval_recorded && !review.lifecycle.execution_recorded;
  const updateMessage = !cancellation && cancellationResult
    ? "Plan cancelled. It can no longer be approved or executed."
    : mutationMessage;

  async function mutate(action: "approve" | "revoke") {
    if (mutationInFlight.current || cancelled || cancelEventId.current !== null) return;
    mutationInFlight.current = true;
    setPendingMutation(true);
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
    } finally {
      mutationInFlight.current = false;
      setPendingMutation(false);
    }
  }

  async function cancelPlan() {
    const reason = cancelReason.trim();
    if (!reason || !canCancel || mutationInFlight.current) return;
    mutationInFlight.current = true;
    setPendingMutation(true);
    setMutationMessage("");
    cancelEventId.current ??= `ui:cancel:${crypto.randomUUID()}`;
    let recorded = false;
    try {
      const result = await cancelPrivilegedOperation(review.operation_id, reason, cancelEventId.current);
      if (result.record.operation_id !== review.operation_id || result.record.status !== "cancelled") {
        throw new Error("Cancellation response did not confirm this plan.");
      }
      recorded = true;
      setCancellation(result);
      setRawDetail("");
      const detail = await readPrivilegedOperationRawDetail(review.operation_id);
      if (detail.record.operation_id !== review.operation_id || detail.record.status !== "cancelled") {
        throw new Error("Cancellation readback did not confirm this plan.");
      }
      setCancellation(detail);
      setRawDetail(JSON.stringify(detail, null, 2));
      setMutationMessage("Plan cancelled. It can no longer be approved or executed.");
    } catch (error) {
      if (!recorded && error instanceof LaunchplaneApiError
        && [400, 403, 409, 410, 422].includes(error.statusCode)) {
        cancelEventId.current = null;
      }
      setMutationMessage(recorded
        ? "Cancellation recorded, but the refreshed result could not be read. Refresh plans to check it."
        : error instanceof LaunchplaneApiError
          ? error.message
          : "Cancellation could not be confirmed. Refresh plans before trying again.");
    } finally {
      refresh();
      mutationInFlight.current = false;
      setPendingMutation(false);
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
  const isProductEvidenceReview =
    review.title === "Review administrator product evidence access";

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
          className={`privileged-operation-status ${cancelled ? "cancelled" : review.lifecycle.status}`}
        >
          {cancelled ? "cancelled" : review.lifecycle.status}
        </span>
      </header>

      {review.blockers.state !== "clear" ? (
        <div className="privileged-operation-warning" role="status">
          <ShieldAlert size={18} aria-hidden="true" />
          <span>
            {review.blockers.state === "error"
              ? "The persisted evidence reports an error state."
              : "The persisted evidence reports blocker state."}
          </span>
        </div>
      ) : null}

      {isProductEvidenceReview ? null : (
        <dl className="privileged-operation-metrics">
          {review.change.metrics.slice(0, 8).map((metric) => (
            <div key={metric.kind}>
              <dt>{metric.label}</dt>
              <dd>{metric.value}</dd>
            </div>
          ))}
        </dl>
      )}

      <p>{review.change.summary}</p>

      <dl className="privileged-operation-details">
        <div>
          <dt>Review validity</dt>
          <dd>
            {cancelled ? "This review has ended and cannot be reused." : review.lifecycle.status === "expired"
              ? "Expired historical review."
              : review.lifecycle.status === "planned" || review.lifecycle.status === "approved"
                ? "No time limit. Policy and pre-state checks still apply; withdrawal or execution ends validity."
                : "This review has ended and cannot be reused."}
          </dd>
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
        {isProductEvidenceReview ? (
          <dl className="privileged-operation-metrics">
            {review.change.metrics.slice(0, 8).map((metric) => (
              <div key={metric.kind}>
                <dt>{metric.label}</dt>
                <dd>{metric.value}</dd>
              </div>
            ))}
          </dl>
        ) : null}
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

      {isAgentPolicyProposal ? (
        <section aria-label="Proposed policy changes" className="privileged-operation-policy-review">
          <h3>Proposed policy changes</h3>
          <p>Review the exact prepared request and change evidence before approving.</p>
          <EngineeringResourceGate noun="Proposed policy changes" refresh={proposalResource.refresh} state={proposalResource.state}>
            {detail => detail && detail.record.operation_id === review.operation_id ? (
              <>
                <h4>Proposer</h4>
                <pre>{JSON.stringify(detail.record.requested_by, null, 2)}</pre>
                <h4>Prepared request</h4>
                <pre>{JSON.stringify(detail.record.request, null, 2)}</pre>
                <h4>Change evidence</h4>
                <pre>{JSON.stringify(detail.record.evidence, null, 2)}</pre>
              </>
            ) : <p>The proposal details do not match this plan.</p>}
          </EngineeringResourceGate>
        </section>
      ) : null}

      {!cancelled && (review.can_approve || review.can_revoke) ? (
        <div className="privileged-operation-actions">
          {review.can_approve ? (
            <button type="button" disabled={pendingMutation || cancelEventId.current !== null || (isAgentPolicyProposal && !proposalMatches)} onClick={() => void mutate("approve")}>
              Approve plan
            </button>
          ) : null}
          {review.can_revoke ? (
            <button type="button" disabled={pendingMutation} onClick={() => void mutate("revoke")}>
              Revoke approval
            </button>
          ) : null}
        </div>
      ) : null}

      {canCancel ? (
        <form className="privileged-operation-cancel" onSubmit={event => {
          event.preventDefault();
          void cancelPlan();
        }}>
          <label>
            Reason for cancellation
            <textarea required maxLength={4000} value={cancelReason} disabled={pendingMutation || cancelEventId.current !== null}
              onChange={event => setCancelReason(event.target.value)} />
          </label>
          <p>Reject this pending plan permanently. It cannot be approved or executed afterwards.</p>
          <button type="submit" disabled={pendingMutation || !cancelReason.trim()}>
            {pendingMutation ? "Updating plan…" : "Reject / Cancel plan"}
          </button>
        </form>
      ) : null}

      {cancellationResult ? (
        <section aria-label="Cancellation result" className="privileged-operation-terminal-reason">
          <p>{cancellationResult.record.terminal_reason}</p>
          {cancellationResult.events.filter(event => event.action === "cancelled").map(event => event.actor.identity_type === "github_human" ? (
            <p key={event.event_id}>Cancelled by {event.actor.login} (GitHub ID {event.actor.github_id}) · {formatTime(event.occurred_at)}</p>
          ) : null)}
        </section>
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

      {updateMessage ? (
        <p className="privileged-operation-terminal-reason" role="status" aria-label="Operation update">
          {updateMessage}
        </p>
      ) : null}

      {review.lifecycle.terminal_reason_available && !cancellationResult ? (
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
  reviewFixture: "default" | "product-evidence" = "default",
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
        ? [
            reviewFixture === "product-evidence"
              ? productEvidenceFixtureReview()
              : policyFixtureReview(),
          ]
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

function productEvidenceFixtureReview(): PrivilegedOperationSemanticReview {
  return semanticReviewFixture({
    operationClass: "managed_authz_policy_set",
    descriptorId: "managed-authz-policy-set",
    safetyClass: "policy_admin",
    title: "Review administrator product evidence access",
    requestedByKind: "github_human",
    createdAt: "2026-09-03T10:01:00+00:00",
    expiresAt: "2026-09-03T23:01:00+00:00",
    scope: "authorization_policy",
    blastRadius:
      "Project-level and environment-level product evidence for all current and future projects.",
    rollbackClass: "policy_cas",
    rollback:
      "Rollback is bounded by authorization policy CAS and record digest evidence.",
    summary:
      "Allow the requesting administrator to read project-level and environment-level product evidence for all current and future projects. This read-only access is standing until a separately governed removal; the Approve-by deadline only bounds this plan. It grants no writes or agent authority.",
    metrics: [
      { kind: "policy_rules_added", label: "Added", value: 2 },
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
