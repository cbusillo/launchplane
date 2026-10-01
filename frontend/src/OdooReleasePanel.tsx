import {
  AlertTriangle,
  CheckCircle2,
  Circle,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
  Rocket,
  Square,
  SquareCheckBig,
  XCircle,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import {
  enqueueProductionBackupGate,
  readProductionBackupGateOperation,
  readReleaseReview,
  rollBackOdooProd,
  runOdooProdPromotion,
} from "./api";
import type { BrowserOperationState } from "./browser-operation";
import {
  createOdooReleaseAttempt,
  odooReleaseFailure,
  readOdooReleaseAttempt,
  releaseReviewAllowsPromotion,
  runOdooRelease,
  waitFor,
  writeOdooReleaseAttempt,
  type OdooReleaseOutcome,
  type OdooReleaseProgress,
  type OdooReleaseScope,
  type OdooReleaseStep,
} from "./odoo-release-operation";
import { promotionFailureCertainty, promotionOperationFailure } from "./promotion-operation";
import { emptyResource, type ResourceState } from "./resource";
import { useBrowserOperationController } from "./use-browser-operation";

import type {
  ProductEnvironmentDetail,
  ReleaseReviewResponse,
  WriteOdooProdRollbackData,
  WriteOdooProdRollbackResponse,
} from "./generated/openapi.ts";

const STEP_LABELS: Record<OdooReleaseStep, string> = {
  review: "Release review",
  backup: "Infrastructure backup",
  promote: "Promote to production",
};
const STEPS: OdooReleaseStep[] = ["review", "backup", "promote"];

type StepProgress = Partial<Record<OdooReleaseStep, OdooReleaseProgress>>;
type RollbackPayload = WriteOdooProdRollbackData["body"];

export function OdooReleasePanel({
  detail,
  fixtureMode,
  onRefresh,
}: {
  detail: ProductEnvironmentDetail;
  fixtureMode: boolean;
  onRefresh: () => void;
}) {
  const scope: OdooReleaseScope = {
    context: detail.context,
    environment: detail.environment,
    product: detail.product,
  };
  return (
    <section className="promotion-control" aria-labelledby="odoo-release-title">
      <header className="promotion-control-header">
        <span aria-hidden="true">
          <Rocket />
        </span>
        <div>
          <p className="eyebrow">Production release</p>
          <h2 id="odoo-release-title">Promote the testing artifact, or roll back</h2>
          <p>
            Promoting reads the release review, takes and verifies an infrastructure
            backup, then promotes the testing artifact. Launchplane refuses the promotion
            without an approved release and that verified backup.
          </p>
        </div>
      </header>
      {fixtureMode ? (
        <ReleaseInlineError message="Release controls are off in fixture mode." />
      ) : (
        <>
          <PromoteSection onRefresh={onRefresh} scope={scope} />
          <RollbackSection detail={detail} onRefresh={onRefresh} scope={scope} />
        </>
      )}
    </section>
  );
}

function PromoteSection({
  onRefresh,
  scope,
}: {
  onRefresh: () => void;
  scope: OdooReleaseScope;
}) {
  const [review, setReview] = useState<ResourceState<ReleaseReviewResponse>>(emptyResource());
  const [pendingAttempt, setPendingAttempt] = useState(() => readOdooReleaseAttempt(scope));
  const [progress, setProgress] = useState<StepProgress>({});
  const [outcome, setOutcome] = useState<OdooReleaseOutcome | null>(null);
  const [running, setRunning] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    void refreshReview();
    return () => abortRef.current?.abort();
  }, [scope.product]);

  async function refreshReview() {
    setReview((current) => ({ ...current, status: "loading", error: "" }));
    try {
      const response = await readReleaseReview(scope.product);
      setReview({ data: response, error: "", status: "ready", statusCode: 200, traceId: response.trace_id });
    } catch (error) {
      const failure = promotionOperationFailure(error);
      setReview({
        data: null,
        error: failure.message,
        status: "error",
        statusCode: failure.statusCode,
        traceId: failure.traceId,
      });
    }
  }

  async function promote() {
    if (running || (!pendingAttempt && !confirmed)) {
      return;
    }
    const attempt = pendingAttempt ?? createOdooReleaseAttempt();
    writeOdooReleaseAttempt(scope, attempt);
    setPendingAttempt(attempt);
    setProgress({});
    setOutcome(null);
    setRunning(true);
    const controller = new AbortController();
    abortRef.current = controller;
    const result = await runOdooRelease({
      attempt,
      dependencies: {
        enqueueBackup: enqueueProductionBackupGate,
        promote: runOdooProdPromotion,
        readBackup: readProductionBackupGateOperation,
        readReleaseReview,
        wait: waitFor,
      },
      onProgress: (update) =>
        setProgress((current) => ({ ...current, [update.step]: update })),
      scope,
      signal: controller.signal,
    });
    abortRef.current = null;
    setRunning(false);
    setOutcome(result);
    if (result.status === "stopped" && result.certainty === "uncertain") {
      return;
    }
    writeOdooReleaseAttempt(scope, null);
    setPendingAttempt(null);
    setConfirmed(false);
    if (result.status === "promoted") {
      onRefresh();
      void refreshReview();
    }
  }

  const reviewData = review.data;
  const approved = reviewData ? releaseReviewAllowsPromotion(reviewData) : false;
  return (
    <section className="promotion-step" data-step="1" id="odoo-release-promote">
      <div className="promotion-step-heading">
        <span>1</span>
        <div>
          <h3>Promote testing to production</h3>
          <p>One action runs the three steps below in order and stops at the first refusal.</p>
        </div>
      </div>
      <div className="odoo-release-review" data-approved={approved}>
        {review.status === "loading" ? (
          <LoaderCircle className="spin" size={16} aria-hidden="true" />
        ) : approved ? (
          <CheckCircle2 size={16} aria-hidden="true" />
        ) : (
          <AlertTriangle size={16} aria-hidden="true" />
        )}
        <span>{releaseReviewSummary(review)}</span>
        <button className="text-button" disabled={review.status === "loading"} onClick={() => void refreshReview()} type="button">
          <RefreshCw size={14} aria-hidden="true" />
          Refresh
        </button>
      </div>
      {reviewData && !approved && reviewData.review.blockers.length ? (
        <ul className="odoo-release-blockers">
          {reviewData.review.blockers.map((blocker) => (
            <li key={blocker}>{blocker}</li>
          ))}
        </ul>
      ) : null}
      <ol className="odoo-release-steps" aria-label="Release steps">
        {STEPS.map((step) => (
          <ReleaseStepRow key={step} progress={progress[step]} step={step} />
        ))}
      </ol>
      {outcome?.status === "stopped" ? (
        <ReleaseOutcomeNotice outcome={outcome} />
      ) : null}
      {outcome?.status === "promoted" ? (
        <div className="promotion-result" data-status="accepted">
          <div className="promotion-result-title">
            <CheckCircle2 aria-hidden="true" />
            <div>
              <strong>Production promoted</strong>
              <p>Backup {outcome.backupRecordId} was verified before the promotion.</p>
            </div>
          </div>
          <dl>
            <div><dt>Artifact</dt><dd>{resultText(outcome.response.result, "artifact_id")}</dd></div>
            <div><dt>Promotion record</dt><dd>{resultText(outcome.response.result, "promotion_record_id")}</dd></div>
            <div><dt>Post-deploy</dt><dd>{resultText(outcome.response.result, "post_deploy_status")}</dd></div>
          </dl>
        </div>
      ) : null}
      {pendingAttempt ? null : (
        <ConfirmationBox
          checked={confirmed}
          disabled={running}
          label="I confirm promoting the testing artifact to production"
          detail={`Product ${scope.product} · ${scope.context}/${scope.environment}. Launchplane takes a backup first and refuses without one.`}
          onChange={setConfirmed}
        />
      )}
      <div className="promotion-button-row">
        <button
          className="danger-button"
          disabled={running || (!pendingAttempt && !confirmed)}
          onClick={() => void promote()}
          type="button"
        >
          {running ? (
            <LoaderCircle className="spin" size={16} aria-hidden="true" />
          ) : (
            <Rocket size={16} aria-hidden="true" />
          )}
          {pendingAttempt && !running ? "Retry the same release" : "Promote to production"}
        </button>
        {running ? (
          <button className="secondary-button" onClick={() => abortRef.current?.abort()} type="button">
            <XCircle size={15} aria-hidden="true" />
            Stop waiting
          </button>
        ) : null}
      </div>
    </section>
  );
}

function RollbackSection({
  detail,
  onRefresh,
  scope,
}: {
  detail: ProductEnvironmentDetail;
  onRefresh: () => void;
  scope: OdooReleaseScope;
}) {
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [pendingPayload, setPendingPayload] = useState<RollbackPayload | null>(null);
  const [response, setResponse] = useState<WriteOdooProdRollbackResponse | null>(null);
  const rollback = useBrowserOperationController<RollbackPayload, WriteOdooProdRollbackResponse>({
    execute: rollBackOdooProd,
    failureCertainty: promotionFailureCertainty,
    failureFor: odooReleaseFailure,
    scope: `${scope.product}:${scope.environment}:odoo-rollback`,
  });
  const rollbackArtifact = detail.driver_extensions.odoo?.rollback_artifact_id ?? "";
  const busy = rollback.state.phase === "queued" || rollback.state.phase === "submitting";
  const continuityRetry = rollback.state.requiresIdempotencyContinuity ? pendingPayload : null;
  const canSubmit =
    !busy && (continuityRetry !== null || (!!rollbackArtifact && confirmed && !!reason.trim()));

  async function submit() {
    const payload: RollbackPayload = continuityRetry ?? {
      product: scope.product,
      rollback: {
        context: scope.context,
        instance: scope.environment,
        reason: reason.trim(),
        schema_version: 1,
      },
      schema_version: 1,
    };
    setPendingPayload(payload);
    setResponse(null);
    const result = await rollback.run(payload);
    if (result) {
      setResponse(result);
      setPendingPayload(null);
      setConfirmed(false);
      onRefresh();
    }
  }

  const result = response?.result ?? null;
  return (
    <section className="promotion-step" data-step="2" id="odoo-release-rollback">
      <div className="promotion-step-heading">
        <span>2</span>
        <div>
          <h3>Roll production back</h3>
          <p>
            Redeploys the artifact of the previous passing production deployment. It
            changes the image only; it does not restore data.
          </p>
        </div>
      </div>
      <dl className="odoo-release-facts">
        <div>
          <dt>Rolls back to</dt>
          <dd className="promotion-code-value">
            {rollbackArtifact || "No earlier passing production deployment"}
          </dd>
        </div>
        {detail.driver_extensions.odoo?.rollback_deployment_record_id ? (
          <div>
            <dt>From deployment</dt>
            <dd className="promotion-code-value">
              {detail.driver_extensions.odoo.rollback_deployment_record_id}
            </dd>
          </div>
        ) : null}
      </dl>
      <label className="promotion-field promotion-field-wide">
        <span>Reason</span>
        <textarea
          disabled={busy || continuityRetry !== null || !rollbackArtifact}
          onChange={(event) => setReason(event.target.value)}
          placeholder="Why is production rolling back?"
          rows={2}
          value={reason}
        />
      </label>
      {continuityRetry ? null : (
        <ConfirmationBox
          checked={confirmed}
          disabled={busy || !rollbackArtifact}
          label="I confirm rolling production back"
          detail={`Product ${scope.product} · ${scope.context}/${scope.environment} → ${rollbackArtifact || "no target"}.`}
          onChange={setConfirmed}
        />
      )}
      <OperationNotice label="Rollback" state={rollback.state} />
      {result ? (
        <div className="promotion-result" data-status={result.rollback_status === "pass" ? "accepted" : "stale"}>
          <div className="promotion-result-title">
            {result.rollback_status === "pass" ? <CheckCircle2 aria-hidden="true" /> : <AlertTriangle aria-hidden="true" />}
            <div>
              <strong>Rollback {resultText(result, "rollback_status")}</strong>
              <p>{resultText(result, "error_message", "")}</p>
            </div>
          </div>
          <dl>
            <div><dt>Artifact</dt><dd>{resultText(result, "artifact_id")}</dd></div>
            <div><dt>Health</dt><dd>{resultText(result, "rollback_health_status")}</dd></div>
            <div><dt>Post-deploy</dt><dd>{resultText(result, "post_deploy_status")}</dd></div>
          </dl>
        </div>
      ) : null}
      <div className="promotion-button-row">
        <button className="danger-button" disabled={!canSubmit} onClick={() => void submit()} type="button">
          {busy ? (
            <LoaderCircle className="spin" size={16} aria-hidden="true" />
          ) : (
            <RotateCcw size={16} aria-hidden="true" />
          )}
          {continuityRetry ? "Retry the same rollback" : "Roll back production"}
        </button>
        {busy ? (
          <button className="secondary-button" onClick={rollback.cancel} type="button">
            <XCircle size={15} aria-hidden="true" />
            Stop waiting
          </button>
        ) : null}
      </div>
    </section>
  );
}

function ReleaseStepRow({
  progress,
  step,
}: {
  progress: OdooReleaseProgress | undefined;
  step: OdooReleaseStep;
}) {
  const state = progress?.state ?? "waiting";
  return (
    <li data-state={state}>
      {state === "running" ? (
        <LoaderCircle className="spin" size={16} aria-hidden="true" />
      ) : state === "passed" ? (
        <CheckCircle2 size={16} aria-hidden="true" />
      ) : state === "failed" ? (
        <XCircle size={16} aria-hidden="true" />
      ) : (
        <Circle size={16} aria-hidden="true" />
      )}
      <strong>{STEP_LABELS[step]}</strong>
      <span>{progress?.detail ?? "Not started."}</span>
    </li>
  );
}

function ReleaseOutcomeNotice({
  outcome,
}: {
  outcome: Extract<OdooReleaseOutcome, { status: "stopped" }>;
}) {
  return (
    <div className="operation-notice" data-phase={outcome.certainty === "uncertain" ? "uncertain" : "failed"} role="alert">
      <AlertTriangle aria-hidden="true" />
      <div>
        <strong>Stopped at {STEP_LABELS[outcome.step].toLowerCase()}</strong>
        <p>{outcome.failure.message}</p>
        {outcome.certainty === "uncertain" && outcome.step === "promote" ? (
          <p>
            The promotion may still be running on the server. Check the latest prod
            deployment in Activity before retrying: the server replays only a finished
            promotion, so a retry while one runs starts a second attempt.
          </p>
        ) : outcome.certainty === "uncertain" ? (
          <p>The result is uncertain. Retrying reuses the same backup request.</p>
        ) : null}
        {outcome.failure.code ? <code>{outcome.failure.code}</code> : null}
        {outcome.failure.traceId ? <small>Trace {outcome.failure.traceId}</small> : null}
      </div>
    </div>
  );
}

function OperationNotice({ label, state }: { label: string; state: BrowserOperationState }) {
  if (!state.failure) {
    return null;
  }
  return (
    <div className="operation-notice" data-phase={state.phase} role="alert">
      <AlertTriangle aria-hidden="true" />
      <div>
        <strong>{label} {state.phase}</strong>
        <p>{state.failure.message}</p>
        {state.failure.code ? <code>{state.failure.code}</code> : null}
        {state.failure.traceId ? <small>Trace {state.failure.traceId}</small> : null}
      </div>
    </div>
  );
}

function ConfirmationBox({
  checked,
  detail,
  disabled,
  label,
  onChange,
}: {
  checked: boolean;
  detail: string;
  disabled: boolean;
  label: string;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="promotion-live-confirmation" data-disabled={disabled}>
      <input checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} type="checkbox" />
      <span aria-hidden="true">{checked ? <SquareCheckBig /> : <Square />}</span>
      <span>
        <strong>{label}</strong>
        <small>{detail}</small>
      </span>
    </label>
  );
}

function ReleaseInlineError({ message }: { message: string }) {
  return (
    <div className="promotion-inline-error" role="alert">
      <AlertTriangle aria-hidden="true" />
      <span>{message}</span>
    </div>
  );
}

function releaseReviewSummary(review: ResourceState<ReleaseReviewResponse>): string {
  if (review.status === "idle" || review.status === "loading") {
    return "Reading the release review…";
  }
  if (review.status === "error" || !review.data) {
    return `Release review unavailable: ${review.error || "no response"}`;
  }
  if (!review.data.review.required) {
    return "No release approval is required.";
  }
  return review.data.review.approved
    ? "The release is approved."
    : "The release is not approved yet; the promotion will stop at the review.";
}

function resultText(
  result: { [key: string]: unknown } | null | undefined,
  key: string,
  fallback = "—",
): string {
  const value = result?.[key];
  return typeof value === "string" && value ? value : fallback;
}
