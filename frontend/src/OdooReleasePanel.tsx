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
  enqueueOdooProdPromotion,
  enqueueProductionBackupGate,
  readOdooProdPromotionOperation,
  readProductionBackupGateOperation,
  enqueueOdooProdRollback,
  readOdooProdRollbackOperation,
  readReleaseReview,
} from "./api";
import {
  createOdooReleaseAttempt,
  createOdooRollbackAttempt,
  readOdooReleaseAttempt,
  readOdooRollbackAttempt,
  rollbackPhaseDetail,
  runOdooRollback,
  writeOdooRollbackAttempt,
  type OdooRollbackOutcome,
  releaseReviewAllowsPromotion,
  runOdooRelease,
  waitFor,
  writeOdooReleaseAttempt,
  type OdooReleaseOutcome,
  type OdooReleaseProgress,
  type OdooReleaseScope,
  type OdooReleaseStep,
} from "./odoo-release-operation";
import { promotionOperationFailure } from "./promotion-operation";
import { emptyResource, type ResourceState } from "./resource";

import type {
  ProductEnvironmentDetail,
  ReleaseReviewResponse,
  OdooProdRollbackOperationView,
} from "./generated/openapi.ts";

const STEP_LABELS: Record<OdooReleaseStep, string> = {
  review: "Release review",
  backup: "Infrastructure backup",
  promote: "Promote to production",
};
const STEPS: OdooReleaseStep[] = ["review", "backup", "promote"];

type StepProgress = Partial<Record<OdooReleaseStep, OdooReleaseProgress>>;

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
  // Each run gets a number; a run that a newer one replaced (a remount, a
  // resume) does not write its stale outcome over the newer run's state.
  const runNumberRef = useRef(0);

  useEffect(() => {
    void refreshReview();
    // A promotion queued before a reload keeps running on the server; resume
    // watching it. Watching only reads, so it needs no new confirmation.
    if (readOdooReleaseAttempt(scope)?.promotionOperationId) {
      void promote();
    }
    return () => {
      abortRef.current?.abort();
      abortRef.current = null;
    };
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
    const storedAttempt = readOdooReleaseAttempt(scope);
    if (abortRef.current || (!storedAttempt && !confirmed)) {
      return;
    }
    let attempt = storedAttempt ?? createOdooReleaseAttempt();
    writeOdooReleaseAttempt(scope, attempt);
    setPendingAttempt(attempt);
    setProgress({});
    setOutcome(null);
    setRunning(true);
    const controller = new AbortController();
    abortRef.current = controller;
    const runNumber = ++runNumberRef.current;
    const result = await runOdooRelease({
      attempt,
      dependencies: {
        enqueueBackup: enqueueProductionBackupGate,
        enqueuePromotion: enqueueOdooProdPromotion,
        readBackup: readProductionBackupGateOperation,
        readPromotion: readOdooProdPromotionOperation,
        readReleaseReview,
        wait: waitFor,
      },
      onProgress: (update) => {
        if (runNumber === runNumberRef.current) {
          setProgress((current) => ({ ...current, [update.step]: update }));
        }
      },
      onPromotionQueued: (operationId) => {
        attempt = { ...attempt, promotionOperationId: operationId };
        writeOdooReleaseAttempt(scope, attempt);
        if (runNumber === runNumberRef.current) {
          setPendingAttempt(attempt);
        }
      },
      scope,
      signal: controller.signal,
    });
    if (abortRef.current === controller) {
      abortRef.current = null;
    }
    if (runNumber !== runNumberRef.current) {
      return;
    }
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
              <p>
                Backup {outcome.result.infrastructure_backup_record_id || "—"} was verified
                before the promotion (operation {outcome.operation.operation_id}).
              </p>
            </div>
          </div>
          <dl>
            <div><dt>Artifact</dt><dd>{resultText(outcome.result, "artifact_id")}</dd></div>
            <div><dt>Deployment record</dt><dd>{resultText(outcome.result, "deployment_record_id")}</dd></div>
            <div><dt>Promotion record</dt><dd>{resultText(outcome.result, "promotion_record_id")}</dd></div>
            <div><dt>Post-deploy</dt><dd>{resultText(outcome.result, "post_deploy_status")}</dd></div>
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
          {running
            ? "Promoting…"
            : pendingAttempt?.promotionOperationId
              ? "Resume watching the promotion"
              : pendingAttempt
                ? "Retry the same release"
                : "Promote to production"}
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
  const [pendingAttempt, setPendingAttempt] = useState(() => readOdooRollbackAttempt(scope));
  const [operation, setOperation] = useState<OdooProdRollbackOperationView | null>(null);
  const [outcome, setOutcome] = useState<OdooRollbackOutcome | null>(null);
  const [running, setRunning] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const runNumberRef = useRef(0);
  const rollbackArtifact = detail.driver_extensions.odoo?.rollback_artifact_id ?? "";
  const canSubmit =
    !running && (pendingAttempt !== null || (!!rollbackArtifact && confirmed && !!reason.trim()));

  useEffect(() => {
    // A rollback queued before a reload keeps running on the server; resume watching it.
    if (readOdooRollbackAttempt(scope)?.operationId) {
      void submit();
    }
    return () => {
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, [scope.product]);

  async function submit() {
    const storedAttempt = readOdooRollbackAttempt(scope);
    if (abortRef.current || (!storedAttempt && !(confirmed && reason.trim()))) {
      return;
    }
    let attempt = storedAttempt ?? createOdooRollbackAttempt(reason.trim());
    writeOdooRollbackAttempt(scope, attempt);
    setPendingAttempt(attempt);
    setOutcome(null);
    setRunning(true);
    const controller = new AbortController();
    abortRef.current = controller;
    const runNumber = ++runNumberRef.current;
    const result = await runOdooRollback({
      attempt,
      dependencies: {
        enqueueRollback: enqueueOdooProdRollback,
        readRollback: readOdooProdRollbackOperation,
        wait: waitFor,
      },
      onOperation: (current) => {
        if (runNumber === runNumberRef.current) {
          setOperation(current);
        }
      },
      onQueued: (operationId) => {
        attempt = { ...attempt, operationId };
        writeOdooRollbackAttempt(scope, attempt);
        if (runNumber === runNumberRef.current) {
          setPendingAttempt(attempt);
        }
      },
      scope,
      signal: controller.signal,
    });
    if (abortRef.current === controller) {
      abortRef.current = null;
    }
    if (runNumber !== runNumberRef.current) {
      return;
    }
    setRunning(false);
    setOutcome(result);
    if (result.status === "stopped" && result.certainty === "uncertain") {
      return;
    }
    writeOdooRollbackAttempt(scope, null);
    setPendingAttempt(null);
    setConfirmed(false);
    if (result.status === "rolled_back") {
      onRefresh();
    }
  }

  const target = operation?.target_artifact_id || rollbackArtifact;
  const targetDeployment =
    operation?.target_deployment_record_id ||
    (operation ? "" : detail.driver_extensions.odoo?.rollback_deployment_record_id ?? "");
  const result = operation?.result ?? null;
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
          <dt>{operation ? "Rolling back to" : "Rolls back to"}</dt>
          <dd className="promotion-code-value">
            {target || "No earlier passing production deployment"}
          </dd>
        </div>
        {targetDeployment ? (
          <div>
            <dt>From deployment</dt>
            <dd className="promotion-code-value">{targetDeployment}</dd>
          </div>
        ) : null}
        {operation ? (
          <div>
            <dt>Status</dt>
            <dd>
              {operation.status} · {rollbackPhaseDetail(operation.phase)}
            </dd>
          </div>
        ) : null}
      </dl>
      <label className="promotion-field promotion-field-wide">
        <span>Reason</span>
        <textarea
          disabled={running || pendingAttempt !== null || !rollbackArtifact}
          onChange={(event) => setReason(event.target.value)}
          placeholder="Why is production rolling back?"
          rows={2}
          value={pendingAttempt ? pendingAttempt.reason : reason}
        />
      </label>
      {pendingAttempt ? null : (
        <ConfirmationBox
          checked={confirmed}
          disabled={running || !rollbackArtifact}
          label="I confirm rolling production back"
          detail={`Product ${scope.product} · ${scope.context}/${scope.environment} → ${rollbackArtifact || "no target"}.`}
          onChange={setConfirmed}
        />
      )}
      {outcome?.status === "stopped" ? (
        <div
          className="operation-notice"
          data-phase={outcome.certainty === "uncertain" ? "uncertain" : "failed"}
          role="alert"
        >
          <AlertTriangle aria-hidden="true" />
          <div>
            <strong>Rollback stopped</strong>
            <p>{outcome.failure.message}</p>
            {outcome.certainty === "uncertain" ? (
              <p>
                The rollback runs on the server whether or not this page is open. Resuming
                only watches it; the same request never starts a second rollback.
              </p>
            ) : null}
            {outcome.failure.code ? <code>{outcome.failure.code}</code> : null}
            {outcome.failure.traceId ? <small>Trace {outcome.failure.traceId}</small> : null}
          </div>
        </div>
      ) : null}
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
          {running ? (
            <LoaderCircle className="spin" size={16} aria-hidden="true" />
          ) : (
            <RotateCcw size={16} aria-hidden="true" />
          )}
          {running
            ? "Rolling back…"
            : pendingAttempt?.operationId
              ? "Resume watching the rollback"
              : pendingAttempt
                ? "Retry the same rollback"
                : "Roll back production"}
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
            The promotion runs on the server whether or not this page is open. Resuming
            only watches it; the same request never starts a second promotion.
          </p>
        ) : outcome.certainty === "uncertain" ? (
          <p>
            The result is uncertain. Retrying reuses the same request, so Launchplane
            returns the backup it already started instead of taking another.
          </p>
        ) : null}
        {outcome.failure.code ? <code>{outcome.failure.code}</code> : null}
        {outcome.failure.traceId ? <small>Trace {outcome.failure.traceId}</small> : null}
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
  result: object | null | undefined,
  key: string,
  fallback = "—",
): string {
  const value = (result as Record<string, unknown> | null | undefined)?.[key];
  return typeof value === "string" && value ? value : fallback;
}
