import {
  ExternalLink,
  History,
  RotateCcw,
  TerminalSquare,
} from "lucide-react";
import { useCallback } from "react";

import { readEveryCodeSummary } from "./api";
import { everyCodeSummaryTone } from "./engineering-model";
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
import type {
  EveryCodeSummaryResponse,
  EveryCodeWorkRequestSummary,
} from "./generated/openapi.ts";

const SUMMARY_LIMIT = 20;

export function EngineeringEveryCodeRoute({
  fixtureMode,
}: {
  fixtureMode: DevFixtureMode;
}) {
  const loader = useCallback(
    async (
      signal: AbortSignal,
      reason: EngineeringLoadReason,
    ): Promise<EveryCodeSummaryResponse> => {
      if (fixtureMode) {
        const fixtures = await loadDevFixtures();
        fixtures.assertEngineeringRefreshAvailable(reason);
        await fixtures.waitForEngineeringFixture(signal);
        return fixtures.everyCodeForFixture(fixtureMode);
      }
      return readEveryCodeSummary(SUMMARY_LIMIT, signal);
    },
    [fixtureMode],
  );
  const resource = useEngineeringResource(loader, `every-code:${fixtureMode}`);

  return (
    <EngineeringRouteFrame
      actions={
        <EngineeringResourceControls
          cancel={resource.cancel}
          refresh={resource.refresh}
          refreshLabel="Refresh queue"
          state={resource.state}
        />
      }
      description="Inspect durable Every Code work-request summaries, provenance, and completion evidence without invoking worker mutations."
      icon={TerminalSquare}
      title="Every Code"
      view="every-code"
    >
      <EngineeringBoundaryNote title="Bounded recent history">
        This view requests at most {SUMMARY_LIMIT} summaries from
        <code> GET /v1/every-code/summary</code>. It is an operator snapshot, not
        a complete audit export or a worker control surface.
      </EngineeringBoundaryNote>
      <EngineeringResourceGate
        noun="Every Code queue"
        refresh={resource.refresh}
        state={resource.state}
      >
        {(data) => <EveryCodeContent data={data} />}
      </EngineeringResourceGate>
    </EngineeringRouteFrame>
  );
}

function EveryCodeContent({ data }: { data: EveryCodeSummaryResponse }) {
  const summaries = data.summary.summaries;
  const active = summaries.filter((summary) => summary.summary_status === "active").length;
  const stuck = summaries.filter((summary) => summary.summary_status === "stuck").length;
  const complete = summaries.filter(
    (summary) => summary.summary_status === "complete",
  ).length;
  const rerunnable = summaries.filter(
    (summary) => summary.summary_status === "rerunnable",
  ).length;

  return (
    <div className="engineering-every-code">
      <section className="engineering-metric-grid" aria-label="Every Code summary">
        <Metric label="Visible" value={summaries.length} />
        <Metric label="Active" value={active} tone={active ? "pending" : "pass"} />
        <Metric label="Stuck" value={stuck} tone={stuck ? "blocked" : "pass"} />
        <Metric label="Complete" value={complete} tone="pass" />
        <Metric label="Rerunnable" value={rerunnable} />
      </section>

      <section className="engineering-evidence-strip" aria-label="Every Code evidence">
        <div>
          <span>Generated</span>
          <strong>{formatTime(data.summary.generated_at)}</strong>
          {data.trace_id ? <code>{data.trace_id}</code> : null}
        </div>
        <div>
          <span>Repository filter</span>
          <strong>{data.summary.repository || "All repositories"}</strong>
        </div>
        <div>
          <span>State filter</span>
          <strong>{data.summary.state_filter || "All states"}</strong>
        </div>
        <div>
          <span>Issue filter</span>
          <strong>{data.summary.issue_number ?? "All issues"}</strong>
        </div>
      </section>

      {!summaries.length ? (
        <EngineeringEmpty
          detail="No work-request summaries matched the bounded service query."
          icon={History}
          title="No recent Every Code work"
        />
      ) : (
        <ol className="engineering-every-code-list">
          {summaries.map((summary) => (
            <li key={summary.request_id}>
              <EveryCodeSummaryCard summary={summary} />
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function EveryCodeSummaryCard({
  summary,
}: {
  summary: EveryCodeWorkRequestSummary;
}) {
  const issueUrl = safeExternalUrl(summary.issue_url);
  const resultUrl = safeExternalUrl(summary.result_pr_url);
  return (
    <article className="engineering-every-code-item">
      <header>
        <div>
          <span className="engineering-kicker">
            {summary.repository} · #{summary.issue_number}
          </span>
          <h2>{summary.issue_title || `Issue #${summary.issue_number}`}</h2>
        </div>
        <span
          className="engineering-status-chip"
          data-status={everyCodeSummaryTone(summary)}
        >
          <StatusIcon status={everyCodeSummaryTone(summary)} />
          {humanize(summary.summary_status)}
        </span>
      </header>
      <div className="engineering-chip-row">
        <span>{summary.state}</span>
        <span>{summary.source}</span>
        <span>{summary.trigger_actor || "Unknown actor"}</span>
        <span>Updated {formatTime(summary.updated_at)}</span>
        <span data-trust={summary.provenance.freshness_status}>
          {summary.provenance.freshness_status}
        </span>
      </div>
      <div className="engineering-work-columns">
        <div>
          <span>Next action</span>
          <p>{summary.next_action || "No next action recorded."}</p>
        </div>
        <div>
          <span>Result</span>
          <p>{summary.result_summary || "No result summary recorded."}</p>
        </div>
      </div>
      <div className="engineering-provenance-row">
        <div>
          <span>Provenance</span>
          <strong>{summary.provenance.source_kind}</strong>
          <small>{summary.provenance.detail}</small>
        </div>
        <div>
          <span>Claim</span>
          <strong>{summary.claimed_by_host || "Unclaimed"}</strong>
          <small>
            {summary.started_at
              ? `Started ${formatTime(summary.started_at)}`
              : `Queued ${formatTime(summary.queued_at)}`}
          </small>
        </div>
        <div>
          <span>Rerun evidence</span>
          <strong>{summary.safe_to_rerun ? "Recorded safe" : "Not recorded safe"}</strong>
          <small>No browser rerun control is exposed.</small>
        </div>
      </div>
      {summary.evidence.length ? (
        <div className="engineering-evidence-list">
          {summary.evidence.map((evidence) => (
            <span data-trust={evidence.state} key={`${summary.request_id}:${evidence.code}`}>
              <strong>{humanize(evidence.code)}</strong>
              {evidence.detail}
            </span>
          ))}
        </div>
      ) : null}
      <div className="engineering-link-row">
        {issueUrl ? (
          <a
            aria-label="Open issue (opens in a new tab)"
            href={issueUrl.href}
            rel="noreferrer"
            target="_blank"
          >
            Open issue
            <ExternalLink size={14} aria-hidden="true" />
          </a>
        ) : null}
        {resultUrl ? (
          <a
            aria-label="Open result PR (opens in a new tab)"
            href={resultUrl.href}
            rel="noreferrer"
            target="_blank"
          >
            Open result PR
            <ExternalLink size={14} aria-hidden="true" />
          </a>
        ) : null}
        {summary.safe_to_rerun ? (
          <span className="engineering-passive-action">
            <RotateCcw size={14} aria-hidden="true" />
            Rerunnable by supported worker callers
          </span>
        ) : null}
      </div>
    </article>
  );
}

function Metric({
  label,
  tone = "unknown",
  value,
}: {
  label: string;
  tone?: string;
  value: number;
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
