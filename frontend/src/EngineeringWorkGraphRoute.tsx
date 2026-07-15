import {
  ExternalLink,
  Filter,
  GitBranch,
  Network,
  Route,
} from "lucide-react";
import { useCallback, useMemo, useState } from "react";

import { readWorkGraphSnapshot, rankWorkGraphSnapshot } from "./api";
import {
  filterWorkGraphItems,
  scalarEvidence,
  workGraphRankScopeKey,
  workGraphStateStatus,
  type WorkGraphRecommendationFilter,
  type WorkGraphStateFilter,
} from "./engineering-model";
import {
  useEngineeringResource,
  type EngineeringLoadReason,
  type EngineeringResourceController,
} from "./engineering-resource";
import {
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
  WorkGraphQueue,
  WorkGraphSnapshotResponse,
} from "./generated/openapi.ts";

interface WorkGraphRankViewData {
  queue: WorkGraphQueue;
  traceId: string;
}

export function EngineeringWorkGraphRoute({
  fixtureMode,
}: {
  fixtureMode: DevFixtureMode;
}) {
  const [stateFilter, setStateFilter] = useState<WorkGraphStateFilter>("all");
  const [recommendationFilter, setRecommendationFilter] =
    useState<WorkGraphRecommendationFilter>("all");
  const snapshotLoader = useCallback(
    async (
      signal: AbortSignal,
      reason: EngineeringLoadReason,
    ): Promise<WorkGraphSnapshotResponse> => {
      if (fixtureMode) {
        const fixtures = await loadDevFixtures();
        fixtures.assertEngineeringRefreshAvailable(reason);
        await fixtures.waitForEngineeringFixture(signal);
        return fixtures.workGraphForFixture(fixtureMode).snapshotResponse;
      }
      return readWorkGraphSnapshot(signal);
    },
    [fixtureMode],
  );
  const snapshotResource = useEngineeringResource(
    snapshotLoader,
    `work-graph-snapshot:${fixtureMode}`,
  );
  const snapshot = snapshotResource.state.data?.snapshot ?? null;
  const rankLoader = useCallback(
    async (
      signal: AbortSignal,
      reason: EngineeringLoadReason,
    ): Promise<WorkGraphRankViewData> => {
      if (!snapshot) {
        throw new Error("No work-graph snapshot is available to rank.");
      }
      if (fixtureMode) {
        const fixtures = await loadDevFixtures();
        fixtures.assertEngineeringRefreshAvailable(reason);
        await fixtures.waitForEngineeringFixture(signal);
        const fixture = fixtures.workGraphForFixture(fixtureMode);
        return { queue: fixture.queue, traceId: fixture.rankTraceId };
      }
      const response = await rankWorkGraphSnapshot(snapshot, 24, signal);
      return { queue: response.result.queue, traceId: response.trace_id };
    },
    [fixtureMode, snapshot],
  );
  const rankResource = useEngineeringResource(
    rankLoader,
    workGraphRankScopeKey(
      snapshotResource.state.lastSuccessfulAt,
      snapshotResource.state.data?.trace_id ?? "",
      snapshot?.generated_at ?? "",
      fixtureMode,
    ),
    Boolean(snapshot?.issues.length),
  );

  return (
    <EngineeringRouteFrame
      actions={
        <EngineeringResourceControls
          cancel={snapshotResource.cancel}
          refresh={snapshotResource.refresh}
          refreshLabel="Refresh snapshot"
          state={snapshotResource.state}
        />
      }
      description="Launchplane ranks compact GitHub and Code Plans facts into a safe-to-start queue without becoming planning authority."
      icon={Network}
      title="Work graph"
      view="work-graph"
    >
      <EngineeringResourceGate
        noun="work-graph snapshot"
        refresh={snapshotResource.refresh}
        state={snapshotResource.state}
      >
        {(snapshotResponse) => (
          <WorkGraphSnapshotContent
            rankResource={rankResource}
            recommendationFilter={recommendationFilter}
            setRecommendationFilter={setRecommendationFilter}
            setStateFilter={setStateFilter}
            snapshotResponse={snapshotResponse}
            stateFilter={stateFilter}
          />
        )}
      </EngineeringResourceGate>
    </EngineeringRouteFrame>
  );
}

function WorkGraphSnapshotContent({
  rankResource,
  recommendationFilter,
  setRecommendationFilter,
  setStateFilter,
  snapshotResponse,
  stateFilter,
}: {
  rankResource: EngineeringResourceController<WorkGraphRankViewData>;
  recommendationFilter: WorkGraphRecommendationFilter;
  setRecommendationFilter: (filter: WorkGraphRecommendationFilter) => void;
  setStateFilter: (filter: WorkGraphStateFilter) => void;
  snapshotResponse: WorkGraphSnapshotResponse;
  stateFilter: WorkGraphStateFilter;
}) {
  const sourceEvidence = scalarEvidence(snapshotResponse.source);
  return (
    <div className="engineering-work-graph">
      <section className="engineering-evidence-strip" aria-label="Snapshot evidence">
        <div>
          <span>Snapshot</span>
          <strong>{formatTime(snapshotResponse.snapshot.generated_at)}</strong>
          {snapshotResponse.trace_id ? <code>{snapshotResponse.trace_id}</code> : null}
        </div>
        <div>
          <span>Repositories</span>
          <strong>{snapshotResponse.snapshot.repos.length}</strong>
        </div>
        <div>
          <span>Issue facts</span>
          <strong>{snapshotResponse.snapshot.issues.length}</strong>
        </div>
        {sourceEvidence.map((evidence) => (
          <div key={evidence.label}>
            <span>{humanize(evidence.label)}</span>
            <strong>{evidence.value}</strong>
          </div>
        ))}
      </section>

      {!snapshotResponse.snapshot.issues.length ? (
        <EngineeringEmpty
          detail="The current Launchplane-assembled snapshot contains no issue or pull-request facts to rank. No synthetic queue is created."
          icon={GitBranch}
          title="No work graph issues"
        />
      ) : (
        <>
          <div className="engineering-status-toolbar">
            <div>
              <span>Stateless ranking</span>
              <strong>Generated browser rank operation</strong>
            </div>
            <EngineeringResourceControls
              cancel={rankResource.cancel}
              refresh={rankResource.refresh}
              refreshLabel="Rank snapshot"
              state={rankResource.state}
            />
          </div>
          <EngineeringResourceGate
            noun="work-graph ranking"
            refresh={rankResource.refresh}
            state={rankResource.state}
          >
            {(rankData) => (
              <WorkGraphRankContent
                data={rankData}
                recommendationFilter={recommendationFilter}
                setRecommendationFilter={setRecommendationFilter}
                setStateFilter={setStateFilter}
                stateFilter={stateFilter}
              />
            )}
          </EngineeringResourceGate>
        </>
      )}
    </div>
  );
}

function WorkGraphRankContent({
  data,
  recommendationFilter,
  setRecommendationFilter,
  setStateFilter,
  stateFilter,
}: {
  data: WorkGraphRankViewData;
  recommendationFilter: WorkGraphRecommendationFilter;
  setRecommendationFilter: (filter: WorkGraphRecommendationFilter) => void;
  setStateFilter: (filter: WorkGraphStateFilter) => void;
  stateFilter: WorkGraphStateFilter;
}) {
  const filteredItems = useMemo(
    () =>
      filterWorkGraphItems(
        data.queue.items,
        stateFilter,
        recommendationFilter,
      ),
    [data.queue.items, recommendationFilter, stateFilter],
  );
  const readyCount = data.queue.items.filter((item) => item.state === "ready").length;
  const blockedCount = data.queue.items.filter(
    (item) => item.state === "blocked",
  ).length;

  return (
    <>
      <section className="engineering-metric-grid" aria-label="Work graph summary">
        <Metric label="Ranked" value={data.queue.items.length} />
        <Metric label="Ready" value={readyCount} tone={readyCount ? "pending" : "pass"} />
        <Metric
          label="Blocked"
          value={blockedCount}
          tone={blockedCount ? "blocked" : "pass"}
        />
        <Metric label="Hidden" value={data.queue.hidden_count} />
        <Metric label="Generated" value={formatTime(data.queue.generated_at)} />
      </section>
      {data.traceId ? <code className="engineering-route-trace">{data.traceId}</code> : null}

      <div className="engineering-filter-bar">
        <span>
          <Filter size={15} aria-hidden="true" />
          Queue filters
        </span>
        <label>
          <span>State</span>
          <select
            value={stateFilter}
            onChange={(event) =>
              setStateFilter(event.target.value as WorkGraphStateFilter)
            }
          >
            <option value="all">All states</option>
            <option value="ready">Ready</option>
            <option value="blocked">Blocked</option>
            <option value="waiting">Waiting</option>
          </select>
        </label>
        <label>
          <span>Recommendation</span>
          <select
            value={recommendationFilter}
            onChange={(event) =>
              setRecommendationFilter(
                event.target.value as WorkGraphRecommendationFilter,
              )
            }
          >
            <option value="all">All recommendations</option>
            <option value="quick_win">Quick win</option>
            <option value="deep_work">Deep work</option>
            <option value="switch_projects">Switch projects</option>
            <option value="blocked_cleanup">Blocked cleanup</option>
            <option value="attention_needed">Attention needed</option>
            <option value="watch">Watch</option>
          </select>
        </label>
        <strong>{filteredItems.length} visible</strong>
      </div>

      {!data.queue.items.length ? (
        <EngineeringEmpty
          detail="The service accepted the snapshot, but no queue items remained visible after ranking."
          icon={Route}
          title="No ranked work"
        />
      ) : !filteredItems.length ? (
        <EngineeringEmpty
          detail="Change the state or recommendation filter to reveal other ranked items."
          icon={Filter}
          title="No work matches these filters"
        />
      ) : (
        <ol className="engineering-work-list">
          {filteredItems.map((item) => (
            <li key={`${item.repository}:${item.number}`}>
              <article className="engineering-work-item">
                <div className="engineering-work-rank">
                  <strong>{item.score}</strong>
                  <span>score</span>
                </div>
                <div className="engineering-work-body">
                  <div className="engineering-work-heading">
                    <div>
                      <span className="engineering-kicker">
                        {item.repository} · #{item.number}
                      </span>
                      <h2>{item.title}</h2>
                    </div>
                    <span
                      className="engineering-status-chip"
                      data-status={workGraphStateStatus(item.state)}
                    >
                      <StatusIcon status={workGraphStateStatus(item.state)} />
                      {item.state}
                    </span>
                  </div>
                  <div className="engineering-chip-row">
                    <span>{item.focus}</span>
                    <span>{item.manager || "Unassigned"}</span>
                    <span>{humanize(item.recommendation)}</span>
                    <span>{item.safe_to_start ? "Safe to start" : "Review first"}</span>
                    <span>{formatTime(item.updated_at)}</span>
                  </div>
                  {item.why_now ? (
                    <p className="engineering-work-why">{item.why_now}</p>
                  ) : null}
                  <div className="engineering-work-columns">
                    <div>
                      <span>Finish line</span>
                      <p>{item.finish_line || "No finish line recorded."}</p>
                    </div>
                    <div>
                      <span>Next action</span>
                      <p>{item.next_action || "Inspect the source issue."}</p>
                    </div>
                  </div>
                  {item.reasons.length ? (
                    <ul className="engineering-reason-list">
                      {item.reasons.map((reason) => (
                        <li key={reason.code}>
                          <strong>{humanize(reason.code)}</strong>
                          <span>{reason.detail}</span>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  {item.evidence.length ? (
                    <div className="engineering-evidence-list">
                      {item.evidence.map((evidence) => (
                        <span data-trust={evidence.state} key={evidence.code}>
                          <strong>{humanize(evidence.code)}</strong>
                          {evidence.detail}
                        </span>
                      ))}
                    </div>
                  ) : null}
                  <div className="engineering-link-row">
                    <ExternalEvidenceLink
                      href={item.source_of_truth_url || item.url}
                      label="Open source issue"
                    />
                    {item.handoff_url && item.handoff_url !== item.source_of_truth_url ? (
                      <ExternalEvidenceLink
                        href={item.handoff_url}
                        label="Open handoff"
                      />
                    ) : null}
                  </div>
                </div>
              </article>
            </li>
          ))}
        </ol>
      )}
    </>
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

function ExternalEvidenceLink({ href, label }: { href: string; label: string }) {
  const url = safeExternalUrl(href);
  if (!url) {
    return null;
  }
  return (
    <a
      aria-label={`${label} (opens in a new tab)`}
      href={url.href}
      rel="noreferrer"
      target="_blank"
    >
      {label}
      <ExternalLink size={14} aria-hidden="true" />
    </a>
  );
}

function humanize(value: string): string {
  return value.replaceAll("_", " ");
}
