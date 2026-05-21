import { FolderGit2, PlusCircle, RefreshCw, SearchCheck } from "lucide-react";
import { useMemo, useState } from "react";

import { formatTime } from "./format";
import { SkeletonRows, StateBlock, StatusIcon, StatusPill } from "./status-ui";
import type {
  GitHubIssueInbox,
  GitHubIssueInboxProjectStatus,
  GitHubIssueInboxReconcileMode,
  GitHubIssueInboxReconcileSummary,
  Status,
} from "./types";

type ReconcileReader = (
  mode: GitHubIssueInboxReconcileMode,
  signal?: AbortSignal,
) => Promise<{ result: { reconcile: GitHubIssueInboxReconcileSummary } }>;

export function GitHubIssueInboxPanel({
  inbox,
  loading,
  error,
  onRefresh,
  reconcile,
}: {
  inbox: GitHubIssueInbox | null;
  loading: boolean;
  error: string;
  onRefresh: () => void;
  reconcile?: ReconcileReader;
}) {
  const [lastSummary, setLastSummary] =
    useState<GitHubIssueInboxReconcileSummary | null>(null);
  const [actionError, setActionError] = useState("");
  const [runningMode, setRunningMode] =
    useState<GitHubIssueInboxReconcileMode | null>(null);
  const counts = useMemo(() => inboxCounts(inbox), [inbox]);
  const status: Status | string = error
    ? "fail"
    : counts.stale || counts.missing
      ? "pending"
      : inbox?.project_configured
        ? "pass"
        : "unknown";
  const canReconcile = Boolean(inbox?.project_configured && reconcile);

  async function runReconcile(mode: GitHubIssueInboxReconcileMode) {
    if (!reconcile || runningMode) {
      return;
    }
    setRunningMode(mode);
    setActionError("");
    const controller = new AbortController();
    try {
      const payload = await reconcile(mode, controller.signal);
      setLastSummary(payload.result.reconcile);
    } catch (apiError) {
      if (apiError instanceof Error) {
        setActionError(apiError.message);
      } else {
        setActionError("GitHub issue inbox reconciliation failed.");
      }
    } finally {
      setRunningMode(null);
    }
  }

  return (
    <div className="github-issue-inbox">
      <div className="queue-head github-inbox-head">
        <span>GitHub inbox</span>
        <strong>{inbox ? `${inbox.issue_count} visible` : "unknown"}</strong>
        <StatusPill status={status} />
      </div>
      <div
        className="github-inbox-actions"
        aria-label="GitHub issue inbox actions"
      >
        <button type="button" onClick={onRefresh} disabled={loading}>
          <RefreshCw size={15} aria-hidden="true" />
          Refresh
        </button>
        <button
          type="button"
          onClick={() => void runReconcile("dry_run")}
          disabled={!canReconcile || runningMode !== null}
        >
          <SearchCheck size={15} aria-hidden="true" />
          Dry run
        </button>
        <button
          type="button"
          onClick={() => void runReconcile("apply")}
          disabled={!canReconcile || runningMode !== null}
        >
          <PlusCircle size={15} aria-hidden="true" />
          Apply
        </button>
      </div>
      {loading && !inbox ? <SkeletonRows /> : null}
      {error ? (
        <StateBlock icon={<FolderGit2 size={18} />} title={error} />
      ) : null}
      {!loading && !error && !inbox ? (
        <StateBlock
          icon={<FolderGit2 size={18} />}
          title="Inbox not configured"
        />
      ) : null}
      {inbox ? (
        <>
          <div className="github-inbox-summary">
            <span>
              <strong>{inbox.repository_count}</strong>
              repos
            </span>
            <span>
              <strong>{counts.present}</strong>
              present
            </span>
            <span>
              <strong>{counts.missing}</strong>
              missing
            </span>
            <span>
              <strong>{counts.stale}</strong>
              stale
            </span>
          </div>
          <div className="github-inbox-sync">
            <span>Last sync</span>
            <strong>{formatTime(inbox.generated_at)}</strong>
          </div>
        </>
      ) : null}
      {lastSummary ? <ReconcileSummary summary={lastSummary} /> : null}
      {actionError ? (
        <StateBlock icon={<FolderGit2 size={18} />} title={actionError} />
      ) : null}
      {inbox?.repositories.map((repository) => (
        <div className="github-inbox-group" key={repository.repository}>
          <div className="github-inbox-group-head">
            <code>{repository.repository}</code>
            <span>{repository.issue_count} issues</span>
          </div>
          {repository.issues.length ? (
            repository.issues.map((issue) => (
              <a
                className="github-inbox-row"
                href={issue.url || undefined}
                key={issue.key}
                target="_blank"
                rel="noreferrer"
              >
                <span className="github-inbox-title">
                  <strong>{issue.title || issue.key}</strong>
                  <code>{issue.key}</code>
                </span>
                <span className="github-inbox-meta">
                  <span>{issue.author || "unknown"}</span>
                  <span>{formatTime(issue.updated_at)}</span>
                  <IssueInboxStatusPill status={issue.project_status} />
                </span>
              </a>
            ))
          ) : (
            <StateBlock
              icon={<FolderGit2 size={18} />}
              title="No open issues"
            />
          )}
        </div>
      ))}
    </div>
  );
}

function ReconcileSummary({
  summary,
}: {
  summary: GitHubIssueInboxReconcileSummary;
}) {
  const failureItems = summary.items.filter((item) => item.action === "failed");
  return (
    <div className="github-inbox-reconcile-summary">
      <div>
        <strong>{summary.mode.replace("_", " ")}</strong>
        <span>{formatTime(summary.generated_at)}</span>
      </div>
      <div className="github-inbox-summary github-inbox-summary-compact">
        <span>{summary.would_add_count} would add</span>
        <span>{summary.added_count} added</span>
        <span>{summary.already_present_count} present</span>
        <span>{summary.failed_count} failed</span>
      </div>
      {failureItems.slice(0, 3).map((item) => (
        <code className="github-inbox-failure" key={item.key}>
          {item.key}: {item.detail || "failed"}
        </code>
      ))}
    </div>
  );
}

function IssueInboxStatusPill({
  status,
}: {
  status: GitHubIssueInboxProjectStatus;
}) {
  const visualStatus = issueInboxVisualStatus(status);
  return (
    <span className="status-pill" data-status={visualStatus}>
      <StatusIcon status={visualStatus} />
      {status}
    </span>
  );
}

function issueInboxVisualStatus(status: GitHubIssueInboxProjectStatus): Status {
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
  return "skipped";
}

function inboxCounts(inbox: GitHubIssueInbox | null) {
  const issues =
    inbox?.repositories.flatMap((repository) => repository.issues) ?? [];
  return {
    present: issues.filter((issue) => issue.project_status === "present")
      .length,
    missing: issues.filter((issue) => issue.project_status === "missing")
      .length,
    stale: issues.filter(
      (issue) =>
        issue.project_status === "stale" || issue.project_status === "closed",
    ).length,
  };
}
