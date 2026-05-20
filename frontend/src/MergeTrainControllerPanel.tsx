import { GitMerge, ListChecks, RefreshCw, Route } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { LaunchplaneApiError, readMergeTrainControllerStatus } from "./api";
import { formatTime } from "./format";
import { MetricTile, PanelHead } from "./panel-ui";
import { SkeletonRows, StateBlock, StatusIcon, StatusPill } from "./status-ui";
import type {
  MergeTrainControllerRecordSummary,
  MergeTrainControllerStatus,
  ProductSiteOverview,
  Status,
  WorkGraphQueueItem,
} from "./types";

type MergeTrainTarget = {
  repository: string;
  baseBranch: string;
  label: string;
};
type MergeTrainStatusReader = (
  repository: string,
  baseBranch: string,
  signal?: AbortSignal,
) => Promise<{ controller_status: MergeTrainControllerStatus }>;

const DEFAULT_BASE_BRANCH = "main";

export function MergeTrainControllerPanel({
  products,
  selectedProduct,
  workGraphItems,
  loading,
  readStatus = readMergeTrainControllerStatus,
}: {
  products: ProductSiteOverview[];
  selectedProduct: ProductSiteOverview | null;
  workGraphItems: WorkGraphQueueItem[];
  loading: boolean;
  readStatus?: MergeTrainStatusReader;
}) {
  const targets = useMemo(
    () => mergeTrainTargets(products, workGraphItems),
    [products, workGraphItems],
  );
  const preferredTarget = preferredMergeTrainTarget(targets, selectedProduct);
  const [targetKey, setTargetKey] = useState(() =>
    targetKeyFor(preferredTarget),
  );
  const [status, setStatus] = useState<MergeTrainControllerStatus | null>(null);
  const [statusLoading, setStatusLoading] = useState(false);
  const [error, setError] = useState("");
  const [traceId, setTraceId] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    if (!targets.length) {
      setTargetKey("");
      return;
    }
    if (!targets.some((target) => targetKeyFor(target) === targetKey)) {
      setTargetKey(targetKeyFor(preferredTarget));
    }
  }, [preferredTarget, targetKey, targets]);

  const selectedTarget =
    targets.find((target) => targetKeyFor(target) === targetKey) ??
    preferredTarget;

  useEffect(() => {
    if (!selectedTarget) {
      setStatus(null);
      setError("");
      setTraceId("");
      setStatusLoading(false);
      return;
    }
    const controller = new AbortController();
    setStatusLoading(true);
    setError("");
    setTraceId("");
    readStatus(
      selectedTarget.repository,
      selectedTarget.baseBranch,
      controller.signal,
    )
      .then((payload) => {
        if (!controller.signal.aborted) {
          setStatus(payload.controller_status);
        }
      })
      .catch((apiError: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        setStatus(null);
        if (apiError instanceof LaunchplaneApiError) {
          setError(apiError.message);
          setTraceId(apiError.traceId);
        } else if (apiError instanceof Error) {
          setError(apiError.message);
          setTraceId("");
        } else {
          setError("Merge train controller status request failed.");
          setTraceId("");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setStatusLoading(false);
        }
      });
    return () => controller.abort();
  }, [readStatus, selectedTarget, refreshKey]);

  const admissionStatus = status?.admission.status ?? "unknown";
  const panelStatus = error ? "fail" : controllerPanelStatus(status);
  const currentRecords = status?.controller_records.filter(
    (record) => record.policy_status === "current",
  );
  const staleRecords = status?.controller_records.filter(
    (record) => record.policy_status === "stale",
  );

  return (
    <section className="merge-train-panel" aria-busy={loading || statusLoading}>
      <PanelHead
        eyebrow="merge train"
        title="Controller status"
        right={<StatusPill status={panelStatus} />}
      />
      <div className="merge-train-toolbar">
        <label className="merge-train-target">
          <span>Repository</span>
          <select
            value={selectedTarget ? targetKeyFor(selectedTarget) : ""}
            onChange={(event) => setTargetKey(event.target.value)}
            disabled={!targets.length || statusLoading}
            aria-label="Merge train repository"
          >
            {targets.map((target) => (
              <option key={targetKeyFor(target)} value={targetKeyFor(target)}>
                {target.label}
              </option>
            ))}
          </select>
        </label>
        <button
          className="icon-button"
          type="button"
          title="Refresh merge train controller status"
          aria-label="Refresh merge train controller status"
          onClick={() => setRefreshKey((value) => value + 1)}
          disabled={!selectedTarget || statusLoading}
        >
          <RefreshCw size={16} className={statusLoading ? "spin" : ""} />
        </button>
      </div>
      {loading || statusLoading ? <SkeletonRows /> : null}
      {!selectedTarget && !loading ? (
        <StateBlock icon={<Route size={18} />} title="No merge train target" />
      ) : null}
      {error ? (
        <div className="merge-train-alert">
          <StatusIcon status="fail" />
          <span>{error}</span>
          {traceId ? <code>{traceId}</code> : null}
        </div>
      ) : null}
      {status && !statusLoading ? (
        <>
          <div className="merge-train-metrics">
            <MetricTile
              label="Admission"
              value={admissionStatus}
              status={statusForAdmission(admissionStatus)}
            />
            <MetricTile
              label="Action"
              value={status.admission.controller_action || "idle"}
              status={statusForControllerAction(
                status.admission.controller_action,
              )}
            />
            <MetricTile
              label="Records"
              value={String(currentRecords?.length ?? 0)}
              status={currentRecords?.length ? "pending" : "pass"}
            />
            <MetricTile
              label="Stale"
              value={String(staleRecords?.length ?? 0)}
              status={staleRecords?.length ? "blocked" : "pass"}
            />
          </div>
          <div className="merge-train-summary">
            <span>
              <GitMerge size={15} aria-hidden="true" />
              <strong>{status.repository}</strong>
              <code>{status.base_branch}</code>
            </span>
            <span>
              {status.admission.detail || status.admission.controller_reason}
            </span>
            <code>{formatTime(status.generated_at)}</code>
          </div>
          {status.latest_run ? (
            <div className="merge-train-latest-run">
              <ListChecks size={15} aria-hidden="true" />
              <span>
                <strong>{status.latest_run.status}</strong>
                <code>{status.latest_run.run_id}</code>
              </span>
              <span>
                {status.latest_run.required_checks_status || "checks unknown"}
              </span>
              <code>{formatTime(status.latest_run.recorded_at)}</code>
            </div>
          ) : null}
          <div className="merge-train-record-list">
            {status.controller_records.length ? (
              status.controller_records
                .slice(0, 4)
                .map((record) => (
                  <MergeTrainRecordRow key={record.record_id} record={record} />
                ))
            ) : (
              <StateBlock
                icon={<GitMerge size={18} />}
                title="No active records"
              />
            )}
          </div>
        </>
      ) : null}
    </section>
  );
}

function MergeTrainRecordRow({
  record,
}: {
  record: MergeTrainControllerRecordSummary;
}) {
  const status = record.policy_status === "stale" ? "blocked" : "pending";
  return (
    <div className="merge-train-record-row" data-policy={record.policy_status}>
      <StatusIcon status={status} />
      <span>
        <strong>{recordLabel(record.record_type)}</strong>
        <code>{record.record_id}</code>
      </span>
      <span className="merge-train-record-meta">
        {record.pull_request_numbers.length ? (
          <code>PR {record.pull_request_numbers.join(", ")}</code>
        ) : null}
        {record.batch_id ? <code>{record.batch_id}</code> : null}
        <code>{record.status}</code>
        {record.required_checks_status ? (
          <code>{record.required_checks_status}</code>
        ) : null}
      </span>
      <span className="merge-train-record-progress">
        {record.planned_count
          ? `${record.merged_count}/${record.planned_count} merged`
          : ""}
        {record.blocked_count ? ` ${record.blocked_count} blocked` : ""}
        {record.stale_reason || ""}
      </span>
      <code>{formatTime(record.updated_at)}</code>
    </div>
  );
}

function mergeTrainTargets(
  products: ProductSiteOverview[],
  workGraphItems: WorkGraphQueueItem[],
): MergeTrainTarget[] {
  const targetByKey = new Map<string, MergeTrainTarget>();
  for (const product of products) {
    addTarget(targetByKey, {
      repository: product.repository,
      baseBranch: DEFAULT_BASE_BRANCH,
      label: product.display_name || product.product || product.repository,
    });
  }
  for (const item of workGraphItems) {
    addTarget(targetByKey, {
      repository: item.repository,
      baseBranch: DEFAULT_BASE_BRANCH,
      label: item.product_display_name || item.product || item.repository,
    });
  }
  return [...targetByKey.values()].sort((left, right) =>
    left.label.localeCompare(right.label),
  );
}

function addTarget(
  targetByKey: Map<string, MergeTrainTarget>,
  target: MergeTrainTarget,
) {
  if (!target.repository.trim()) {
    return;
  }
  const key = targetKeyFor(target);
  if (!targetByKey.has(key)) {
    targetByKey.set(key, {
      repository: target.repository.trim(),
      baseBranch: target.baseBranch.trim() || DEFAULT_BASE_BRANCH,
      label: target.label.trim() || target.repository.trim(),
    });
  }
}

function preferredMergeTrainTarget(
  targets: MergeTrainTarget[],
  selectedProduct: ProductSiteOverview | null,
): MergeTrainTarget | null {
  if (selectedProduct?.repository) {
    const selected = targets.find(
      (target) => target.repository === selectedProduct.repository,
    );
    if (selected) {
      return selected;
    }
  }
  return targets[0] ?? null;
}

function targetKeyFor(target: MergeTrainTarget | null): string {
  return target ? `${target.repository}:${target.baseBranch}` : "";
}

function controllerPanelStatus(
  status: MergeTrainControllerStatus | null,
): Status | string {
  if (!status) {
    return "unknown";
  }
  if (
    status.controller_records.some(
      (record) => record.policy_status === "stale",
    ) ||
    status.admission.controller_action.includes("failed")
  ) {
    return "blocked";
  }
  return statusForAdmission(status.admission.status);
}

function statusForAdmission(status: string): Status | string {
  if (status === "admitted") {
    return "pending";
  }
  if (status === "deferred") {
    return "unknown";
  }
  return "unknown";
}

function statusForControllerAction(action: string): Status | string {
  if (!action || action === "idle") {
    return "pass";
  }
  if (action.includes("failed") || action.includes("unsupported")) {
    return "blocked";
  }
  if (action.includes("wait") || action.includes("observe")) {
    return "unknown";
  }
  return "pending";
}

function recordLabel(recordType: string): string {
  return recordType.replaceAll("_", " ");
}
