import {
  AlertTriangle,
  CheckCircle2,
  ExternalLink,
  GitBranch,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
  ShieldCheck,
  Square,
  SquareCheckBig,
  XCircle,
} from "lucide-react";
import { useEffect, useState } from "react";

import {
  dispatchProductPromotionWorkflow,
  dryRunProductPromotion,
  readProductPromotionWorkflowDelivery,
} from "./api";
import type { BrowserOperationState } from "./browser-operation";
import { loadDevFixtures, type DevFixtureMode } from "./dev-fixture-loader";
import {
  clearPromotionDraft,
  promotionDeliveryNeedsRefresh,
  promotionFailureCertainty,
  promotionLiveConfirmation,
  promotionOperationFailure,
  promotionPlanMatchesStatus,
  readPromotionDraft,
  writePromotionDraft,
  type PromotionBump,
  type StoredPromotionDirectRequest,
  type StoredPromotionWorkflowRequest,
} from "./promotion-operation";
import { EvidenceBadge, humanize } from "./ProductOps";
import { emptyResource, type ResourceState } from "./resource";
import { safeExternalUrl } from "./url";
import { useBrowserOperationController } from "./use-browser-operation";

import type {
  ProductEnvironmentDetail,
  ProductPromotionDryRunResponse,
  ProductPromotionOperationAvailability,
  ProductPromotionStatus,
  ProductPromotionWorkflowDeliveryStatus,
  ProductPromotionWorkflowDispatchResponse,
} from "./generated/openapi.ts";

type PromotionDryRunRequest = StoredPromotionDirectRequest;
type PromotionWorkflowRequest = StoredPromotionWorkflowRequest;

export function ProductPromotionFlow({
  detail,
  fixtureMode,
  onRefresh,
  statusResource,
}: {
  detail: ProductEnvironmentDetail;
  fixtureMode: DevFixtureMode;
  onRefresh: () => void;
  statusResource: ResourceState<ProductPromotionStatus>;
}) {
  if (statusResource.status === "idle" || statusResource.status === "loading") {
    return <PromotionLoading />;
  }
  if (statusResource.status === "error" || !statusResource.data) {
    return (
      <section className="promotion-authority-unavailable" role="alert">
        <ShieldAlert aria-hidden="true" />
        <div>
          <p className="eyebrow">Promotion authority unavailable</p>
          <h2>Promotion controls are locked</h2>
          <p>
            {statusResource.error ||
              "Launchplane did not return current product-owned promotion evidence."}
          </p>
          {statusResource.traceId ? (
            <small>Trace {statusResource.traceId}</small>
          ) : null}
          <button className="secondary-button" onClick={onRefresh} type="button">
            <RefreshCw size={15} aria-hidden="true" />
            Refresh authority
          </button>
        </div>
      </section>
    );
  }
  return (
    <PromotionControl
      detail={detail}
      fixtureMode={fixtureMode}
      onRefresh={onRefresh}
      status={statusResource.data}
    />
  );
}

function PromotionControl({
  detail,
  fixtureMode,
  onRefresh,
  status,
}: {
  detail: ProductEnvironmentDetail;
  fixtureMode: DevFixtureMode;
  onRefresh: () => void;
  status: ProductPromotionStatus;
}) {
  const [initialDraft] = useState(() =>
    readPromotionDraft(detail.product, detail.environment),
  );
  const [bump, setBump] = useState<PromotionBump>(
    initialDraft?.bump ?? status.default_bump,
  );
  const [reason, setReason] = useState(initialDraft?.reason ?? "");
  const [localError, setLocalError] = useState("");
  const [directResult, setDirectResult] =
    useState<ProductPromotionDryRunResponse | null>(null);
  const [workflowResult, setWorkflowResult] =
    useState<ProductPromotionWorkflowDispatchResponse | null>(
      initialDraft?.acceptedWorkflow ?? null,
    );
  const [liveConfirmed, setLiveConfirmed] = useState(false);
  const [pendingDirectRequest, setPendingDirectRequest] =
    useState<PromotionDryRunRequest | null>(initialDraft?.pendingDirect ?? null);
  const [pendingWorkflowRequest, setPendingWorkflowRequest] =
    useState<PromotionWorkflowRequest | null>(
      initialDraft?.pendingWorkflow ?? null,
    );
  const [deliveryResource, setDeliveryResource] = useState<
    ResourceState<ProductPromotionWorkflowDeliveryStatus>
  >(emptyResource());
  const directOperation = useBrowserOperationController<
    PromotionDryRunRequest,
    ProductPromotionDryRunResponse
  >({
    execute: async (payload, options) => {
      if (fixtureMode) {
        const fixtures = await loadDevFixtures();
        options.onDispatch?.();
        return fixtures.dryRunProductPromotionForFixture(
          fixtureMode,
          detail.product,
          detail.environment,
          payload,
          options.signal,
        );
      }
      return dryRunProductPromotion(
        detail.product,
        detail.environment,
        payload,
        options,
      );
    },
    failureCertainty: promotionFailureCertainty,
    failureFor: promotionOperationFailure,
    scope: `${detail.product}:${detail.environment}:promotion:direct-dry-run`,
  });
  const workflowOperation = useBrowserOperationController<
    PromotionWorkflowRequest,
    ProductPromotionWorkflowDispatchResponse
  >({
    execute: async (payload, options) => {
      if (fixtureMode) {
        const fixtures = await loadDevFixtures();
        options.onDispatch?.();
        return fixtures.dispatchProductPromotionWorkflowForFixture(
          fixtureMode,
          detail.product,
          detail.environment,
          payload,
          options.signal,
        );
      }
      return dispatchProductPromotionWorkflow(
        detail.product,
        detail.environment,
        payload,
        options,
      );
    },
    failureCertainty: promotionFailureCertainty,
    failureFor: promotionOperationFailure,
    scope: `${detail.product}:${detail.environment}:promotion:workflow`,
  });
  const continuityRetry =
    workflowOperation.state.requiresIdempotencyContinuity && pendingWorkflowRequest
      ? pendingWorkflowRequest
      : null;
  const directContinuityRetry =
    directOperation.state.requiresIdempotencyContinuity && pendingDirectRequest
      ? pendingDirectRequest
      : null;
  const continuityLocked =
    directOperation.state.requiresIdempotencyContinuity ||
    workflowOperation.state.requiresIdempotencyContinuity;
  const directPlanMatches = promotionPlanMatchesStatus(directResult, status, bump);
  const planMatches = directPlanMatches || continuityRetry !== null;
  const operationBusy =
    isOperationBusy(directOperation.state) || isOperationBusy(workflowOperation.state);
  const acceptedDeliveryId = workflowResult?.result.delivery_id ?? "";
  const acceptedDeliveryUnresolved =
    workflowResult !== null &&
    (deliveryResource.status !== "ready" ||
      deliveryResource.data === null ||
      promotionDeliveryNeedsRefresh(deliveryResource.data));
  const runUrl = safeExternalUrl(deliveryResource.data?.run_url ?? "");

  useEffect(() => {
    writePromotionDraft(detail.product, detail.environment, {
      acceptedWorkflow: workflowResult,
      bump,
      reason,
      pendingDirect: pendingDirectRequest,
      pendingWorkflow: pendingWorkflowRequest,
    });
  }, [
    bump,
    detail.environment,
    detail.product,
    pendingDirectRequest,
    pendingWorkflowRequest,
    reason,
    workflowResult,
  ]);

  useEffect(() => {
    if (!acceptedDeliveryId || deliveryResource.status !== "idle") {
      return;
    }
    void refreshDelivery(acceptedDeliveryId);
  }, [acceptedDeliveryId, deliveryResource.status]);

  useEffect(() => {
    if (
      !acceptedDeliveryId ||
      deliveryResource.status === "idle" ||
      deliveryResource.status === "loading"
    ) {
      return;
    }
    if (
      deliveryResource.data !== null &&
      !promotionDeliveryNeedsRefresh(deliveryResource.data)
    ) {
      return;
    }
    const timeout = window.setTimeout(() => {
      void refreshDelivery(acceptedDeliveryId);
    }, deliveryResource.status === "error" ? 6000 : 3000);
    return () => window.clearTimeout(timeout);
  }, [
    acceptedDeliveryId,
    deliveryResource.data?.delivery_id,
    deliveryResource.data?.state,
    deliveryResource.status,
  ]);

  async function refreshDelivery(deliveryId = deliveryResource.data?.delivery_id ?? "") {
    if (!deliveryId) {
      return;
    }
    setDeliveryResource((current) => ({
      ...current,
      status: "loading",
      error: "",
      traceId: "",
      statusCode: 0,
    }));
    try {
      const response = fixtureMode
        ? await (await loadDevFixtures()).readProductPromotionWorkflowDeliveryForFixture(
            fixtureMode,
            detail.product,
            detail.environment,
            deliveryId,
          )
        : await readProductPromotionWorkflowDelivery(
            detail.product,
            detail.environment,
            deliveryId,
          );
      setDeliveryResource({
        data: response.delivery,
        error: "",
        status: "ready",
        statusCode: 200,
        traceId: response.trace_id,
      });
    } catch (error) {
      const failure = promotionOperationFailure(error);
      setDeliveryResource((current) => ({
        ...current,
        error: failure.message,
        status: "error",
        statusCode: failure.statusCode,
        traceId: failure.traceId,
      }));
    }
  }

  async function runDirectDryRun() {
    setLocalError("");
    if (acceptedDeliveryUnresolved) {
      setLocalError("Wait for the accepted workflow delivery to settle before starting over.");
      return;
    }
    if (directOperation.state.requiresIdempotencyContinuity) {
      if (!directContinuityRetry) {
        setLocalError("The exact uncertain direct dry-run request is unavailable.");
        return;
      }
      const response = await directOperation.run(directContinuityRetry);
      if (response) {
        setPendingDirectRequest(null);
        setDirectResult(response);
      }
      return;
    }
    if (!reason.trim()) {
      setLocalError("Enter a reason before running the direct dry-run.");
      return;
    }
    if (!status.direct_dry_run.enabled) {
      setLocalError("Current server evidence blocks the direct dry-run.");
      return;
    }
    if (workflowOperation.state.requiresIdempotencyContinuity) {
      setLocalError("Resolve the uncertain workflow dispatch before reviewing a new plan.");
      return;
    }
    workflowOperation.reset();
    setPendingWorkflowRequest(null);
    setWorkflowResult(null);
    setDeliveryResource(emptyResource());
    setLiveConfirmed(false);
    const directPayload: PromotionDryRunRequest = {
      schema_version: 1,
      reason: reason.trim(),
      evidence_fingerprint: status.evidence_fingerprint,
      bump,
    };
    setPendingDirectRequest(directPayload);
    writePromotionDraft(detail.product, detail.environment, {
      acceptedWorkflow: null,
      bump,
      reason,
      pendingDirect: directPayload,
      pendingWorkflow: null,
    });
    const response = await directOperation.run(directPayload);
    if (response) {
      setPendingDirectRequest(null);
      setDirectResult(response);
    }
  }

  async function dispatchWorkflow(dryRun: boolean) {
    setLocalError("");
    if (continuityRetry) {
      if (continuityRetry.dry_run !== dryRun) {
        setLocalError("Retry the exact uncertain workflow operation before starting another.");
        return;
      }
      const response = await workflowOperation.run(continuityRetry);
      if (response) {
        setPendingWorkflowRequest(null);
        setWorkflowResult(response);
        setLiveConfirmed(false);
        setDeliveryResource(emptyResource());
        writePromotionDraft(detail.product, detail.environment, {
          acceptedWorkflow: response,
          bump,
          reason,
          pendingDirect: pendingDirectRequest,
          pendingWorkflow: null,
        });
      }
      return;
    }
    if (acceptedDeliveryUnresolved) {
      setLocalError("Wait for the accepted workflow delivery to settle before dispatching again.");
      return;
    }
    if (!reason.trim()) {
      setLocalError("Enter a reason before dispatching the promotion workflow.");
      return;
    }
    if (!planMatches) {
      setLocalError("Run and accept the matching direct dry-run first.");
      return;
    }
    const availability = dryRun ? status.workflow_dry_run : status.workflow_live;
    if (!availability.enabled) {
      setLocalError("Current server evidence blocks this workflow dispatch.");
      return;
    }
    if (!dryRun && !liveConfirmed) {
      setLocalError("Confirm the reviewed release scope and side effects before live dispatch.");
      return;
    }
    const workflowPayload: PromotionWorkflowRequest = {
      schema_version: 1,
      dry_run: dryRun,
      reason: reason.trim(),
      evidence_fingerprint: status.evidence_fingerprint,
      bump,
      confirmation: dryRun ? "" : promotionLiveConfirmation(status, bump),
    };
    setPendingWorkflowRequest(workflowPayload);
    writePromotionDraft(detail.product, detail.environment, {
      acceptedWorkflow: null,
      bump,
      reason,
      pendingDirect: pendingDirectRequest,
      pendingWorkflow: workflowPayload,
    });
    const response = await workflowOperation.run(workflowPayload);
    if (response) {
      setPendingWorkflowRequest(null);
      setWorkflowResult(response);
      setLiveConfirmed(false);
      setDeliveryResource(emptyResource());
      writePromotionDraft(detail.product, detail.environment, {
        acceptedWorkflow: response,
        bump,
        reason,
        pendingDirect: pendingDirectRequest,
        pendingWorkflow: null,
      });
    }
  }

  function startOver() {
    if (acceptedDeliveryUnresolved) {
      setLocalError("Wait for the accepted workflow delivery to settle before starting over.");
      return;
    }
    if (!directOperation.reset() || !workflowOperation.reset()) {
      setLocalError("An uncertain request must retry with its existing operation key.");
      return;
    }
    setReason("");
    clearPromotionDraft(detail.product, detail.environment);
    setDirectResult(null);
    setPendingDirectRequest(null);
    setPendingWorkflowRequest(null);
    setWorkflowResult(null);
    setDeliveryResource(emptyResource());
    setLiveConfirmed(false);
    setLocalError("");
  }

  return (
    <section className="promotion-control" aria-labelledby="promotion-control-title">
      <header className="promotion-control-header">
        <span aria-hidden="true">
          <GitBranch />
        </span>
        <div>
          <p className="eyebrow">Production promotion</p>
          <h2 id="promotion-control-title">Review evidence, then dispatch the workflow</h2>
          <p>
            Direct execution is dry-run only. Live production changes happen only through
            the configured GitHub workflow after the exact reviewed evidence is confirmed.
          </p>
        </div>
        <EvidenceBadge state={status.trust_state} />
      </header>

      <PromotionEvidence status={status} />

      <div className="promotion-blocker-grid">
        <PromotionAvailability
          availability={status.direct_dry_run}
          label="Direct dry-run"
        />
        <PromotionAvailability
          availability={status.workflow_dry_run}
          label="Workflow dry-run"
        />
        <PromotionAvailability
          availability={status.workflow_live}
          label="Workflow live"
        />
      </div>

      <div className="promotion-form-grid">
        <label className="promotion-field promotion-field-wide">
          <span>Operator reason</span>
          <textarea
            disabled={operationBusy || continuityLocked || acceptedDeliveryUnresolved}
            onChange={(event) => setReason(event.target.value)}
            placeholder="Why is this artifact ready for production?"
            rows={3}
            value={reason}
          />
        </label>
        <label className="promotion-field">
          <span>Version bump</span>
          <select
            disabled={operationBusy || continuityLocked || acceptedDeliveryUnresolved}
            onChange={(event) => {
              setBump(event.target.value as PromotionBump);
              setLiveConfirmed(false);
            }}
            value={bump}
          >
            {status.bump_options.map((option) => (
              <option key={option} value={option}>
                {humanize(option)}
              </option>
            ))}
          </select>
        </label>
      </div>

      {localError ? <PromotionInlineError message={localError} /> : null}
      <PromotionOperationNotice label="Direct dry-run" state={directOperation.state} />
      <PromotionOperationNotice label="Workflow dispatch" state={workflowOperation.state} />

      <section className="promotion-step" data-step="1" id="promotion-direct-dry-run">
        <div className="promotion-step-heading">
          <span>1</span>
          <div>
            <h3>Verify current runtime evidence</h3>
            <p>
              Launchplane re-reads testing and production evidence and performs no live
              deployment or release mutation.
            </p>
          </div>
        </div>
        <div className="promotion-button-row">
          <button
            className="primary-button"
            disabled={
              operationBusy ||
              acceptedDeliveryUnresolved ||
              workflowOperation.state.requiresIdempotencyContinuity ||
              (!directContinuityRetry &&
                (!status.direct_dry_run.enabled || !reason.trim()))
            }
            onClick={() => void runDirectDryRun()}
            type="button"
          >
            {isOperationBusy(directOperation.state) ? (
              <LoaderCircle className="spin" size={16} aria-hidden="true" />
            ) : (
              <ShieldCheck size={16} aria-hidden="true" />
            )}
            {directContinuityRetry
              ? "Retry uncertain direct dry-run"
              : "Run direct dry-run"}
          </button>
          {isOperationBusy(directOperation.state) ? (
            <button
              className="secondary-button"
              onClick={directOperation.cancel}
              type="button"
            >
              <XCircle size={15} aria-hidden="true" />
              Stop waiting
            </button>
          ) : null}
        </div>
        {directResult ? (
          <DirectDryRunResult
            matches={directPlanMatches}
            response={directResult}
            status={status}
          />
        ) : null}
      </section>

      <section className="promotion-step" data-step="2" id="promotion-workflow-dispatch">
        <div className="promotion-step-heading">
          <span>2</span>
          <div>
            <h3>Dispatch the product-owned workflow</h3>
            <p>
              Dispatch acceptance stays pending until the outbox worker observes a GitHub
              run. It never claims that the run or production promotion completed.
            </p>
          </div>
        </div>
        {!planMatches ? (
          <div className="promotion-plan-required">
            <AlertTriangle aria-hidden="true" />
            <span>A matching direct dry-run for the selected bump is required.</span>
          </div>
        ) : null}
        <LiveConfirmation
          bump={bump}
          checked={liveConfirmed}
          disabled={
            operationBusy ||
            acceptedDeliveryUnresolved ||
            continuityRetry !== null ||
            !planMatches ||
            !status.workflow_live.enabled
          }
          onChange={setLiveConfirmed}
          status={status}
        />
        <div className="promotion-button-row">
          <button
            className="secondary-button"
            disabled={
              operationBusy ||
              acceptedDeliveryUnresolved ||
              (continuityRetry
                ? !continuityRetry.dry_run
                : !planMatches || !status.workflow_dry_run.enabled)
            }
            onClick={() => void dispatchWorkflow(true)}
            type="button"
          >
            <GitBranch size={16} aria-hidden="true" />
            {continuityRetry?.dry_run
              ? "Retry uncertain workflow dry-run"
              : "Dispatch workflow dry-run"}
          </button>
          <button
            className="danger-button"
            disabled={
              operationBusy ||
              acceptedDeliveryUnresolved ||
              (continuityRetry
                ? continuityRetry.dry_run
                : !planMatches || !status.workflow_live.enabled || !liveConfirmed)
            }
            onClick={() => void dispatchWorkflow(false)}
            type="button"
          >
            {isOperationBusy(workflowOperation.state) ? (
              <LoaderCircle className="spin" size={16} aria-hidden="true" />
            ) : (
              <SquareCheckBig size={16} aria-hidden="true" />
            )}
            {continuityRetry && !continuityRetry.dry_run
              ? "Retry uncertain live promotion"
              : "Dispatch live promotion"}
          </button>
          {isOperationBusy(workflowOperation.state) ? (
            <button
              className="secondary-button"
              onClick={workflowOperation.cancel}
              type="button"
            >
              <XCircle size={15} aria-hidden="true" />
              Stop waiting
            </button>
          ) : null}
        </div>
        {workflowResult ? (
          <WorkflowDispatchResult
            deliveryResource={deliveryResource}
            onRefresh={() => void refreshDelivery()}
            response={workflowResult}
            runUrl={runUrl}
          />
        ) : null}
      </section>

      <div className="promotion-footer-actions">
        <button className="secondary-button" onClick={onRefresh} type="button">
          <RefreshCw size={15} aria-hidden="true" />
          Refresh evidence
        </button>
        <button
          className="text-button"
          disabled={acceptedDeliveryUnresolved}
          onClick={startOver}
          type="button"
        >
          <RotateCcw size={14} aria-hidden="true" />
          Start over
        </button>
      </div>
    </section>
  );
}

function PromotionEvidence({ status }: { status: ProductPromotionStatus }) {
  return (
    <section className="promotion-evidence" aria-label="Reviewed promotion evidence">
      <div className="promotion-evidence-heading">
        <div>
          <p className="eyebrow">Server-owned inputs</p>
          <h3>{status.display_name}</h3>
        </div>
        <code>{status.evidence_fingerprint.slice(0, 16)}…</code>
      </div>
      <dl>
        <div>
          <dt>Source artifact</dt>
          <dd className="promotion-code-value">{status.source.artifact_id || "Missing"}</dd>
        </div>
        <div>
          <dt>Source revision</dt>
          <dd className="promotion-code-value">{status.source.source_git_ref || "Missing"}</dd>
        </div>
        <div>
          <dt>Testing evidence</dt>
          <dd>
            {humanize(status.source.health_status)} health · {humanize(status.source.runtime_identity_status)} identity
          </dd>
        </div>
        <div>
          <dt>Production lane</dt>
          <dd>{status.destination_environment}</dd>
        </div>
        <div>
          <dt>Workflow</dt>
          <dd className="promotion-code-value">
            {status.repository} · {status.workflow_id}@{status.workflow_ref}
          </dd>
        </div>
        <div>
          <dt>Driver</dt>
          <dd>{status.driver_id}{status.base_driver_id ? ` · base ${status.base_driver_id}` : ""}</dd>
        </div>
      </dl>
    </section>
  );
}

function PromotionAvailability({
  availability,
  label,
}: {
  availability: ProductPromotionOperationAvailability;
  label: string;
}) {
  return (
    <article className="promotion-availability" data-enabled={availability.enabled}>
      <div>
        {availability.enabled ? (
          <CheckCircle2 aria-hidden="true" />
        ) : (
          <AlertTriangle aria-hidden="true" />
        )}
        <strong>{label}</strong>
      </div>
      <span>{availability.enabled ? "Available" : "Blocked"}</span>
      {availability.disabled_reasons.length ? (
        <ul>
          {availability.disabled_reasons.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      ) : (
        <small>{availability.authz_action}</small>
      )}
    </article>
  );
}

function DirectDryRunResult({
  matches,
  response,
  status,
}: {
  matches: boolean;
  response: ProductPromotionDryRunResponse;
  status: ProductPromotionStatus;
}) {
  return (
    <div className="promotion-result" data-status={matches ? "accepted" : "stale"}>
      <div className="promotion-result-title">
        {matches ? <CheckCircle2 aria-hidden="true" /> : <AlertTriangle aria-hidden="true" />}
        <div>
          <strong>{matches ? "Direct dry-run accepted" : "Dry-run no longer matches"}</strong>
          <p>
            {matches
              ? "The reviewed artifact and evidence fingerprint are locked for this bump mode."
              : "The selected bump or current evidence changed. Run a new direct dry-run."}
          </p>
        </div>
      </div>
      <dl>
        <div><dt>Artifact</dt><dd>{response.result.artifact_id}</dd></div>
        <div><dt>Revision</dt><dd>{response.result.source_git_ref}</dd></div>
        <div><dt>Promotion</dt><dd>{humanize(response.result.promotion_status)}</dd></div>
        <div><dt>Evidence</dt><dd>{status.evidence_fingerprint.slice(0, 16)}…</dd></div>
      </dl>
      <OperationReceipt response={response} />
    </div>
  );
}

function LiveConfirmation({
  bump,
  checked,
  disabled,
  onChange,
  status,
}: {
  bump: PromotionBump;
  checked: boolean;
  disabled: boolean;
  onChange: (checked: boolean) => void;
  status: ProductPromotionStatus;
}) {
  return (
    <label className="promotion-live-confirmation" data-disabled={disabled}>
      <input
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        type="checkbox"
      />
      <span aria-hidden="true">
        {checked ? <SquareCheckBig /> : <Square />}
      </span>
      <span>
        <strong>I confirm this live production promotion</strong>
        <small>
          Product <b>{status.product}</b> · artifact <code>{status.source.artifact_id}</code> ·
          revision <code>{status.source.source_git_ref}</code> · destination <b>prod</b> ·
          bump <b>{bump}</b>.
        </small>
        <small>
          Side effects may include a GitHub release/tag and deployment of the reviewed
          immutable artifact to production.
        </small>
        <code>{promotionLiveConfirmation(status, bump)}</code>
      </span>
    </label>
  );
}

function WorkflowDispatchResult({
  deliveryResource,
  onRefresh,
  response,
  runUrl,
}: {
  deliveryResource: ResourceState<ProductPromotionWorkflowDeliveryStatus>;
  onRefresh: () => void;
  response: ProductPromotionWorkflowDispatchResponse;
  runUrl: URL | null;
}) {
  const delivery = deliveryResource.data;
  return (
    <div className="promotion-result" data-status="pending">
      <div className="promotion-result-title">
        <GitBranch aria-hidden="true" />
        <div>
          <strong>Workflow dispatch accepted</strong>
          <p>
            Launchplane recorded a pending outbox delivery. This is not a completion claim.
          </p>
        </div>
      </div>
      <dl>
        <div><dt>Mode</dt><dd>{response.result.dry_run ? "Workflow dry-run" : "Live"}</dd></div>
        <div><dt>Dispatch</dt><dd>{humanize(delivery?.dispatch_status ?? response.result.dispatch_status)}</dd></div>
        <div><dt>Run</dt><dd>{humanize(delivery?.run_status ?? response.result.run_status)}</dd></div>
        <div><dt>Delivery</dt><dd>{response.result.delivery_id}</dd></div>
        <div><dt>Run conclusion</dt><dd>{humanize(delivery?.run_conclusion || "pending")}</dd></div>
        <div><dt>Observation</dt><dd>{humanize(delivery?.run_observation_status ?? "pending")}</dd></div>
      </dl>
      {deliveryResource.status === "error" ? (
        <PromotionInlineError message={deliveryResource.error} />
      ) : null}
      {delivery?.error_code ? (
        <PromotionInlineError
          message={`Workflow delivery requires attention: ${delivery.error_code}`}
        />
      ) : null}
      <div className="promotion-button-row">
        <button className="secondary-button" onClick={onRefresh} type="button">
          {deliveryResource.status === "loading" ? (
            <LoaderCircle className="spin" size={15} aria-hidden="true" />
          ) : (
            <RefreshCw size={15} aria-hidden="true" />
          )}
          Refresh dispatch status
        </button>
        {runUrl ? (
          <a className="secondary-button" href={runUrl.href} rel="noreferrer" target="_blank">
            View GitHub run
            <ExternalLink size={13} aria-hidden="true" />
          </a>
        ) : null}
      </div>
      <OperationReceipt response={response} />
    </div>
  );
}

function OperationReceipt({
  response,
}: {
  response: ProductPromotionDryRunResponse | ProductPromotionWorkflowDispatchResponse;
}) {
  return (
    <div className="promotion-receipt">
      <span>Trace <code>{response.trace_id}</code></span>
      <span>{response.replayed ? "Replayed request" : "Original request"}</span>
      {response.original_trace_id ? (
        <span>Original trace <code>{response.original_trace_id}</code></span>
      ) : null}
    </div>
  );
}

function PromotionOperationNotice({
  label,
  state,
}: {
  label: string;
  state: BrowserOperationState;
}) {
  if (!state.failure && state.phase !== "uncertain" && state.phase !== "cancelled") {
    return null;
  }
  return (
    <div className="operation-notice" data-phase={state.phase} role="status">
      <AlertTriangle aria-hidden="true" />
      <div>
        <strong>{label} {state.phase}</strong>
        <p>
          {state.failure?.message ||
            (state.phase === "cancelled"
              ? "The request was cancelled before dispatch."
              : "The result is uncertain. Retry only with the existing operation key.")}
        </p>
        {state.failure?.code ? <code>{state.failure.code}</code> : null}
        {state.failure?.traceId ? <small>Trace {state.failure.traceId}</small> : null}
      </div>
    </div>
  );
}

function PromotionInlineError({ message }: { message: string }) {
  return (
    <div className="promotion-inline-error" role="alert">
      <AlertTriangle aria-hidden="true" />
      <span>{message}</span>
    </div>
  );
}

function PromotionLoading() {
  return (
    <section className="promotion-loading" aria-live="polite">
      <LoaderCircle className="spin" aria-hidden="true" />
      <div>
        <p className="eyebrow">Production promotion</p>
        <h2>Reading current promotion authority</h2>
        <p>Testing and production evidence must settle before controls can render.</p>
      </div>
    </section>
  );
}

function isOperationBusy(state: BrowserOperationState): boolean {
  return state.phase === "queued" || state.phase === "submitting";
}
