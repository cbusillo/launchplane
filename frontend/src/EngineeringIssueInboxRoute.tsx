import {
  ExternalLink,
  FolderGit2,
  Inbox,
  ShieldAlert,
} from "lucide-react";
import { useCallback } from "react";

import { readGitHubIssueInbox } from "./api";
import {
  ISSUE_RECONCILIATION_BROWSER_BOUNDARY,
  issueInboxCounts,
  issueProjectStatusTone,
} from "./engineering-model";
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
import { StatusIcon } from "./status-ui";
import { safeExternalUrl } from "./url";

import { loadDevFixtures, type DevFixtureMode } from "./dev-fixture-loader";
import type { WorkGraphIssueInboxResponse } from "./generated/openapi.ts";

export function EngineeringIssueInboxRoute({
  fixtureMode,
}: {
  fixtureMode: DevFixtureMode;
}) {
  const loader = useCallback(
    async (
      signal: AbortSignal,
      reason: EngineeringLoadReason,
    ): Promise<WorkGraphIssueInboxResponse> => {
      if (fixtureMode) {
        const fixtures = await loadDevFixtures();
        fixtures.assertEngineeringRefreshAvailable(reason);
        await fixtures.waitForEngineeringFixture(signal);
        return fixtures.issueInboxForFixture(fixtureMode);
      }
      return readGitHubIssueInbox(signal);
    },
    [fixtureMode],
  );
  const resource = useEngineeringResource(loader, `issue-inbox:${fixtureMode}`);

  return (
    <EngineeringRouteFrame
      actions={
        <EngineeringResourceControls
          cancel={resource.cancel}
          refresh={resource.refresh}
          refreshLabel="Refresh inbox"
          state={resource.state}
        />
      }
      description="Read the explicit GitHub repository inventory and Code Plans membership without turning Launchplane into issue authority."
      icon={Inbox}
      title="Issue inbox"
      view="issue-inbox"
    >
      <EngineeringBoundaryNote title="Read-only browser boundary">
        {ISSUE_RECONCILIATION_BROWSER_BOUNDARY} Apply additionally requires
        <code> work_graph.issue_inbox.reconcile</code>. No Dry Run or Apply
        control is rendered here.
      </EngineeringBoundaryNote>
      <EngineeringResourceGate
        noun="issue inbox"
        refresh={resource.refresh}
        state={resource.state}
      >
        {(data) => <IssueInboxContent data={data} />}
      </EngineeringResourceGate>
    </EngineeringRouteFrame>
  );
}

function IssueInboxContent({ data }: { data: WorkGraphIssueInboxResponse }) {
  const inbox = data.inbox;
  const counts = issueInboxCounts(inbox);
  const openCount = inbox.repositories
    .flatMap((repository) => repository.issues)
    .filter((issue) => issue.state.toLowerCase() === "open").length;

  return (
    <div className="engineering-issue-inbox">
      <section className="engineering-metric-grid" aria-label="Issue inbox summary">
        <Metric label="Repositories" value={inbox.repository_count} />
        <Metric label="Visible items" value={inbox.issue_count} />
        <Metric label="Open issues" value={openCount} />
        {inbox.project_configured ? (
          <>
            <Metric label="In project" value={counts.present} tone="pass" />
            <Metric
              label="Missing"
              value={counts.missing}
              tone={counts.missing ? "pending" : "pass"}
            />
          </>
        ) : (
          <Metric label="Project membership" value="Not evaluated" />
        )}
        <Metric
          label="Stale project items"
          value={inbox.stale_project_item_count}
          tone={inbox.stale_project_item_count ? "blocked" : "pass"}
        />
      </section>

      <section className="engineering-evidence-strip" aria-label="Inbox evidence">
        <div>
          <span>Inventory</span>
          <strong>{data.configured ? "Configured" : "Not configured"}</strong>
        </div>
        <div>
          <span>Code Plans Project</span>
          <strong>{inbox.project_configured ? "Configured" : "Unconfigured"}</strong>
        </div>
        <div>
          <span>Generated</span>
          <strong>{formatTime(inbox.generated_at)}</strong>
          {data.trace_id ? <code>{data.trace_id}</code> : null}
        </div>
      </section>

      {!data.configured ? (
        <EngineeringEmpty
          detail="Launchplane has no explicit issue-inbox repository inventory. It will not search owners or infer repositories."
          icon={ShieldAlert}
          title="Issue inventory not configured"
        />
      ) : !inbox.repositories.length ? (
        <EngineeringEmpty
          detail="The configured issue inventory contains no repository groups."
          icon={FolderGit2}
          title="No repositories in the inbox"
        />
      ) : (
        <div className="engineering-inbox-groups">
          {inbox.repositories.map((repository) => (
            <section
              className="engineering-inbox-group"
              key={repository.repository}
            >
              <header>
                <div>
                  <span className="engineering-kicker">Repository</span>
                  <h2>{repository.repository}</h2>
                </div>
                <div className="engineering-chip-row">
                  <span>{repository.issue_count} issues</span>
                  <span>{repository.present_in_project_count} present</span>
                  <span>{repository.missing_from_project_count} missing</span>
                </div>
              </header>
              {!repository.issues.length ? (
                <EngineeringEmpty
                  detail="GitHub returned no open issues for this configured repository."
                  icon={Inbox}
                  title="No open issues"
                />
              ) : (
                <ul className="engineering-inbox-list">
                  {repository.issues.map((issue) => {
                    const url = safeExternalUrl(issue.url);
                    return (
                      <li key={issue.key}>
                        <div className="engineering-inbox-title">
                          <div>
                            <span>{issue.key}</span>
                            <strong>{issue.title}</strong>
                          </div>
                          <span
                            className="engineering-status-chip"
                            data-status={issueProjectStatusTone(issue.project_status)}
                          >
                            <StatusIcon
                              status={issueProjectStatusTone(issue.project_status)}
                            />
                            {humanize(issue.project_status)}
                          </span>
                        </div>
                        <div className="engineering-inbox-meta">
                          <span>{issue.author || "Unknown author"}</span>
                          <span>{issue.state}</span>
                          <span>Updated {formatTime(issue.updated_at)}</span>
                          {issue.labels.map((label) => (
                            <span key={label}>{label}</span>
                          ))}
                        </div>
                        {url ? (
                          <a
                            aria-label={`Open ${issue.key} (opens in a new tab)`}
                            href={url.href}
                            rel="noreferrer"
                            target="_blank"
                          >
                            Open issue
                            <ExternalLink size={14} aria-hidden="true" />
                          </a>
                        ) : null}
                      </li>
                    );
                  })}
                </ul>
              )}
            </section>
          ))}
        </div>
      )}
    </div>
  );
}

function Metric({
  label,
  tone = "unknown",
  value,
}: {
  label: string;
  tone?: string;
  value: number | string;
}) {
  return (
    <div className="engineering-metric" data-tone={tone}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function humanize(value: string): string {
  return value.replaceAll("_", " ");
}
