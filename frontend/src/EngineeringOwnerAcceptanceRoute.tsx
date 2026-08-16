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
  readOwnerAcceptanceCurrentItems,
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
  ownerAcceptanceBindingEligibility,
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
import { useAppSearchParams } from "./router";
import { ownerAcceptanceLookupFromSearch } from "./route-model";
import {
  ownerAcceptanceFailure,
  ownerAcceptanceFailureCertainty,
  ownerAcceptanceOperationScope,
  ownerAcceptanceRequest,
  type OwnerAcceptanceHumanAction,
} from "./owner-acceptance-operation";

import type {
  OwnerAcceptanceBinding,
  OwnerAcceptanceCurrentItem,
  OwnerAcceptanceCurrentItemsResponse,
  OwnerAcceptanceEventEnvelope,
  OwnerAcceptanceQueueEntry,
  OwnerAcceptanceQueueResponse,
  OwnerAcceptanceViewerBindingEligibility,
  OwnerAcceptanceViewerCapabilities,
} from "./generated/openapi.ts";

const QUEUE_LIMIT = 50;

export function EngineeringOwnerAcceptanceRoute({
  fixtureMode,
}: {
  fixtureMode: DevFixtureMode;
}) {
  const searchParams = useAppSearchParams();
  const lookup = ownerAcceptanceLookupFromSearch(searchParams.toString());
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

  const currentLoader = useCallback(
    async (
      signal: AbortSignal,
      reason: EngineeringLoadReason,
    ): Promise<OwnerAcceptanceCurrentItemsResponse> => {
      if (fixtureMode) {
        const fixtures = await loadDevFixtures();
        fixtures.assertEngineeringRefreshAvailable(reason);
        await fixtures.waitForEngineeringFixture(signal);
        return ownerAcceptanceCurrentItemsForFixture(
          fixtureMode,
          fixtures.ownerAcceptanceEvaluationForFixture(fixtureMode),
        );
      }
      return readOwnerAcceptanceCurrentItems({ limit: 10 }, signal);
    },
    [fixtureMode],
  );
  const currentResource = useEngineeringResource(
    currentLoader,
    `owner-acceptance-current:${fixtureMode}`,
  );

  return (
    <EngineeringRouteFrame
      actions={
        <EngineeringResourceControls
          cancel={() => {
            currentResource.cancel();
            resource.cancel();
          }}
          refresh={() => {
            currentResource.refresh();
            resource.refresh();
          }}
          refreshLabel="Refresh items"
          state={currentResource.state}
        />
      }
      description="Review the product behavior of current pull requests, then inspect recorded review history. Launchplane discovers current items automatically from active change-impact repositories."
      icon={UserCheck}
      title="Owner product review"
      view="owner-acceptance"
    >
      <EngineeringBoundaryNote title="Launchplane is the Owner-review authority">
        Owner acceptance is required for product-impacting changes and is evaluated against the
        exact pull request head, tree, serving preview, artifact, runtime identity, impact policy,
        and Owner policy. It remains separate from technical checks, engineering review, merge
        admission, landing, and production authorization. Current items come from open pull requests
        in repositories with active change-impact policy records and are evaluated server-side.
        Recorded entries are{" "}
        <strong>Recorded</strong> — derived from the persisted acceptance event ledger
        with no live GitHub calls. Recorded queue rows remain read-only.
      </EngineeringBoundaryNote>

      <EngineeringResourceGate
        noun="Current Owner product review items"
        refresh={currentResource.refresh}
        state={currentResource.state}
      >
        {(data) => <OwnerAcceptanceCurrentItems data={data} fixtureMode={fixtureMode} />}
      </EngineeringResourceGate>

      <details
        className="engineering-owner-acceptance-lookup-fallback"
        open={lookup.requested || undefined}
      >
        <summary>Exact lookup fallback</summary>
        <OwnerAcceptanceLookupPane
          autoLookup={lookup.valid}
          fixtureMode={fixtureMode}
          initialPullRequest={lookup.pullRequest}
          initialRepository={lookup.repository}
        />
      </details>

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

function ownerAcceptanceCurrentItemsForFixture(
  fixtureMode: Exclude<DevFixtureMode, "">,
  decision: OwnerAcceptanceDecision,
): OwnerAcceptanceCurrentItemsResponse {
  const binding = decision.binding ?? decision.products.find((product) => product.binding)?.binding;
  const items: OwnerAcceptanceCurrentItem[] =
    fixtureMode === "empty" || !binding
      ? []
      : [
          {
            repository: binding.repository,
            pull_request_number: binding.pull_request_number,
            title: "Fixture pull request requiring current Owner review",
            url: `https://github.com/${binding.repository}/pull/${binding.pull_request_number}`,
            updated_at: decision.evaluated_at,
            evaluation_status: "available",
            decision,
            error_code: "",
          },
        ];
  return {
    status: "ok",
    trace_id: `fixture-owner-acceptance-current-${fixtureMode}`,
    derivation: "active_change_impact_open_pull_requests",
    generated_at: decision.evaluated_at,
    viewer_capabilities: ownerAcceptanceFixtureViewerCapabilities(decision, fixtureMode),
    repository_count: fixtureMode === "empty" ? 0 : 1,
    repository_failure_count: 0,
    candidate_count: items.length,
    evaluated_count: items.length,
    unavailable_count: 0,
    truncated: false,
    items,
    repository_failures: [],
  };
}

function OwnerAcceptanceCurrentItems({
  data,
  fixtureMode,
}: {
  data: OwnerAcceptanceCurrentItemsResponse;
  fixtureMode: DevFixtureMode;
}) {
  if (!data.items.length && !data.repository_failures.length) {
    return (
      <EngineeringEmpty
        icon={UserCheck}
        title="No current pull requests"
        detail="Launchplane found no open pull requests in active change-impact repositories. Exact lookup remains available as a fallback."
      />
    );
  }
  return (
    <section className="engineering-owner-current" aria-label="Current Owner product review items">
      <header className="engineering-owner-current-header">
        <div>
          <span>Current items</span>
          <strong>{data.candidate_count}</strong>
        </div>
        <p>
          Automatically discovered from {data.repository_count} active change-impact {data.repository_count === 1 ? "repository" : "repositories"}.
        </p>
      </header>
      {data.truncated ? (
        <p className="engineering-owner-action-message" role="status">
          The server work bound was reached. Refresh later or use exact lookup for a missing PR.
        </p>
      ) : null}
      {data.repository_failures.map((failure) => (
        <p className="engineering-owner-acceptance-lookup-error" role="alert" key={failure.repository}>
          {failure.repository}: open pull requests could not be loaded.
        </p>
      ))}
      <div className="engineering-owner-current-list">
        {data.items.map((item) => (
          <OwnerAcceptanceCurrentItemCard
            viewerCapabilities={data.viewer_capabilities}
            fixtureMode={fixtureMode}
            item={item}
            key={`${item.repository}:${item.pull_request_number}`}
          />
        ))}
      </div>
    </section>
  );
}

function OwnerAcceptanceCurrentItemCard({
  viewerCapabilities,
  fixtureMode,
  item,
}: {
  viewerCapabilities: OwnerAcceptanceViewerCapabilities;
  fixtureMode: DevFixtureMode;
  item: OwnerAcceptanceCurrentItem;
}) {
  const [decision, setDecision] = useState(item.decision);
  const [currentViewerCapabilities, setCurrentViewerCapabilities] =
    useState(viewerCapabilities);
  const [driftMessage, setDriftMessage] = useState("");
  const [error, setError] = useState("");
  const pullRequestUrl = safeExternalUrl(item.url);
  useEffect(() => {
    setDecision(item.decision);
    setCurrentViewerCapabilities(viewerCapabilities);
    setDriftMessage("");
    setError("");
  }, [item.decision, item.error_code, item.evaluation_status, viewerCapabilities]);
  const refreshCurrentEvaluation = useCallback(async (binding: OwnerAcceptanceBinding) => {
    try {
      const evaluation = fixtureMode
        ? null
        : await evaluateOwnerAcceptance(binding.repository, binding.pull_request_number);
      const nextDecision = fixtureMode
        ? fixtureMode === "missing"
          ? fixtureDecisionWithBindingDigest(
              (await loadDevFixtures()).ownerAcceptanceEvaluationForFixture(fixtureMode),
              "b".repeat(64),
            )
          : (await loadDevFixtures()).ownerAcceptanceEvaluationForFixture(fixtureMode)
        : evaluation!.decision;
      setDecision(nextDecision);
      setCurrentViewerCapabilities(
        fixtureMode
          ? ownerAcceptanceFixtureViewerCapabilities(nextDecision, fixtureMode)
          : evaluation!.viewer_capabilities,
      );
      setDriftMessage(
        "The reviewed binding changed. Current evidence was refreshed; review the new binding and explicitly submit again.",
      );
      setError("");
    } catch (refreshError: unknown) {
      const apiError = refreshError as LaunchplaneApiError;
      setError(apiError?.message || "Current Owner acceptance evidence could not be refreshed.");
    }
  }, [fixtureMode]);

  return (
    <article className="engineering-owner-current-item">
      <header>
        <div>
          <span>{item.repository} · PR #{item.pull_request_number}</span>
          <h2>{item.title}</h2>
          <small>updated {formatTime(item.updated_at)}</small>
        </div>
        {pullRequestUrl ? (
          <a
            aria-label={`Open ${item.repository} pull request #${item.pull_request_number} on GitHub`}
            href={pullRequestUrl.toString()}
            target="_blank"
            rel="noreferrer"
          >
            Open PR <ExternalLink size={14} aria-hidden="true" />
          </a>
        ) : null}
      </header>
      {item.evaluation_status === "unavailable" || !decision ? (
        <p className="engineering-owner-acceptance-lookup-error" role="alert">
          Current evaluation is unavailable. Exact lookup may provide more specific evidence later.
        </p>
      ) : (
        <OwnerAcceptanceDecisionDetails
          decision={decision}
          driftMessage={driftMessage}
          viewerCapabilities={currentViewerCapabilities}
          fixtureMode={fixtureMode}
          onBindingChanged={refreshCurrentEvaluation}
          onDecision={(nextDecision) => {
            setDecision(nextDecision);
            setDriftMessage("");
          }}
        />
      )}
      {error ? (
        <p className="engineering-owner-acceptance-lookup-error" role="alert">
          {error}
        </p>
      ) : null}
    </article>
  );
}

function OwnerAcceptanceLookupPane({
  autoLookup,
  fixtureMode,
  initialPullRequest,
  initialRepository,
}: {
  autoLookup: boolean;
  fixtureMode: DevFixtureMode;
  initialPullRequest: string;
  initialRepository: string;
}) {
  const [repository, setRepository] = useState(initialRepository);
  const [prNumber, setPrNumber] = useState(initialPullRequest);
  const [decision, setDecision] = useState<OwnerAcceptanceDecision | null>(null);
  const [viewerCapabilities, setViewerCapabilities] =
    useState<OwnerAcceptanceViewerCapabilities | null>(null);
  const [driftMessage, setDriftMessage] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const lastAutoLookupRef = useRef("");

  const runLookup = useCallback(async (repositoryValue: string, prValue: string) => {
    const repo = repositoryValue.trim();
    const pr = parseInt(prValue, 10);
    if (!repo || !pr || pr < 1) return;

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setLoading(true);
    setDecision(null);
    setViewerCapabilities(null);
    setDriftMessage("");
    setError(null);

    try {
      if (fixtureMode) {
        const fixtures = await loadDevFixtures();
        const result = fixtures.ownerAcceptanceEvaluationForFixture(fixtureMode);
        if (!controller.signal.aborted) {
          setDecision(result);
          setViewerCapabilities(ownerAcceptanceFixtureViewerCapabilities(result, fixtureMode));
        }
      } else {
        const result = await evaluateOwnerAcceptance(repo, pr, controller.signal);
        if (!controller.signal.aborted) {
          setDecision(result.decision);
          setViewerCapabilities(result.viewer_capabilities);
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
  }, [fixtureMode]);

  const handleLookup = useCallback(
    () => runLookup(repository, prNumber),
    [prNumber, repository, runLookup],
  );

  useEffect(() => {
    setRepository(initialRepository);
    setPrNumber(initialPullRequest);
    if (!autoLookup) {
      lastAutoLookupRef.current = "";
      return;
    }
    const lookupKey = `${initialRepository}:${initialPullRequest}`;
    if (lastAutoLookupRef.current === lookupKey) {
      return;
    }
    lastAutoLookupRef.current = lookupKey;
    void runLookup(initialRepository, initialPullRequest);
  }, [autoLookup, initialPullRequest, initialRepository, runLookup]);

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
      setViewerCapabilities(
        fixtureMode
          ? ownerAcceptanceFixtureViewerCapabilities(nextDecision, fixtureMode)
          : evaluation!.viewer_capabilities,
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
          onClick={() => void handleLookup()}
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
          <OwnerAcceptanceDecisionDetails
            decision={decision}
            driftMessage={driftMessage}
            viewerCapabilities={viewerCapabilities ?? {
              event_write_authorized: false,
              bindings: [],
            }}
            fixtureMode={fixtureMode}
            onBindingChanged={refreshCurrentEvaluation}
            onDecision={(nextDecision) => {
              setDecision(nextDecision);
              setDriftMessage("");
            }}
            showReadOnlyNotice={viewerCapabilities?.event_write_authorized === false}
          />
        </div>
      ) : null}
    </section>
  );
}

function OwnerAcceptanceDecisionDetails({
  decision,
  driftMessage,
  viewerCapabilities,
  fixtureMode,
  onBindingChanged,
  onDecision,
  showReadOnlyNotice = true,
}: {
  decision: OwnerAcceptanceDecision;
  driftMessage: string;
  viewerCapabilities: OwnerAcceptanceViewerCapabilities;
  fixtureMode: DevFixtureMode;
  onBindingChanged: (binding: OwnerAcceptanceBinding) => Promise<void>;
  onDecision: (decision: OwnerAcceptanceDecision) => void;
  showReadOnlyNotice?: boolean;
}) {
  return (
    <>
      <div className="engineering-chip-row">
        <span
          className="engineering-status-chip"
          data-status={ownerAcceptanceDecisionTone(decision.status)}
        >
          <StatusIcon status={ownerAcceptanceDecisionTone(decision.status)} />
          Owner product review: {humanizeStatus(decision.status)}
        </span>
        <span>reason: {humanizeStatus(decision.reason_code)}</span>
        {decision.evaluated_at ? (
          <span>evaluated {formatTime(decision.evaluated_at)}</span>
        ) : null}
      </div>
      {decision.products.length > 0 ? (
        <>
          <OwnerAcceptanceProductList products={decision.products} />
          {driftMessage ? (
            <p className="engineering-owner-action-message" role="alert">
              {driftMessage}
            </p>
          ) : null}
          {viewerCapabilities.event_write_authorized ? (
            <div className="engineering-owner-acceptance-actions">
              {decision.products.map((product) => {
                if (!product.binding) return null;
                const eligibility = ownerAcceptanceBindingEligibility(
                  viewerCapabilities,
                  product.binding.binding_sha256,
                );
                return eligibility?.can_submit_event ? (
                  <OwnerAcceptanceActionPanel
                    key={`${product.product}:${product.system}:${product.action}:${product.environment}`}
                    binding={product.binding}
                    decision={decision}
                    eligibility={eligibility}
                    fixtureMode={fixtureMode}
                    onBindingChanged={onBindingChanged}
                    onDecision={onDecision}
                  />
                ) : (
                  <OwnerAcceptanceIneligibleNotice
                    binding={product.binding}
                    eligibility={eligibility}
                    key={`${product.product}:${product.system}:${product.action}:${product.environment}`}
                  />
                );
              })}
            </div>
          ) : showReadOnlyNotice ? (
            <OwnerAcceptanceReadOnlyNotice />
          ) : null}
        </>
      ) : null}
    </>
  );
}

function OwnerAcceptanceIneligibleNotice({
  binding,
  eligibility,
}: {
  binding: OwnerAcceptanceBinding;
  eligibility: OwnerAcceptanceViewerBindingEligibility | undefined;
}) {
  const notOwner = eligibility?.reason_code === "not_current_product_owner";
  const selfReviewDenied = eligibility?.reason_code === "self_review_denied";
  const heading = selfReviewDenied
    ? "Self-review denied for this change"
    : notOwner
      ? "Not a current product Owner"
      : "Owner eligibility unavailable";
  return (
    <section
      className="engineering-owner-acceptance-read-only"
      aria-label={`Owner product review unavailable for ${binding.product}`}
    >
      <header>
        <ShieldOff size={16} aria-hidden="true" />
        <strong>{heading}</strong>
      </header>
      <p>
        {selfReviewDenied
          ? `You contributed to this exact ${binding.product} change, so product policy does not let you record its product review. A different current product Owner must review it.`
          : notOwner
            ? `You can inspect this exact ${binding.product} change, but only a current product Owner can record its product review.`
            : `You can inspect this exact ${binding.product} change, but Launchplane could not verify that this session may record its product review.`}{" "}
        Owner authority is revalidated for every submission.
      </p>
    </section>
  );
}

function OwnerAcceptanceReadOnlyNotice() {
  return (
    <section
      className="engineering-owner-acceptance-read-only"
      aria-label="Read-only product review visibility"
    >
      <header>
        <ShieldOff size={16} aria-hidden="true" />
        <strong>Read-only product review visibility</strong>
      </header>
      <p>
        You can inspect the current product-review decision and exact server-issued
        bindings, but this session is not authorized to submit Owner events. Launchplane
        rechecks both event-write access and current product Owner authority for every
        submission.
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

function ownerAcceptanceFixtureViewerCapabilities(
  decision: OwnerAcceptanceDecision,
  fixtureMode: DevFixtureMode,
): OwnerAcceptanceViewerCapabilities {
  const eventWriteAuthorized = fixtureMode !== "empty";
  const viewer = new URLSearchParams(window.location.search).get("viewer");
  const currentOwner = viewer !== "non-owner";
  const canAccept = currentOwner && viewer !== "contributor";
  return {
    event_write_authorized: eventWriteAuthorized,
    bindings: eventWriteAuthorized
      ? decision.products.flatMap((product) =>
          product.binding
            ? [
                {
                  schema_version: 1,
                  binding_sha256: product.binding.binding_sha256,
                  product: product.product,
                  system: product.system,
                  action: product.action,
                  environment: product.environment,
                  can_submit_event: currentOwner,
                  can_accept: canAccept,
                  can_request_changes: currentOwner,
                  can_revoke: currentOwner,
                  reason_code: canAccept
                    ? "current_product_owner"
                    : currentOwner
                      ? "self_review_denied"
                      : "not_current_product_owner",
                } satisfies OwnerAcceptanceViewerBindingEligibility,
              ]
            : [],
        )
      : [],
  };
}

function OwnerAcceptanceActionPanel({
  binding,
  decision,
  eligibility,
  fixtureMode,
  onBindingChanged,
  onDecision,
}: {
  binding: OwnerAcceptanceBinding;
  decision: OwnerAcceptanceDecision;
  eligibility: OwnerAcceptanceViewerBindingEligibility;
  fixtureMode: DevFixtureMode;
  onBindingChanged: (binding: OwnerAcceptanceBinding) => Promise<void>;
  onDecision: (decision: OwnerAcceptanceDecision) => void;
}) {
  const defaultAction = ownerAcceptanceDefaultAction(eligibility);
  const [action, setAction] = useState<OwnerAcceptanceHumanAction>(defaultAction);
  const [reason, setReason] = useState("");
  const [resolutionSummary, setResolutionSummary] = useState("");
  const [resolutionReferences, setResolutionReferences] = useState("");
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
        subject_sequence: (decision.current_event?.subject_sequence ?? 0) + 1,
        binding,
        action: payload.action,
        occurred_at: occurredAt,
        source_event_kind: "browser_api" as const,
        source_event_id: options.idempotencyKey,
        reason: payload.reason ?? "",
        resolution: payload.resolution ?? null,
        authorization: null,
      };
      return {
        status: "ok" as const,
        trace_id: "fixture-owner-acceptance-write",
        write_status: "written" as const,
        record,
        semantics: {
          human_action_semantics:
            payload.action === "accepted"
              ? ("product_review_accepted" as const)
              : payload.action === "revoked"
                ? ("product_review_revoked" as const)
                : ("product_review_changes_requested" as const),
        },
        decision: {
          ...decision,
          status: payload.action === "accepted" ? "accepted" : payload.action,
          human_action_semantics:
            payload.action === "accepted"
              ? ("product_review_accepted" as const)
              : payload.action === "revoked"
                ? ("product_review_revoked" as const)
                : ("product_review_changes_requested" as const),
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
    setAction(defaultAction);
    setReason("");
    setResolutionSummary("");
    setResolutionReferences("");
    setConfirmRevoke(false);
  }, [binding.binding_sha256, defaultAction]);
  const reasonRequired = action !== "accepted";
  const currentProductEvent = decision.products.find(
    (product) => product.binding?.binding_sha256 === binding.binding_sha256,
  )?.current_event;
  const resolutionRequired =
    action === "accepted" &&
    currentProductEvent?.binding.binding_sha256 === binding.binding_sha256 &&
    currentProductEvent.action === "changes_requested";
  const resolvedEvidenceReferences = Array.from(
    new Set(
      resolutionReferences
        .split(/\r?\n/)
        .map((reference) => reference.trim())
        .filter(Boolean),
    ),
  );
  const busy = ["queued", "submitting"].includes(operation.state.phase);
  const canSubmit =
    (!reasonRequired || reason.trim().length > 0) &&
    (!resolutionRequired ||
      (resolutionSummary.trim().length > 0 && resolvedEvidenceReferences.length > 0)) &&
    (action !== "revoked" || confirmRevoke) &&
    ownerAcceptanceActionAllowed(eligibility, action) &&
    !busy;

  return (
    <section className="engineering-owner-action-panel" aria-label={`Owner product review for ${binding.product}`}>
      <header>
        <div><strong>{binding.product}</strong><span>{binding.system} · {binding.action} · {binding.environment}</span></div>
        <code>{binding.binding_sha256.slice(0, 12)}</code>
      </header>
      <p><strong>Authoritative Owner decision.</strong> Record your judgment of this exact change. Acceptance satisfies the Owner prerequisite only when the binding remains current; technical checks, engineering review, merge admission, landing, and production authorization remain separate. Launchplane revalidates the exact change and your Owner authority at write time.</p>
      {!eligibility.can_accept ? <p className="engineering-owner-action-message" role="status">You contributed to this exact change, so product policy prevents you from accepting it. You may still request changes or revoke prior acceptance.</p> : null}
      <label>
        <span>Product review action</span>
        <select value={action} disabled={operation.state.requiresIdempotencyContinuity} onChange={(event) => {
          setAction(event.target.value as OwnerAcceptanceHumanAction);
          setConfirmRevoke(false);
        }}>
          {eligibility.can_accept ? <option value="accepted">Accept product change</option> : null}
          {eligibility.can_request_changes ? <option value="changes_requested">Request product changes</option> : null}
          {eligibility.can_revoke ? <option value="revoked">Revoke prior product acceptance</option> : null}
        </select>
      </label>
      {reasonRequired ? <label><span>Reason</span><textarea value={reason} maxLength={4000} disabled={operation.state.requiresIdempotencyContinuity} onChange={(event) => setReason(event.target.value)} /></label> : null}
      {resolutionRequired ? <div className="engineering-owner-action-resolution"><p><strong>Resolution evidence required.</strong> Summarize how the requested changes were resolved and reference the exact records, tests, or review evidence.</p><label><span>Resolution summary</span><textarea value={resolutionSummary} maxLength={4000} disabled={operation.state.requiresIdempotencyContinuity} onChange={(event) => setResolutionSummary(event.target.value)} /></label><label><span>Resolved evidence references</span><textarea value={resolutionReferences} maxLength={10000} placeholder={"One reference per line\ntest:owner-flow\nrecord:product-spec-17"} disabled={operation.state.requiresIdempotencyContinuity} onChange={(event) => setResolutionReferences(event.target.value)} /></label></div> : null}
      {action === "revoked" ? <label className="engineering-owner-action-confirm"><input type="checkbox" checked={confirmRevoke} disabled={operation.state.requiresIdempotencyContinuity} onChange={(event) => setConfirmRevoke(event.target.checked)} /><span>I confirm this exact binding should be revoked.</span></label> : null}
      <div className="engineering-owner-action-buttons">
        <button className="button button-primary" type="button" disabled={!canSubmit} onClick={async () => {
          const response = await operation.run(ownerAcceptanceRequest(
            binding,
            action,
            reason,
            resolutionRequired
              ? {
                  schema_version: 1,
                  summary: resolutionSummary.trim(),
                  resolved_evidence_references: resolvedEvidenceReferences,
                }
              : null,
          ));
          if (response) {
            onDecision(response.decision);
            setAction(ownerAcceptanceDefaultAction(eligibility));
            setReason("");
            setResolutionSummary("");
            setResolutionReferences("");
            setConfirmRevoke(false);
          }
        }}>{busy ? "Recording…" : "Record product review"}</button>
        {busy ? <button className="button" type="button" onClick={operation.cancel}>Cancel wait</button> : null}
      </div>
      {failure && failure.code !== "owner_acceptance_binding_changed" ? <p className="engineering-owner-action-message" role="alert">{failure.message}{failure.traceId ? <code>{failure.traceId}</code> : null}</p> : null}
      {operation.state.receipt ? <p className="engineering-owner-action-message" data-tone="success" role="status">{operation.state.receipt.replayed ? "Owner decision was already recorded (idempotent replay)." : "Authoritative Owner decision recorded."} Launchplane will recompute exact-head merge readiness; production authorization remains separate.<code>{operation.state.receipt.traceId}</code></p> : null}
      {operation.state.requiresIdempotencyContinuity ? <p className="engineering-owner-action-message" role="status">Outcome uncertain. Retry only this unchanged action; the idempotency key is preserved.</p> : null}
    </section>
  );
}

function ownerAcceptanceDefaultAction(
  eligibility: OwnerAcceptanceViewerBindingEligibility,
): OwnerAcceptanceHumanAction {
  if (eligibility.can_accept) return "accepted";
  if (eligibility.can_request_changes) return "changes_requested";
  return "revoked";
}

function ownerAcceptanceActionAllowed(
  eligibility: OwnerAcceptanceViewerBindingEligibility,
  action: OwnerAcceptanceHumanAction,
): boolean {
  if (action === "accepted") return eligibility.can_accept;
  if (action === "changes_requested") return eligibility.can_request_changes;
  return eligibility.can_revoke;
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
    <div
      className="engineering-owner-acceptance"
      aria-label="Recorded Owner acceptance history"
    >
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
          <span>Product accepted shown</span>
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
          <span>Authority</span>
          <strong>Launchplane Owner acceptance</strong>
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
            <option value="accepted">Product accepted</option>
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
              : "No recorded Owner product reviews yet. Owners do not appear here until they submit an action; use Current items or the exact lookup fallback to inspect a never-acted PR."
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
          Owner product review: {humanizeStatus(entry.ledger_status)}
        </span>
      </header>

      <div className="engineering-chip-row">
        <span data-mode="recorded">recorded ledger</span>
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
