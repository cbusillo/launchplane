import {
  AlertTriangle,
  GitCompareArrows,
  Loader2,
  Play,
  Send,
  TerminalSquare,
} from "lucide-react";
import { useEffect, useState } from "react";
import {
  LaunchplaneApiError,
  dispatchGenericWebPromotionWorkflow,
  dryRunGenericWebProdPromotion,
} from "./api";
import { formatTime, labelForStatus } from "./format";
import { artifactFromLane, shorten, sourceRefFromLane } from "./laneSummary";
import { KeyValue, PanelHead } from "./panel-ui";
import { StatusIcon, StatusPill, SkeletonRows } from "./status-ui";
import type {
  DriverActionDescriptor,
  GenericWebProdPromotionPayload,
  GenericWebProdPromotionRequest,
  GenericWebPromotionWorkflowPayload,
  GenericWebPromotionWorkflowRequest,
  LaneSummary,
  ProductActionAvailability,
  Status,
} from "./types";

type EvidencePoint = {
  lane: string;
  kind: string;
  time: string;
};

export type PromotionVerdict = "ready" | "pending" | "blocked";

type PromotionGate = {
  label: string;
  status: Status | string;
  detail: string;
  evidence: string;
};

export type PromotionDecision = {
  verdict: PromotionVerdict;
  gates: PromotionGate[];
  latestEvidence: string;
  blockingEvidence: string;
  prodArtifact: string;
  testingArtifact: string;
};

export function PromotionBridge({
  prod,
  testing,
  actions,
  environmentActions = [],
  product,
  context,
  environment = "prod",
  decision,
  loading,
  onAction,
  dryRunPromotion = dryRunGenericWebProdPromotion,
  dispatchWorkflow = dispatchGenericWebPromotionWorkflow,
}: {
  prod: LaneSummary | null;
  testing: LaneSummary | null;
  actions: DriverActionDescriptor[];
  environmentActions?: ProductActionAvailability[];
  product: string;
  context: string;
  environment?: string;
  decision: PromotionDecision;
  loading: boolean;
  onAction: (action: DriverActionDescriptor) => void;
  dryRunPromotion?: (
    payload: GenericWebProdPromotionRequest,
  ) => Promise<GenericWebProdPromotionPayload>;
  dispatchWorkflow?: (
    payload: GenericWebPromotionWorkflowRequest,
  ) => Promise<GenericWebPromotionWorkflowPayload>;
}) {
  const primaryAction = pickNextAction(actions, decision.verdict);
  const workflowAction = actions.find(
    (action) =>
      action.route_path === "/v1/drivers/generic-web/prod-promotion-workflow",
  );
  const productPromotionAction = environmentActions.find(
    (action) => action.action_id === "prod_promotion",
  );
  const productWorkflowAction = environmentActions.find(
    (action) => action.action_id === "prod_promotion_workflow",
  );
  const [dryRunResult, setDryRunResult] =
    useState<GenericWebProdPromotionPayload | null>(null);
  const [workflowResult, setWorkflowResult] =
    useState<GenericWebPromotionWorkflowPayload | null>(null);
  const [dryRunError, setDryRunError] = useState("");
  const [dryRunTraceId, setDryRunTraceId] = useState("");
  const [submittingDryRun, setSubmittingDryRun] = useState(false);
  const [workflowError, setWorkflowError] = useState("");
  const [workflowTraceId, setWorkflowTraceId] = useState("");
  const [submittingWorkflowMode, setSubmittingWorkflowMode] = useState<
    "dry-run" | "promote" | ""
  >("");
  const testingArtifact = artifactFromLane(testing);
  const testingSourceRef = sourceRefFromLane(testing);
  const supportsGenericWebPromotion =
    primaryAction?.route_path === "/v1/drivers/generic-web/prod-promotion";
  const productAllowsWorkflow = productWorkflowAction?.enabled ?? Boolean(workflowAction);
  const promotionBlockers = actionDisabledReasons(productPromotionAction);
  const workflowBlockers = actionDisabledReasons(productWorkflowAction);
  const canDryRun = Boolean(
    supportsGenericWebPromotion &&
      decision.verdict === "ready" &&
      testingArtifact &&
      testingSourceRef &&
      product.trim() &&
      context.trim() &&
      !loading &&
      !submittingDryRun,
  );
  const canDispatchWorkflow = Boolean(
    supportsGenericWebPromotion &&
      workflowAction &&
      dryRunResult &&
      product.trim() &&
      context.trim() &&
      !loading &&
      !submittingWorkflowMode,
  );
  const verdictLabel =
    decision.verdict === "ready"
      ? "Ready to promote"
      : decision.verdict === "blocked"
        ? "Promotion blocked"
        : "Evidence pending";

  useEffect(() => {
    setDryRunResult(null);
    setWorkflowResult(null);
    setDryRunError("");
    setDryRunTraceId("");
    setWorkflowError("");
    setWorkflowTraceId("");
  }, [product, context, testingArtifact, testingSourceRef]);

  function runPromotionDryRun() {
    if (!canDryRun) {
      return;
    }
    setSubmittingDryRun(true);
    setDryRunError("");
    setDryRunTraceId("");
    const request: GenericWebProdPromotionRequest = {
      schema_version: 1,
      product: product.trim(),
      promotion: {
        schema_version: 1,
        product: product.trim(),
        artifact_id: testingArtifact,
        source_git_ref: testingSourceRef,
        from_instance: "testing",
        to_instance: "prod",
        timeout_seconds: 300,
        health_timeout_seconds: 120,
        dry_run: true,
      },
    };
    dryRunPromotion(request)
      .then((payload) => setDryRunResult(payload))
      .catch((apiError: unknown) => {
        if (apiError instanceof LaunchplaneApiError) {
          setDryRunError(apiError.message);
          setDryRunTraceId(apiError.traceId);
        } else if (apiError instanceof Error) {
          setDryRunError(apiError.message);
        } else {
          setDryRunError("Promotion dry run failed.");
        }
      })
      .finally(() => setSubmittingDryRun(false));
  }

  function dispatchPromotionWorkflow(dryRun: boolean) {
    if (!canDispatchWorkflow) {
      return;
    }
    setSubmittingWorkflowMode(dryRun ? "dry-run" : "promote");
    setWorkflowError("");
    setWorkflowTraceId("");
    const request: GenericWebPromotionWorkflowRequest = {
      schema_version: 1,
      product: product.trim(),
      workflow: {
        schema_version: 1,
        product: product.trim(),
        context: context.trim(),
        dry_run: dryRun,
        observe_timeout_seconds: 12,
      },
    };
    dispatchWorkflow(request)
      .then((payload) => setWorkflowResult(payload))
      .catch((apiError: unknown) => {
        if (apiError instanceof LaunchplaneApiError) {
          setWorkflowError(apiError.message);
          setWorkflowTraceId(apiError.traceId);
        } else if (apiError instanceof Error) {
          setWorkflowError(apiError.message);
        } else {
          setWorkflowError("Promotion workflow dispatch failed.");
        }
      })
      .finally(() => setSubmittingWorkflowMode(""));
  }

  return (
    <section className={`panel promotion-bridge verdict-${decision.verdict}`}>
      <PanelHead
        eyebrow={`${product || "product"} promotion`}
        title={`Testing to ${environment}`}
        right={<StatusPill status={decision.verdict} />}
      />
      {loading ? (
        <SkeletonRows />
      ) : (
        <>
          <div className="bridge-verdict">
            <span>{verdictLabel}</span>
            <strong>
              {decision.blockingEvidence || decision.latestEvidence}
            </strong>
          </div>
          <div
            className="bridge-direction"
            aria-label="Promotion artifact delta"
          >
            <div>
              <span className="lane-chip lane-chip-testing">testing</span>
              <code>{decision.testingArtifact || "unknown candidate"}</code>
            </div>
            <GitCompareArrows size={20} aria-hidden="true" />
            <div>
              <span className="lane-chip lane-chip-prod">prod</span>
              <code>{decision.prodArtifact || "unknown prod"}</code>
            </div>
          </div>
          <PromotionActionAvailability
            productAction={productPromotionAction}
            workflowAction={productWorkflowAction}
            promotionBlockers={promotionBlockers}
            workflowBlockers={workflowBlockers}
          />
          <div className="gate-list">
            {decision.gates.map((gate) => (
              <div className="gate-row" key={gate.label}>
                <StatusIcon status={gate.status} />
                <span>
                  {gate.label}
                  <em>{gate.evidence}</em>
                </span>
                <strong data-status={gate.status}>{gate.detail}</strong>
              </div>
            ))}
          </div>
          {primaryAction ? (
            <div className="bridge-action-stack">
              {supportsGenericWebPromotion ? (
                <>
                  <button
                    className="button button-primary bridge-action"
                    type="button"
                    data-safety="safe_write"
                    aria-label="Dry run generic-web prod promotion"
                    disabled={!canDryRun}
                    onClick={runPromotionDryRun}
                  >
                    {submittingDryRun ? (
                      <Loader2 className="spin" size={16} />
                    ) : (
                      <Send size={16} />
                    )}
                    <span>Dry run promotion</span>
                  </button>
                  {!canDryRun ? (
                    <ActionBlockerList
                      reasons={dryRunDisabledReasons({
                        decision,
                        testingArtifact,
                        testingSourceRef,
                        product,
                        context,
                      })}
                    />
                  ) : null}
                  {dryRunResult ? (
                    <div className="workflow-action-row">
                      <button
                        className="button button-secondary bridge-action"
                        type="button"
                        data-safety="safe_write"
                        disabled={!canDispatchWorkflow}
                        onClick={() => dispatchPromotionWorkflow(true)}
                      >
                        {submittingWorkflowMode === "dry-run" ? (
                          <Loader2 className="spin" size={16} />
                        ) : (
                          <Play size={16} />
                        )}
                        <span>Run workflow dry run</span>
                      </button>
                    </div>
                  ) : null}
                  {dryRunResult && !canDispatchWorkflow ? (
                    <ActionBlockerList
                      reasons={workflowDisabledReasons({
                        product,
                        context,
                      })}
                    />
                  ) : null}
                </>
              ) : (
                <button
                  className="button button-primary bridge-action"
                  type="button"
                  data-safety={primaryAction.safety}
                  aria-label={`Review ${primaryAction.label}`}
                  disabled={
                    decision.verdict !== "ready" &&
                    primaryAction.safety === "mutation"
                  }
                  onClick={() => onAction(primaryAction)}
                >
                  <TerminalSquare size={16} />
                  <span>{primaryAction.label}</span>
                </button>
              )}
              {dryRunError ? (
                <InlinePanelError
                  message={dryRunError}
                  traceId={dryRunTraceId}
                />
              ) : null}
              {dryRunResult ? (
                <PromotionDryRunResult payload={dryRunResult} />
              ) : null}
              {workflowError ? (
                <InlinePanelError
                  message={workflowError}
                  traceId={workflowTraceId}
                />
              ) : null}
              {workflowResult ? (
                <PromotionWorkflowResult payload={workflowResult} />
              ) : null}
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}

function PromotionActionAvailability({
  productAction,
  workflowAction,
  promotionBlockers,
  workflowBlockers,
}: {
  productAction?: ProductActionAvailability;
  workflowAction?: ProductActionAvailability;
  promotionBlockers: string[];
  workflowBlockers: string[];
}) {
  if (!productAction && !workflowAction) {
    return null;
  }
  return (
    <div className="bridge-action-availability" aria-label="Promotion action availability">
      <ActionAvailabilityRow
        label="Live promotion"
        action={productAction}
        blockers={promotionBlockers}
      />
      <ActionAvailabilityRow
        label="Workflow dispatch"
        action={workflowAction}
        blockers={workflowBlockers}
      />
    </div>
  );
}

function ActionAvailabilityRow({
  label,
  action,
  blockers,
}: {
  label: string;
  action?: ProductActionAvailability;
  blockers: string[];
}) {
  if (!action) {
    return (
      <div className="bridge-action-availability-row" data-enabled="false">
        <strong>{label}</strong>
        <span>not advertised</span>
      </div>
    );
  }
  return (
    <div className="bridge-action-availability-row" data-enabled={action.enabled}>
      <strong>{label}</strong>
      <span>{action.enabled ? "available" : blockers.join("; ") || "blocked"}</span>
    </div>
  );
}

function ActionBlockerList({ reasons }: { reasons: string[] }) {
  if (!reasons.length) {
    return null;
  }
  return (
    <div className="bridge-action-blockers" role="status">
      {reasons.map((reason) => (
        <span key={reason}>{reason}</span>
      ))}
    </div>
  );
}

function actionDisabledReasons(action?: ProductActionAvailability): string[] {
  if (!action || action.enabled) {
    return [];
  }
  return action.disabled_reasons.length ? action.disabled_reasons : ["Action is disabled."];
}

function dryRunDisabledReasons({
  decision,
  testingArtifact,
  testingSourceRef,
  product,
  context,
}: {
  decision: PromotionDecision;
  testingArtifact: string;
  testingSourceRef: string;
  product: string;
  context: string;
}): string[] {
  const reasons: string[] = [];
  if (decision.verdict !== "ready") {
    reasons.push(decision.blockingEvidence || "Promotion evidence is not ready.");
  }
  if (!testingArtifact) {
    reasons.push("Testing artifact evidence is missing.");
  }
  if (!testingSourceRef) {
    reasons.push("Testing source ref evidence is missing.");
  }
  if (!product.trim()) {
    reasons.push("Product key is missing.");
  }
  if (!context.trim()) {
    reasons.push("Prod context is missing.");
  }
  return uniqueStrings(reasons);
}

function workflowDisabledReasons({
  product,
  context,
}: {
  product: string;
  context: string;
}): string[] {
  const reasons: string[] = [];
  if (!product.trim()) {
    reasons.push("Product key is missing.");
  }
  if (!context.trim()) {
    reasons.push("Prod context is missing.");
  }
  return uniqueStrings(reasons);
}

function uniqueStrings(values: string[]): string[] {
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))];
}

export function buildPromotionDecision(
  prod: LaneSummary | null,
  testing: LaneSummary | null,
  options: { requireProdBackup?: boolean } = {},
): PromotionDecision {
  const gates = promotionGates(prod, testing, options);
  const verdict = gates.some(
    (gate) => gate.status === "fail" || gate.status === "blocked",
  )
    ? "blocked"
    : gates.some(
          (gate) => gate.status === "pending" || gate.status === "unknown",
        )
      ? "pending"
      : "ready";
  const blockingGate = gates.find(
    (gate) =>
      gate.status === "fail" ||
      gate.status === "blocked" ||
      gate.status === "unknown",
  );
  return {
    verdict,
    gates,
    latestEvidence: latestEvidenceLabel(prod, testing),
    blockingEvidence: blockingGate
      ? `${blockingGate.label}: ${blockingGate.evidence}`
      : "",
    prodArtifact: artifactFromLane(prod),
    testingArtifact: artifactFromLane(testing),
  };
}

export function pickNextAction(
  actions: DriverActionDescriptor[],
  verdict: PromotionVerdict,
): DriverActionDescriptor | undefined {
  if (verdict === "ready") {
    return actions.find((action) => action.action_id === "prod_promotion");
  }
  return (
    actions.find((action) => action.action_id === "prod_backup_gate") ??
    actions.find((action) => action.safety === "safe_write") ??
    actions.find((action) => action.safety === "read")
  );
}

function InlinePanelError({
  message,
  traceId,
}: {
  message: string;
  traceId: string;
}) {
  return (
    <div className="config-inline-alert bridge-inline-alert" role="alert">
      <AlertTriangle size={15} aria-hidden="true" />
      <span>{message}</span>
      {traceId ? <code>{traceId}</code> : null}
    </div>
  );
}

function PromotionDryRunResult({
  payload,
}: {
  payload: GenericWebProdPromotionPayload;
}) {
  const result = payload.result;
  return (
    <div className="config-result bridge-dry-run-result" aria-live="polite">
      <div className="config-result-summary">
        <KeyValue
          label="Mode"
          value={result.dry_run ? "dry-run" : "unknown"}
          status="pending"
        />
        <KeyValue
          label="Promotion"
          value={labelForStatus(result.promotion_status)}
          status={result.promotion_status}
        />
        <KeyValue
          label="Deploy"
          value={labelForStatus(result.deployment_status)}
          status={result.deployment_status}
        />
        <KeyValue
          label="Health"
          value={labelForStatus(result.destination_health_status)}
          status={result.destination_health_status}
        />
      </div>
      <div className="config-result-list">
        <div className="config-result-row">
          <strong>Promotion record</strong>
          <code>{result.promotion_record_id || "pending dry-run record"}</code>
        </div>
        <div className="config-result-row">
          <strong>Record writes</strong>
          <code>
            {result.deployment_record_id || result.inventory_record_id
              ? "unexpected write candidate"
              : "none"}
          </code>
        </div>
        <div className="config-result-row">
          <strong>Trace</strong>
          <code>{payload.trace_id}</code>
        </div>
      </div>
    </div>
  );
}

function PromotionWorkflowResult({
  payload,
}: {
  payload: GenericWebPromotionWorkflowPayload;
}) {
  const result = payload.result;
  return (
    <div className="config-result bridge-workflow-result" aria-live="polite">
      <div className="config-result-summary">
        <KeyValue
          label="Workflow"
          value={result.dry_run ? "dry-run" : "promote"}
          status={result.dry_run ? "pending" : "pass"}
        />
        <KeyValue
          label="Dispatch"
          value={result.dispatch_status}
          status="pass"
        />
        <KeyValue
          label="Run"
          value={result.run_status || "pending"}
          status={result.run_status}
        />
        <KeyValue
          label="Conclusion"
          value={result.run_conclusion || "pending"}
          status={result.run_conclusion || "pending"}
        />
      </div>
      <div className="config-result-list">
        <div className="config-result-row">
          <strong>Repository</strong>
          <code>{result.repository}</code>
        </div>
        <div className="config-result-row">
          <strong>Workflow</strong>
          <code>{`${result.workflow_id}@${result.ref}`}</code>
        </div>
        <div className="config-result-row">
          <strong>Run</strong>
          {result.run_url ? (
            <a href={result.run_url} target="_blank" rel="noreferrer">
              {result.run_id || result.run_url}
            </a>
          ) : (
            <code>not observed yet</code>
          )}
        </div>
        <div className="config-result-row">
          <strong>Trace</strong>
          <code>{payload.trace_id}</code>
        </div>
      </div>
    </div>
  );
}

function promotionGates(
  prod: LaneSummary | null,
  testing: LaneSummary | null,
  { requireProdBackup = true }: { requireProdBackup?: boolean } = {},
): PromotionGate[] {
  const testingDeploy =
    testing?.latest_deployment?.deploy.status ??
    testing?.inventory?.deploy.status ??
    "unknown";
  const testingHealth =
    testing?.inventory?.destination_health.status ??
    testing?.latest_deployment?.destination_health.status ??
    "unknown";
  const backupGate = prod?.latest_backup_gate?.status ?? "unknown";
  const candidateArtifact = artifactFromLane(testing);
  const prodArtifact = artifactFromLane(prod);
  const gates: PromotionGate[] = [
    {
      label: "Candidate deployment",
      status: normalizeGateStatus(testingDeploy),
      detail: labelForStatus(testingDeploy),
      evidence:
        testing?.latest_deployment?.record_id ?? "missing deployment evidence",
    },
    {
      label: "Candidate health",
      status: normalizeGateStatus(testingHealth),
      detail: labelForStatus(testingHealth),
      evidence: testing?.inventory?.destination_health.verified
        ? "verified healthcheck"
        : "missing healthcheck",
    },
  ];
  if (requireProdBackup) {
    gates.push({
      label: "Prod backup gate",
      status:
        backupGate === "pass"
          ? "pass"
          : backupGate === "fail"
            ? "blocked"
            : "unknown",
      detail: labelForStatus(backupGate),
      evidence:
        prod?.latest_backup_gate?.record_id ?? "required before prod change",
    });
  }
  gates.push({
    label: "Artifact delta",
    status:
      candidateArtifact && candidateArtifact !== prodArtifact
        ? "pass"
        : "unknown",
    detail: candidateArtifact && prodArtifact ? "changed" : "missing",
    evidence:
      candidateArtifact && prodArtifact
        ? `${shorten(candidateArtifact)} -> ${shorten(prodArtifact)}`
        : "candidate or prod artifact missing",
  });
  return gates;
}

function normalizeGateStatus(status: Status | string): Status {
  if (status === "pass") {
    return "pass";
  }
  if (status === "fail") {
    return "blocked";
  }
  if (status === "pending") {
    return "pending";
  }
  return "unknown";
}

function latestEvidenceLabel(
  prod: LaneSummary | null,
  testing: LaneSummary | null,
): string {
  const latest = latestEvidencePoint(prod, testing);
  if (!latest) {
    return "No promotion evidence has been recorded.";
  }
  return `${latest.lane} ${latest.kind}: ${formatTime(latest.time)}`;
}

function latestEvidencePoint(
  prod: LaneSummary | null,
  testing: LaneSummary | null,
): EvidencePoint | null {
  const points = [
    ...evidencePointsForLane(prod, "prod"),
    ...evidencePointsForLane(testing, "testing"),
  ].sort((left, right) => right.time.localeCompare(left.time));
  return points[0] ?? null;
}

function evidencePointsForLane(
  lane: LaneSummary | null,
  laneName: string,
): EvidencePoint[] {
  const points: EvidencePoint[] = [];
  if (lane?.inventory) {
    points.push({ lane: laneName, kind: "inventory", time: lane.inventory.updated_at });
  }
  if (lane?.release_tuple) {
    points.push({
      lane: laneName,
      kind: "release",
      time: lane.release_tuple.minted_at,
    });
  }
  if (lane?.latest_backup_gate) {
    points.push({
      lane: laneName,
      kind: "backup",
      time: lane.latest_backup_gate.created_at,
    });
  }
  if (lane?.latest_deployment) {
    points.push({
      lane: laneName,
      kind: "deployment",
      time:
        lane.latest_deployment.deploy.finished_at ??
        lane.latest_deployment.deploy.started_at ??
        "",
    });
  }
  if (lane?.latest_promotion) {
    points.push({
      lane: laneName,
      kind: "promotion",
      time:
        lane.latest_promotion.deploy.finished_at ??
        lane.latest_promotion.deploy.started_at ??
        "",
    });
  }
  return points;
}
