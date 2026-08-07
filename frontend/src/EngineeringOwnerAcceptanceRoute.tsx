import {
  ExternalLink,
  History,
  ShieldOff,
  UserCheck,
} from "lucide-react";
import { useCallback, useMemo, useState } from "react";

import { readOwnerAcceptanceQueue } from "./api";
import { loadDevFixtures, type DevFixtureMode } from "./dev-fixture-loader";
import {
  filterOwnerAcceptanceEntries,
  ownerAcceptanceDecisionTone,
  type OwnerAcceptanceStatusFilter,
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

import type {
  OwnerAcceptanceBinding,
  OwnerAcceptanceProductDecision,
  OwnerAcceptanceQueueEntry,
  OwnerAcceptanceQueueResponse,
} from "./generated/openapi.ts";

const QUEUE_LIMIT = 50;

export function EngineeringOwnerAcceptanceRoute({
  fixtureMode,
}: {
  fixtureMode: DevFixtureMode;
}) {
  const [statusFilter, setStatusFilter] = useState<OwnerAcceptanceStatusFilter>("all");
  const [repositoryFilter, setRepositoryFilter] = useState("");

  const loader = useCallback(
    async (
      signal: AbortSignal,
      reason: EngineeringLoadReason,
    ): Promise<OwnerAcceptanceQueueResponse> => {
      if (fixtureMode) {
        const fixtures = await loadDevFixtures();
        fixtures.assertEngineeringRefreshAvailable(reason);
        await fixtures.waitForEngineeringFixture(signal);
        return fixtures.ownerAcceptanceForFixture(fixtureMode);
      }
      return readOwnerAcceptanceQueue({}, signal);
    },
    [fixtureMode],
  );

  const resource = useEngineeringResource(loader, `owner-acceptance:${fixtureMode}`);

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
      description="Inspect current Owner acceptance decisions for repository pull requests. Queue candidates are server-derived from engineering review records and acceptance event history. No Owner mutations are exposed here."
      icon={UserCheck}
      title="Owner acceptance"
      view="owner-acceptance"
    >
      <EngineeringBoundaryNote title="Shadow mode — read only">
        All decisions are <code>mode: shadow</code>, <code>authoritative: false</code>,{" "}
        <code>enforcement_effect: none</code>. Showing at most {QUEUE_LIMIT} entries,
        newest-first. Queue candidates are assembled server-side from engineering review
        decision records and acceptance event history — the browser supplies only optional
        display filters.
      </EngineeringBoundaryNote>
      <EngineeringResourceGate
        noun="Owner acceptance queue"
        refresh={resource.refresh}
        state={resource.state}
      >
        {(data) => (
          <OwnerAcceptanceContent
            data={data}
            repositoryFilter={repositoryFilter}
            statusFilter={statusFilter}
            onRepositoryFilter={setRepositoryFilter}
            onStatusFilter={setStatusFilter}
          />
        )}
      </EngineeringResourceGate>
    </EngineeringRouteFrame>
  );
}

function OwnerAcceptanceContent({
  data,
  repositoryFilter,
  statusFilter,
  onRepositoryFilter,
  onStatusFilter,
}: {
  data: OwnerAcceptanceQueueResponse;
  repositoryFilter: string;
  statusFilter: OwnerAcceptanceStatusFilter;
  onRepositoryFilter: (v: string) => void;
  onStatusFilter: (v: OwnerAcceptanceStatusFilter) => void;
}) {
  const entries = useMemo(
    () => filterOwnerAcceptanceEntries(data.entries, statusFilter, repositoryFilter),
    [data.entries, statusFilter, repositoryFilter],
  );

  const pending = data.entries.filter(
    (e) => e.owner_acceptance_decision.status === "pending",
  ).length;
  const accepted = data.entries.filter(
    (e) => e.owner_acceptance_decision.status === "accepted",
  ).length;
  const actioned = data.entries.filter((e) =>
    ["changes_requested", "revoked"].includes(e.owner_acceptance_decision.status),
  ).length;

  return (
    <div className="engineering-owner-acceptance">
      <section className="engineering-metric-grid" aria-label="Owner acceptance summary">
        <div className="engineering-metric" data-tone="unknown">
          <span>Total</span>
          <strong>{data.entry_count}</strong>
        </div>
        <div className="engineering-metric" data-tone={pending ? "pending" : "pass"}>
          <span>Pending</span>
          <strong>{pending}</strong>
        </div>
        <div className="engineering-metric" data-tone={accepted ? "pass" : "unknown"}>
          <span>Accepted</span>
          <strong>{accepted}</strong>
        </div>
        <div className="engineering-metric" data-tone={actioned ? "blocked" : "pass"}>
          <span>Actioned</span>
          <strong>{actioned}</strong>
        </div>
      </section>

      <section className="engineering-evidence-strip" aria-label="Queue provenance">
        <div>
          <span>Generated</span>
          <strong>{formatTime(data.generated_at)}</strong>
          {data.trace_id ? <code>{data.trace_id}</code> : null}
        </div>
        <div>
          <span>Mode</span>
          <strong>{data.mode}</strong>
        </div>
        <div>
          <span>Authoritative</span>
          <strong>{String(data.authoritative)}</strong>
        </div>
        <div>
          <span>Enforcement</span>
          <strong>{data.enforcement_effect}</strong>
        </div>
      </section>

      <div className="engineering-filter-row" role="search" aria-label="Filter queue">
        <label>
          <span>Status</span>
          <select
            value={statusFilter}
            onChange={(e) => onStatusFilter(e.target.value as OwnerAcceptanceStatusFilter)}
          >
            <option value="all">All statuses</option>
            <option value="pending">Pending</option>
            <option value="accepted">Accepted</option>
            <option value="changes_requested">Changes requested</option>
            <option value="revoked">Revoked</option>
            <option value="stale">Stale</option>
            <option value="not_required">Not required</option>
            <option value="unavailable">Unavailable</option>
          </select>
        </label>
        <label>
          <span>Repository</span>
          <input
            type="search"
            placeholder="owner/repo"
            value={repositoryFilter}
            onChange={(e) => onRepositoryFilter(e.target.value)}
          />
        </label>
        {(statusFilter !== "all" || repositoryFilter) ? (
          <button
            className="button"
            type="button"
            onClick={() => {
              onStatusFilter("all");
              onRepositoryFilter("");
            }}
          >
            Clear filters
          </button>
        ) : null}
      </div>

      {!entries.length ? (
        <EngineeringEmpty
          detail={
            statusFilter !== "all" || repositoryFilter
              ? "No queue entries match the current filters."
              : "No Owner acceptance candidates found. Queue candidates appear when engineering review decision records or acceptance events exist."
          }
          icon={History}
          title="No queue entries"
        />
      ) : (
        <ol className="engineering-owner-acceptance-list">
          {entries.map((entry) => (
            <li key={`${entry.repository}#${entry.pull_request_number}`}>
              <OwnerAcceptanceEntryCard entry={entry} />
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function OwnerAcceptanceEntryCard({ entry }: { entry: OwnerAcceptanceQueueEntry }) {
  const decision = entry.owner_acceptance_decision;
  const tone = ownerAcceptanceDecisionTone(decision.status);
  const prUrl = safeExternalUrl(
    `https://github.com/${entry.repository}/pull/${entry.pull_request_number}`,
  );

  return (
    <article className="engineering-owner-acceptance-item">
      <header>
        <div>
          <span className="engineering-kicker">
            {entry.repository} · PR #{entry.pull_request_number}
          </span>
          <h2>
            {entry.repository} #{entry.pull_request_number}
          </h2>
        </div>
        <span className="engineering-status-chip" data-status={tone}>
          <StatusIcon status={tone} />
          {humanizeStatus(decision.status)}
        </span>
      </header>

      <div className="engineering-chip-row">
        <span data-mode={entry.mode}>{entry.mode}</span>
        <span>enforcement: {entry.enforcement_effect}</span>
        {decision.evaluated_at ? (
          <span>Evaluated {formatTime(decision.evaluated_at)}</span>
        ) : null}
      </div>

      <div className="engineering-work-columns">
        <div>
          <span>Next action</span>
          <p>{entry.next_action || "No next action recorded."}</p>
        </div>
        <div>
          <span>Reason</span>
          <p>{decision.reason_code ? humanizeStatus(decision.reason_code) : "—"}</p>
        </div>
      </div>

      {decision.products && decision.products.length > 0 ? (
        <OwnerAcceptanceProductList products={decision.products} />
      ) : null}

      {decision.binding ? (
        <OwnerAcceptanceBindingSection binding={decision.binding} label="Current binding" />
      ) : null}

      {entry.engineering_review_decision ? (
        <div className="engineering-provenance-row">
          <div>
            <span>Engineering review</span>
            <strong>{entry.engineering_review_decision.status}</strong>
            <small>
              Evaluated {formatTime(entry.engineering_review_decision.evaluated_at)}
            </small>
          </div>
          <div>
            <span>Review tier</span>
            <strong>{entry.engineering_review_decision.engineering_review_tier}</strong>
            <small>
              Required reviews: {entry.engineering_review_decision.required_review_count}
            </small>
          </div>
          {entry.engineering_review_decision.target.head_sha ? (
            <div>
              <span>Head SHA</span>
              <strong>
                <code>{entry.engineering_review_decision.target.head_sha.slice(0, 12)}</code>
              </strong>
              <small>
                {entry.engineering_review_decision.target.repository}
              </small>
            </div>
          ) : null}
        </div>
      ) : (
        <div className="engineering-provenance-row">
          <div>
            <span>Engineering review</span>
            <strong>No recorded decision</strong>
            <small>Candidate sourced from acceptance event history.</small>
          </div>
        </div>
      )}

      <div className="engineering-link-row">
        {prUrl ? (
          <a
            aria-label={`Open PR #${entry.pull_request_number} on GitHub (opens in a new tab)`}
            href={prUrl.href}
            rel="noreferrer"
            target="_blank"
          >
            Open PR on GitHub
            <ExternalLink size={14} aria-hidden="true" />
          </a>
        ) : null}
        <span className="engineering-passive-action">
          <ShieldOff size={14} aria-hidden="true" />
          No Owner mutation controls exposed
        </span>
      </div>
    </article>
  );
}

function OwnerAcceptanceProductList({
  products,
}: {
  products: OwnerAcceptanceProductDecision[];
}) {
  return (
    <div className="engineering-owner-acceptance-products">
      <span>Per-product decisions</span>
      <ul>
        {products.map((product) => {
          const tone = ownerAcceptanceDecisionTone(product.status);
          return (
            <li key={`${product.product}:${product.system}:${product.action}:${product.environment}`}>
              <span className="engineering-status-chip" data-status={tone}>
                <StatusIcon status={tone} />
                {humanizeStatus(product.status)}
              </span>
              <div>
                <strong>{product.product}</strong>
                <span>
                  {product.system} · {product.action} · {product.environment}
                </span>
                {product.binding ? (
                  <OwnerAcceptanceBindingSection
                    binding={product.binding}
                    label="Product binding"
                  />
                ) : null}
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function OwnerAcceptanceBindingSection({
  binding,
  label,
}: {
  binding: OwnerAcceptanceBinding;
  label: string;
}) {
  return (
    <div className="engineering-owner-acceptance-binding">
      <span>{label}</span>
      <div className="engineering-chip-row">
        <span>
          <code>{binding.binding_sha256.slice(0, 16)}…</code>
        </span>
        <span>
          head: <code>{binding.head_sha.slice(0, 12)}</code>
        </span>
        {binding.preview ? (
          <span>
            preview: {binding.preview.context}
          </span>
        ) : null}
      </div>
    </div>
  );
}

function humanizeStatus(value: string): string {
  return value.replaceAll("_", " ");
}
