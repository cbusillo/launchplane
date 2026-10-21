import type {
  EveryCodeWorkRequestSummary,
  MergeTrainControllerRecordSummary,
  MergeTrainControllerStatusReadModel,
  MergeTrainPolicyTarget,
} from "./generated/openapi.ts";
import type { Status } from "./types";

export interface MergeTrainTargetOption {
  key: string;
  repository: string;
  baseBranch: string;
  label: string;
  target: MergeTrainPolicyTarget;
}

export const MERGE_TRAIN_BROWSER_BOUNDARY =
  "Merge-train worker mutations are not browser operations. This route reads policy and controller evidence only and never executes a worker route dynamically.";

export interface MergeTrainTargetSelection {
  invalid: boolean;
  key: string;
}

export function mergeTrainTargetSelection(search: string): MergeTrainTargetSelection {
  const params = new URLSearchParams(search);
  const repositories = params.getAll("repository");
  const baseBranches = params.getAll("base_branch");
  if (!repositories.length && !baseBranches.length) {
    return { invalid: false, key: "" };
  }
  if (repositories.length !== 1 || baseBranches.length !== 1) {
    return { invalid: true, key: "" };
  }
  const repository = repositories[0].trim();
  const baseBranch = baseBranches[0].trim();
  if (!repository || !baseBranch) {
    return { invalid: true, key: "" };
  }
  return { invalid: false, key: `${repository}:${baseBranch}` };
}

export function mergeTrainStatusScopeKey(
  targetKey: string,
  policyLoadedAt: string,
  policySha256: string,
  fixtureMode: string | null,
): string {
  return JSON.stringify([
    "merge-train-status",
    targetKey,
    policyLoadedAt,
    policySha256,
    fixtureMode ?? "",
  ]);
}

export function everyCodeSummaryTone(
  summary: EveryCodeWorkRequestSummary,
): Status {
  if (summary.summary_status === "complete") {
    return "pass";
  }
  if (summary.summary_status === "stuck") {
    return "blocked";
  }
  if (summary.summary_status === "rerunnable") {
    return "unknown";
  }
  return "pending";
}

export function mergeTrainTargetOptions(
  targets: MergeTrainPolicyTarget[],
): MergeTrainTargetOption[] {
  return targets
    .filter((target) => target.repository.trim() && target.base_branch.trim())
    .map((target) => ({
      key: `${target.repository.trim()}:${target.base_branch.trim()}`,
      repository: target.repository.trim(),
      baseBranch: target.base_branch.trim(),
      label: `${target.repository.trim()} · ${target.base_branch.trim()}`,
      target,
    }))
    .sort(
      (left, right) =>
        left.repository.localeCompare(right.repository) ||
        left.baseBranch.localeCompare(right.baseBranch),
    );
}

export function mergeTrainControllerTone(
  status: MergeTrainControllerStatusReadModel,
  expectedPolicySha256 = "",
): Status {
  if (
    (expectedPolicySha256 &&
      status.current_policy_sha256 !== expectedPolicySha256) ||
    status.controller_state?.status === "reconcile_required" ||
    mergeTrainStaleAttentionRecords(status).length > 0 ||
    status.controller_diagnostics?.reconciliation_status === "required"
  ) {
    return "blocked";
  }
  if (
    status.controller_state?.status === "running" ||
    status.controller_state?.reconciliation_status === "adopted" ||
    status.controller_diagnostics?.reconciliation_status === "adopted"
  ) {
    return "pending";
  }
  if (!status.controller_state && !status.controller_diagnostics) {
    return "unknown";
  }
  return "pass";
}

export function mergeTrainActiveRecordIds(
  status: MergeTrainControllerStatusReadModel,
): Set<string> {
  return new Set(
    [
      status.controller_state?.active_record_id,
      status.controller_diagnostics?.active_record_id,
    ].filter((id): id is string => Boolean(id)),
  );
}

export function mergeTrainStaleAttentionRecords(
  status: MergeTrainControllerStatusReadModel,
): MergeTrainControllerRecordSummary[] {
  const activeIds = mergeTrainActiveRecordIds(status);
  return status.controller_records.filter(
    (record) => record.policy_status === "stale" &&
      (record.historical !== true || activeIds.has(record.record_id)),
  );
}
