import {
  ExternalLink,
  History,
  Search,
  ShieldOff,
  UserCheck,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  evaluateOwnerAcceptance,
  LaunchplaneApiError,
  readOwnerAcceptanceQueue,
  writeOwnerAcceptanceEvent,
  type OwnerAcceptanceDecision,
  type OwnerAcceptanceEventMutationResponse,
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
import { useBrowserOperationController } from "./use-browser-operation";
import {
  ownerAcceptanceFailure,
  ownerAcceptanceFailureCertainty,
  ownerAcceptanceOperationScope,
  ownerAcceptanceRequest,
  type OwnerAcceptanceHumanAction,
} from "./owner-acceptance-operation";

import type {
  OwnerAcceptanceBinding,
  OwnerAcceptanceEventEnvelope,
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
      <EngineeringBoundaryNote title="Shadow mode — recorded evidence and Current controls">
        All decisions are <code>mode: shadow</code>, <code>authoritative: false</code>,{" "}
        <code>enforcement_effect: none</code>. Showing at most {QUEUE_LIMIT} entries,
        newest-first. Queue entries are{" "}
        <strong>Recorded</strong> — derived from the persisted acceptance event ledger
        with no live GitHub calls. Use the Exact Lookup pane below for a{" "}
        <strong>Current</strong> live evaluation and binding-scoped Owner actions. Recorded
        queue rows remain read-only.
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
  const [eventWriteAuthorized, setEventWriteAuthorized] = useState<boolean | null>(null);
  const [driftMessage, setDriftMessage] = useState("");
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
    setEventWriteAuthorized(null);
    setDriftMessage("");
    setError(null);

    try {
      if (fixtureMode) {
        const fixtures = await loadDevFixtures();
        const result = fixtures.ownerAcceptanceEvaluationForFixture(fixtureMode);
        if (!controller.signal.aborted) {
          setDecision(result);
          setEventWriteAuthorized(fixtureMode !== "empty");
        }
      } else {
        const result = await evaluateOwnerAcceptance(repo, pr, controller.signal);
        if (!controller.signal.aborted) {
          setDecision(result.decision);
          setEventWriteAuthorized(result.viewer_capabilities.event_write_authorized);
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

  const refreshCurrentEvaluation = useCallback(async (reviewedBinding: OwnerAcceptanceBinding) => {
    const repo = reviewedBinding.repository;
    const pr = reviewedBinding.pull_request_number;
    try {
      const evaluation = fixtureMode
        ? null
        : await evaluateOwnerAcceptance(repo, pr);
      const nextDecision = fixtureMode
        ? fixtureMode === "missing"
          ? fixtureDecisionWithBindingDigest(
              (await loadDevFixtures()).ownerAcceptanceEvaluationForFixture(fixtureMode),
              "b".repeat(64),
            )
          : (await loadDevFixtures()).ownerAcceptanceEvaluationForFixture(fixtureMode)
        : evaluation!.decision;
      setDecision(nextDecision);
      setEventWriteAuthorized(
        fixtureMode
          ? fixtureMode !== "empty"
          : evaluation!.viewer_capabilities.event_write_authorized,
      );
      setDriftMessage(
        "The reviewed binding changed. Current evidence was refreshed; review the new binding and explicitly submit again.",
      );
      setError(null);
    } catch (err: unknown) {
      const apiErr = err as LaunchplaneApiError;
      setError(apiErr?.message || "Current Owner acceptance evidence could not be refreshed.");
    }
  }, [fixtureMode]);

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
            <>
              <OwnerAcceptanceProductList products={decision.products} />
              {driftMessage ? (
                <p className="engineering-owner-action-message" role="alert">
                  {driftMessage}
                </p>
              ) : null}
              {eventWriteAuthorized ? (
                <div className="engineering-owner-acceptance-actions">
                  {decision.products.map((product) =>
                    product.binding ? (
                      <OwnerAcceptanceActionPanel
                        key={`${product.product}:${product.system}:${product.action}:${product.environment}`}
                        binding={product.binding}
                        decision={decision}
                        fixtureMode={fixtureMode}
                        onBindingChanged={refreshCurrentEvaluation}
                        onDecision={(nextDecision) => {
                          setDecision(nextDecision);
                          setDriftMessage("");
                        }}
                      />
                    ) : null,
                  )}
                </div>
              ) : eventWriteAuthorized === false ? (
                <OwnerAcceptanceReadOnlyNotice />
              ) : null}
            </>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

function OwnerAcceptanceReadOnlyNotice() {
  return (
    <section
      className="engineering-owner-acceptance-read-only"
      aria-label="Read-only Owner acceptance visibility"
    >
      <header>
        <ShieldOff size={16} aria-hidden="true" />
        <strong>Read-only engineering visibility</strong>
      </header>
      <p>
        You can inspect the Current decision and exact server-issued bindings, but this
        session is not authorized to submit Owner events. Launchplane rechecks both
        event-write access and current product Owner authority for every submission.
      </p>
    </section>
  );
}

function fixtureDecisionWithBindingDigest(
  decision: OwnerAcceptanceDecision,
  bindingSha256: string,
): OwnerAcceptanceDecision {
  const products = decision.products.map((product) => ({
    ...product,
    binding: product.binding
      ? { ...product.binding, binding_sha256: bindingSha256 }
      : null,
  }));
  return {
    ...decision,
    binding: decision.binding
      ? { ...decision.binding, binding_sha256: bindingSha256 }
      : null,
    products,
  };
}

function OwnerAcceptanceActionPanel({
  binding,
  decision,
  fixtureMode,
  onBindingChanged,
  onDecision,
}: {
  binding: OwnerAcceptanceBinding;
  decision: OwnerAcceptanceDecision;
  fixtureMode: DevFixtureMode;
  onBindingChanged: (binding: OwnerAcceptanceBinding) => Promise<void>;
  onDecision: (decision: OwnerAcceptanceDecision) => void;
}) {
  const [action, setAction] = useState<OwnerAcceptanceHumanAction>("accepted");
  const [reason, setReason] = useState("");
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  const handledDriftFailureRef = useRef<object | null>(null);
  const operation = useBrowserOperationController<
    OwnerAcceptanceEventEnvelope,
    OwnerAcceptanceEventMutationResponse
  >({
    scope: ownerAcceptanceOperationScope(binding),
    execute: async (payload, options) => {
      if (!fixtureMode) return writeOwnerAcceptanceEvent(payload, options);
      options.onDispatch?.();
      if (fixtureMode === "missing") {
        throw new LaunchplaneApiError(
          "The reviewed Owner acceptance binding changed.",
          409,
          "fixture-binding-changed",
          "owner_acceptance_binding_changed",
        );
      }
      const occurredAt = new Date().toISOString();
      const record = {
        schema_version: 1,
        event_id: `fixture-${options.idempotencyKey}`,
        acceptance_id: `fixture-${binding.binding_sha256.slice(0, 32)}`,
        binding,
        action: payload.action,
        occurred_at: occurredAt,
        source_event_kind: "browser_api" as const,
        source_event_id: options.idempotencyKey,
        reason: payload.reason ?? "",
        authorization: null,
      };
      return {
        status: "ok" as const,
        trace_id: "fixture-owner-acceptance-write",
        write_status: "written" as const,
        record,
        decision: {
          ...decision,
          status: payload.action === "accepted" ? "accepted" : payload.action,
          products: decision.products.map((product) =>
            product.binding?.binding_sha256 === binding.binding_sha256
              ? {
                  ...product,
                  status: payload.action === "accepted" ? "accepted" : payload.action,
                  current_event: record,
                }
              : product,
          ),
          current_event: record,
        },
        replayed: false,
      } satisfies OwnerAcceptanceEventMutationResponse;
    },
    failureFor: ownerAcceptanceFailure,
    failureCertainty: ownerAcceptanceFailureCertainty,
  });

  const failure = operation.state.failure;
  useEffect(() => {
    if (
      failure?.code === "owner_acceptance_binding_changed" &&
      handledDriftFailureRef.current !== failure
    ) {
      handledDriftFailureRef.current = failure;
      void onBindingChanged(binding);
    }
  }, [binding, failure, onBindingChanged]);
  useEffect(() => {
    setAction("accepted");
    setReason("");
    setConfirmRevoke(false);
  }, [binding.binding_sha256]);
  const reasonRequired = action !== "accepted";
  const busy = ["queued", "submitting"].includes(operation.state.phase);
  const canSubmit =
    (!reasonRequired || reason.trim().length > 0) &&
    (action !== "revoked" || confirmRevoke) &&
    !busy;

  return (
    <section className="engineering-owner-action-panel" aria-label={`Owner action for ${binding.product}`}>
      <header>
        <div><strong>{binding.product}</strong><span>{binding.system} · {binding.action} · {binding.environment}</span></div>
        <code>{binding.binding_sha256.slice(0, 12)}</code>
      </header>
      <p>Act only on this Current binding. Launchplane revalidates the exact change and your Owner authority at write time.</p>
      <label>
        <span>Owner action</span>
        <select value={action} disabled={operation.state.requiresIdempotencyContinuity} onChange={(event) => {
          setAction(event.target.value as OwnerAcceptanceHumanAction);
          setConfirmRevoke(false);
        }}>
          <option value="accepted">Accept</option>
          <option value="changes_requested">Request changes</option>
          <option value="revoked">Revoke</option>
        </select>
      </label>
      {reasonRequired ? <label><span>Reason</span><textarea value={reason} maxLength={4000} disabled={operation.state.requiresIdempotencyContinuity} onChange={(event) => setReason(event.target.value)} /></label> : null}
      {action === "revoked" ? <label className="engineering-owner-action-confirm"><input type="checkbox" checked={confirmRevoke} disabled={operation.state.requiresIdempotencyContinuity} onChange={(event) => setConfirmRevoke(event.target.checked)} /><span>I confirm this exact binding should be revoked.</span></label> : null}
      <div className="engineering-owner-action-buttons">
        <button className="button button-primary" type="button" disabled={!canSubmit} onClick={async () => {
          const response = await operation.run(ownerAcceptanceRequest(binding, action, reason));
          if (response) {
            onDecision(response.decision);
            setAction("accepted");
            setReason("");
            setConfirmRevoke(false);
          }
        }}>{busy ? "Submitting…" : "Submit Owner action"}</button>
        {busy ? <button className="button" type="button" onClick={operation.cancel}>Cancel wait</button> : null}
      </div>
      {failure && failure.code !== "owner_acceptance_binding_changed" ? <p className="engineering-owner-action-message" role="alert">{failure.message}{failure.traceId ? <code>{failure.traceId}</code> : null}</p> : null}
      {operation.state.receipt ? <p className="engineering-owner-action-message" data-tone="success">{operation.state.receipt.replayed ? "Owner action was already recorded (idempotent replay)." : "Owner action recorded in shadow mode."} No merge or production authority was granted.<code>{operation.state.receipt.traceId}</code></p> : null}
      {operation.state.requiresIdempotencyContinuity ? <p className="engineering-owner-action-message" role="status">Outcome uncertain. Retry only this unchanged action; the idempotency key is preserved.</p> : null}
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
              : "No recorded Owner acceptance events yet. Owners do not appear here until they submit an action; use Current evaluation above to inspect a never-acted PR."
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
