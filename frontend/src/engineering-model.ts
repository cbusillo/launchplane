import type {
  EveryCodeWorkRequestSummary,
  GitHubIssueInboxReadModel,
  MergeTrainControllerStatusReadModel,
  MergeTrainPolicyTarget,
  OwnerAcceptanceQueueEntry,
  WorkGraphQueueItem,
} from "./generated/openapi.ts";
import type { Status } from "./types";

export type OwnerAcceptanceStatusFilter =
  | "all"
  | OwnerAcceptanceQueueEntry["ledger_status"];

export type WorkGraphStateFilter =
  | "all"
  | "ready"
  | "blocked"
  | "waiting";
export type WorkGraphRecommendationFilter =
  | "all"
  | WorkGraphQueueItem["recommendation"];

export interface MergeTrainTargetOption {
  key: string;
  repository: string;
  baseBranch: string;
  label: string;
  target: MergeTrainPolicyTarget;
}

export const ISSUE_RECONCILIATION_BROWSER_BOUNDARY =
  "Browser reconciliation is unsupported. The service route requires the native GitHub Actions OIDC or trusted owner-agent write identity boundary and is intentionally absent from the generated browser write contract.";

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

export function workGraphRankScopeKey(
  snapshotLoadedAt: string,
  traceId: string,
  generatedAt: string,
  fixtureMode: string | null,
): string {
  return JSON.stringify([
    "work-graph-rank",
    snapshotLoadedAt,
    traceId,
    generatedAt,
    fixtureMode ?? "",
  ]);
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

export function filterWorkGraphItems(
  items: WorkGraphQueueItem[],
  state: WorkGraphStateFilter,
  recommendation: WorkGraphRecommendationFilter,
): WorkGraphQueueItem[] {
  return items.filter(
    (item) =>
      (state === "all" || item.state === state) &&
      (recommendation === "all" || item.recommendation === recommendation),
  );
}

export function workGraphStateStatus(
  state: WorkGraphQueueItem["state"],
): Status {
  if (state === "ready") {
    return "pending";
  }
  if (state === "blocked") {
    return "blocked";
  }
  if (state === "waiting") {
    return "unknown";
  }
  return "pass";
}

export function issueInboxCounts(inbox: GitHubIssueInboxReadModel) {
  const issues = inbox.repositories.flatMap((repository) => repository.issues);
  return {
    present: issues.filter((issue) => issue.project_status === "present").length,
    missing: issues.filter((issue) => issue.project_status === "missing").length,
    stale: issues.filter(
      (issue) =>
        issue.project_status === "stale" || issue.project_status === "closed",
    ).length,
  };
}

export function issueProjectStatusTone(
  status: GitHubIssueInboxReadModel["repositories"][number]["issues"][number]["project_status"],
): Status {
  if (status === "present") {
    return "pass";
  }
  if (status === "missing") {
    return "pending";
  }
  if (status === "closed") {
    return "blocked";
  }
  if (status === "stale") {
    return "unknown";
  }
  return "unknown";
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
    status.controller_records.some((record) => record.policy_status === "stale") ||
    status.controller_diagnostics?.reconciliation_status === "required"
  ) {
    return "blocked";
  }
  if (status.controller_state?.status === "running") {
    return "pending";
  }
  if (!status.controller_state && !status.controller_diagnostics) {
    return "unknown";
  }
  return "pass";
}

export function scalarEvidence(
  source: Record<string, unknown>,
): Array<{ label: string; value: string }> {
  return Object.entries(source)
    .flatMap(([label, value]) => {
      if (typeof value === "string" || typeof value === "number") {
        return [{ label, value: String(value) }];
      }
      if (typeof value === "boolean") {
        return [{ label, value: value ? "yes" : "no" }];
      }
      return [];
    })
    .sort((left, right) => left.label.localeCompare(right.label));
}

export function ownerAcceptanceDecisionTone(
  status: OwnerAcceptanceQueueEntry["ledger_status"],
): Status {
  if (status === "accepted") return "pass";
  if (status === "not_required") return "pass";
  if (status === "pending") return "pending";
  if (status === "changes_requested") return "blocked";
  if (status === "revoked") return "blocked";
  if (status === "stale") return "unknown";
  if (status === "unavailable") return "unknown";
  return "unknown";
}

export function filterOwnerAcceptanceEntries(
  entries: OwnerAcceptanceQueueEntry[],
  status: OwnerAcceptanceStatusFilter,
  repository: string,
): OwnerAcceptanceQueueEntry[] {
  const normalized = repository.trim().toLowerCase();
  return entries.filter(
    (entry) =>
      (status === "all" || entry.ledger_status === status) &&
      (!normalized || entry.repository.includes(normalized)),
  );
}
