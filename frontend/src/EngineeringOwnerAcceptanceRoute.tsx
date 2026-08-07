import {
  ExternalLink,
  History,
  Search,
  ShieldOff,
  UserCheck,
} from "lucide-react";
import { useCallback, useRef, useState } from "react";

import {
  evaluateOwnerAcceptance,
  readOwnerAcceptanceQueue,
  type LaunchplaneApiError,
  type OwnerAcceptanceDecision,
  type OwnerAcceptanceProductDecision,
} from "./api";
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
  const [repositoryDraft, setRepositoryDraft] = useState("");

  const loader = useCallback(
    async (
      signal: AbortSignal,
      reason: EngineeringLoadReason,
    ): Promise<OwnerAcceptanceQueueResponse> => {
      if (fixtureMode) {
        const fixtures = await loadDevFixtures();
        fixtures.assertEngineeringRefreshAvailable(reason);
        await fixtures.waitForEngineeringFixture(signal);
        const response = fixtures.ownerAcceptanceForFixture(fixtureMode);
        const entries = filterOwnerAcceptanceEntries(
          response.entries,
          statusFilter,
          repositoryFilter,
        );
        return {
          ...response,
          candidate: entries.length,
          entries,
          entry_count: entries.length,
        };
      }
      return readOwnerAcceptanceQueue(
        {
          repository: repositoryFilter.trim() || undefined,
          status: statusFilter === "all" ? undefined : statusFilter,
        },
        signal,
      );
    },
    [fixtureMode, repositoryFilter, statusFilter],
  );

  const resource = useEngineeringResource(
    loader,
    `owner-acceptance:${fixtureMode}:${statusFilter}:${repositoryFilter}`,
  );

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
      description="Inspect recorded Owner acceptance history for repository pull requests. The queue is derived solely from the acceptance event ledger — no GitHub calls. Use the exact lookup below for a current live evaluation."
      icon={UserCheck}
      title="Owner acceptance"
      view="owner-acceptance"
    >
      <EngineeringBoundaryNote title="Shadow mode — read only">
        All decisions are <code>mode: shadow</code>, <code>authoritative: false</code>,{" "}
        <code>enforcement_effect: none</code>. Showing at most {QUEUE_LIMIT} entries,
        newest-first. Queue entries are{" "}
        <strong>Recorded</strong> — derived from the persisted acceptance event ledger
        with no live GitHub calls. Use the Exact Lookup pane below for a{" "}
        <strong>Current</strong> live evaluation of any repository and PR.
      </EngineeringBoundaryNote>

      <OwnerAcceptanceLookupPane fixtureMode={fixtureMode} />

      <EngineeringResourceGate
        noun="Owner acceptance queue"
        refresh={resource.refresh}
        state={resource.state}
      >
        {(data) => (
          <OwnerAcceptanceContent
            data={data}
            repositoryDraft={repositoryDraft}
            repositoryFilter={repositoryFilter}
            statusFilter={statusFilter}
            onApplyRepositoryFilter={() => setRepositoryFilter(repositoryDraft.trim())}
            onRepositoryDraft={setRepositoryDraft}
            onRepositoryFilter={setRepositoryFilter}
            onStatusFilter={setStatusFilter}
          />
        )}
      </EngineeringResourceGate>
    </EngineeringRouteFrame>
  );
}

function OwnerAcceptanceLookupPane({ fixtureMode }: { fixtureMode: DevFixtureMode }) {
  const [repository, setRepository] = useState("");
  const [prNumber, setPrNumber] = useState("");
  const [decision, setDecision] = useState<OwnerAcceptanceDecision | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const handleLookup = useCallback(async () => {
    const repo = repository.trim();
    const pr = parseInt(prNumber, 10);
    if (!repo || !pr || pr < 1) return;

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setLoading(true);
    setDecision(null);
    setError(null);

    try {
      if (fixtureMode) {
        const fixtures = await loadDevFixtures();
        const result = fixtures.ownerAcceptanceEvaluationForFixture(fixtureMode);
        if (!controller.signal.aborted) {
          setDecision(result);
        }
      } else {
        const result = await evaluateOwnerAcceptance(repo, pr, controller.signal);
        if (!controller.signal.aborted) {
          setDecision(result.decision);
        }
      }
    } catch (err: unknown) {
      if (!controller.signal.aborted) {
        const apiErr = err as LaunchplaneApiError;
        setError(
          apiErr?.message
            ? `${apiErr.message} (${apiErr.statusCode ?? "error"})`
            : "Evaluation unavailable. Check that the repository and PR exist and evidence is ready.",
        );
      }
    } finally {
      if (!controller.signal.aborted) {
        setLoading(false);
      }
    }
  }, [repository, prNumber, fixtureMode]);

  const isValid = repository.trim().includes("/") && parseInt(prNumber, 10) >= 1;

  return (
    <section className="engineering-owner-acceptance-lookup" aria-label="Exact PR lookup">
      <header>
        <Search size={14} aria-hidden="true" />
        <span>Exact lookup — Current evaluation</span>
        <small>
          Shows the live decision from the evaluation route, including never-acted PRs. Provider
          failures appear here only, not as a global error.
        </small>
      </header>
      <div className="engineering-owner-acceptance-lookup-form">
        <label>
          <span>Repository</span>
          <input
            type="text"
            placeholder="owner/repo"
            value={repository}
            onChange={(e) => setRepository(e.target.value)}
            aria-label="Repository (owner/repo)"
          />
        </label>
        <label>
          <span>PR number</span>
          <input
            type="number"
            placeholder="1234"
            min={1}
            value={prNumber}
            onChange={(e) => setPrNumber(e.target.value)}
            aria-label="Pull request number"
          />
        </label>
        <button
          className="button"
          type="button"
          disabled={!isValid || loading}
          onClick={handleLookup}
          aria-label="Look up current Owner acceptance evaluation"
        >
          {loading ? "Loading…" : "Look up"}
        </button>
      </div>
      {error ? (
        <p className="engineering-owner-acceptance-lookup-error" role="alert">
          {error}
        </p>
      ) : null}
      {decision ? (
        <div className="engineering-owner-acceptance-lookup-result" aria-label="Current evaluation result">
          <div className="engineering-chip-row">
            <span className="engineering-status-chip" data-status={ownerAcceptanceDecisionTone(decision.status)}>
              <StatusIcon status={ownerAcceptanceDecisionTone(decision.status)} />
              Current: {humanizeStatus(decision.status)}
            </span>
            <span>reason: {humanizeStatus(decision.reason_code)}</span>
            {decision.evaluated_at ? (
              <span>evaluated {formatTime(decision.evaluated_at)}</span>
            ) : null}
          </div>
          {decision.products && decision.products.length > 0 ? (
            <OwnerAcceptanceProductList products={decision.products} />
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

function OwnerAcceptanceContent({
  data,
  repositoryDraft,
  repositoryFilter,
  statusFilter,
  onApplyRepositoryFilter,
  onRepositoryDraft,
  onRepositoryFilter,
  onStatusFilter,
}: {
  data: OwnerAcceptanceQueueResponse;
  repositoryDraft: string;
  repositoryFilter: string;
  statusFilter: OwnerAcceptanceStatusFilter;
  onApplyRepositoryFilter: () => void;
  onRepositoryDraft: (v: string) => void;
  onRepositoryFilter: (v: string) => void;
  onStatusFilter: (v: OwnerAcceptanceStatusFilter) => void;
}) {
  const accepted = data.entries.filter((e) => e.ledger_status === "accepted").length;
  const actioned = data.entries.filter((e) =>
    ["changes_requested", "revoked"].includes(e.ledger_status),
  ).length;
  const staleOrUnavailable = data.entries.filter((e) =>
    ["stale", "unavailable"].includes(e.ledger_status),
  ).length;

  return (
    <div className="engineering-owner-acceptance">
      <section className="engineering-metric-grid" aria-label="Recorded ledger summary">
        <div className="engineering-metric" data-tone="unknown">
          <span>Ledger subjects</span>
          <strong>{data.total}</strong>
        </div>
        <div className="engineering-metric" data-tone={data.candidate !== data.total ? "pending" : "unknown"}>
          <span>Matching filters</span>
          <strong>{data.candidate}</strong>
        </div>
        <div className="engineering-metric" data-tone={accepted ? "pass" : "unknown"}>
          <span>Accepted shown</span>
          <strong>{accepted}</strong>
        </div>
        <div className="engineering-metric" data-tone={actioned ? "blocked" : "pass"}>
          <span>Actioned shown</span>
          <strong>{actioned}</strong>
        </div>
        {staleOrUnavailable ? (
          <div className="engineering-metric" data-tone="unknown">
            <span>Stale / unavailable shown</span>
            <strong>{staleOrUnavailable}</strong>
          </div>
        ) : null}
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
        {data.truncated ? (
          <div>
            <span>Truncated</span>
            <strong>
              yes — {data.candidate} candidates, {QUEUE_LIMIT} shown; refine filters or use
              exact lookup
            </strong>
          </div>
        ) : null}
      </section>

      <form
        className="engineering-filter-row"
        role="search"
        aria-label="Filter queue"
        onSubmit={(event) => {
          event.preventDefault();
          onApplyRepositoryFilter();
        }}
      >
        <label>
          <span>Status</span>
          <select
            value={statusFilter}
            onChange={(e) => onStatusFilter(e.target.value as OwnerAcceptanceStatusFilter)}
          >
            <option value="all">All statuses</option>
            <option value="accepted">Accepted</option>
            <option value="changes_requested">Changes requested</option>
            <option value="revoked">Revoked</option>
            <option value="stale">Stale</option>
            <option value="unavailable">Unavailable</option>
          </select>
        </label>
        <label>
          <span>Repository (substring)</span>
          <input
            type="search"
            placeholder="partial owner/repo"
            value={repositoryDraft}
            onChange={(e) => onRepositoryDraft(e.target.value)}
            aria-label="Filter by repository substring"
          />
        </label>
        <button className="button" type="submit">
          Apply repository filter
        </button>
        {(statusFilter !== "all" || repositoryDraft || repositoryFilter) ? (
          <button
            className="button"
            type="button"
            onClick={() => {
              onStatusFilter("all");
              onRepositoryDraft("");
              onRepositoryFilter("");
            }}
          >
            Clear filters
          </button>
        ) : null}
      </form>

      {!data.entries.length ? (
        <EngineeringEmpty
          detail={
            statusFilter !== "all" || repositoryFilter
              ? "No recorded entries match the current filters."
              : "No recorded Owner acceptance candidates. Entries appear when acceptance events exist in the ledger."
          }
          icon={History}
          title="No recorded entries"
        />
      ) : (
        <ol className="engineering-owner-acceptance-list">
          {data.entries.map((entry) => (
            <li key={`${entry.repository_id}:${entry.pull_request_number}:${entry.product}:${entry.system}:${entry.action}:${entry.environment}`}>
              <OwnerAcceptanceEntryCard entry={entry} />
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function OwnerAcceptanceEntryCard({ entry }: { entry: OwnerAcceptanceQueueEntry }) {
  const tone = ownerAcceptanceDecisionTone(entry.ledger_status);
  const prUrl = safeExternalUrl(
    `https://github.com/${entry.repository}/pull/${entry.pull_request_number}`,
  );

  return (
    <article className="engineering-owner-acceptance-item">
      <header>
        <div>
          <span className="engineering-kicker">
            Recorded · {entry.repository} · PR #{entry.pull_request_number}
          </span>
          <h2>
            {entry.product} — {entry.system} · {entry.action} · {entry.environment}
          </h2>
        </div>
        <span className="engineering-status-chip" data-status={tone}>
          <StatusIcon status={tone} />
          {humanizeStatus(entry.ledger_status)}
        </span>
      </header>

      <div className="engineering-chip-row">
        <span data-mode={entry.mode}>{entry.mode}</span>
        <span>enforcement: {entry.enforcement_effect}</span>
        <span>verification required: {String(entry.verification_required)}</span>
        {entry.occurred_at ? (
          <span>Recorded {formatTime(entry.occurred_at)}</span>
        ) : null}
      </div>

      <div className="engineering-work-columns">
        <div>
          <span>Next action</span>
          <p>{entry.next_action || "No next action recorded."}</p>
        </div>
        <div>
          <span>Event action</span>
          <p>{humanizeStatus(entry.latest_event.action)}</p>
        </div>
      </div>

      <OwnerAcceptanceBindingSection binding={entry.latest_binding} label="Recorded binding" />

      <div className="engineering-provenance-row">
        <div>
          <span>Event ID</span>
          <code>{entry.latest_event.event_id.slice(0, 32)}…</code>
          <small>{entry.latest_event.source_event_kind}</small>
        </div>
        <div>
          <span>Acceptance ID</span>
          <code>{entry.latest_event.acceptance_id.slice(0, 32)}…</code>
        </div>
        {entry.latest_event.authorization ? (
          <div>
            <span>Authorized by</span>
            <strong>{entry.latest_event.authorization.owner_login}</strong>
            <small>GitHub ID {entry.latest_event.authorization.owner_github_id}</small>
          </div>
        ) : null}
      </div>

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
