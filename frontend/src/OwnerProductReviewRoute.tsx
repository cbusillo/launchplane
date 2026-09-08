import { ExternalLink, LogOut, Moon, Sun } from "lucide-react";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  evaluateOwnerAcceptance,
  LaunchplaneApiError,
  writeOwnerAcceptanceEvent,
  type OwnerAcceptanceDecision,
  type OwnerAcceptanceEventMutationResponse,
  type OwnerAcceptanceProductDecision,
} from "./api";
import type { DevFixtureMode } from "./dev-fixture-loader";
import { loadDevFixtures } from "./dev-fixture-loader";
import { formatTime } from "./format";
import {
  ownerAcceptanceFailure,
  ownerAcceptanceFailureCertainty,
  ownerAcceptanceOperationScope,
  ownerAcceptanceRequest,
  type OwnerAcceptanceHumanAction,
} from "./owner-acceptance-operation";
import { ownerAcceptanceLookupFromSearch } from "./route-model";
import { useAppSearchParams } from "./router";
import { safeExternalUrl } from "./url";
import { useBrowserOperationController } from "./use-browser-operation";

import type {
  GitHubHumanIdentityResponse,
  OwnerAcceptanceBinding,
  OwnerAcceptanceEvaluationResponse,
  OwnerAcceptanceEventEnvelope,
  OwnerAcceptanceViewerBindingEligibility,
  OwnerAcceptanceViewerCapabilities,
} from "./generated/openapi.ts";

type Theme = "dark" | "light";

export function OwnerReviewShell({
  children,
  identity,
  notice,
  onDismissNotice,
  onLogout,
  onThemeChange,
  signingOut,
  theme,
}: {
  children: ReactNode;
  identity: GitHubHumanIdentityResponse;
  notice: string;
  onDismissNotice: () => void;
  onLogout: () => void;
  onThemeChange: (theme: Theme) => void;
  signingOut: boolean;
  theme: Theme;
}) {
  useEffect(() => {
    const previousTitle = document.title;
    document.title = "Product review · Launchplane";
    document.querySelector<HTMLElement>("[data-route-heading]")?.focus({
      preventScroll: true,
    });
    return () => {
      document.title = previousTitle;
    };
  }, []);

  return (
    <div className="owner-review-shell">
      <a className="skip-link" href="#main-content">
        Skip to review
      </a>
      <header className="owner-review-header">
        <div className="owner-review-brand" aria-label="Launchplane product review">
          <img
            alt=""
            src={`${import.meta.env.BASE_URL}assets/brand/launchplane-icon.svg`}
          />
          <span>
            <strong>Launchplane</strong>
            <small>Product review</small>
          </span>
        </div>
        <div className="owner-review-session">
          <span>{identity.name || identity.login}</span>
          <button
            aria-label={`Use ${theme === "dark" ? "light" : "dark"} theme`}
            className="icon-button"
            type="button"
            onClick={() => onThemeChange(theme === "dark" ? "light" : "dark")}
          >
            {theme === "dark" ? (
              <Sun size={16} aria-hidden="true" />
            ) : (
              <Moon size={16} aria-hidden="true" />
            )}
          </button>
          <button className="button" type="button" disabled={signingOut} onClick={onLogout}>
            <LogOut size={15} aria-hidden="true" />
            {signingOut ? "Signing out…" : "Sign out"}
          </button>
        </div>
      </header>
      {notice ? (
        <div className="owner-review-notice" role="status">
          <span>{notice}</span>
          <button type="button" onClick={onDismissNotice}>
            Dismiss
          </button>
        </div>
      ) : null}
      <main id="main-content" className="owner-review-main">
        {children}
      </main>
    </div>
  );
}

export function OwnerProductReviewRoute({
  fixtureMode,
}: {
  fixtureMode: DevFixtureMode;
}) {
  const searchParams = useAppSearchParams();
  const lookup = ownerAcceptanceLookupFromSearch(searchParams.toString());
  const [evaluation, setEvaluation] =
    useState<OwnerAcceptanceEvaluationResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [driftMessage, setDriftMessage] = useState("");
  const requestRef = useRef(0);

  const loadEvaluation = useCallback(
    async (
      afterBindingChange = false,
      afterWrite = false,
      signal?: AbortSignal,
    ) => {
      if (!lookup.valid) return;
      const requestId = requestRef.current + 1;
      requestRef.current = requestId;
      setLoading(true);
      setError("");
      try {
        const response = fixtureMode
          ? await loadDevFixtures().then((fixtures) =>
              fixtures.ownerReviewEvaluationForFixture(
                fixtureMode,
                afterBindingChange,
                afterWrite,
              ),
            )
          : await evaluateOwnerAcceptance(
              lookup.repository,
              Number(lookup.pullRequest),
              signal,
            );
        if (requestRef.current !== requestId || signal?.aborted) return;
        setEvaluation(response);
        setDriftMessage(
          afterBindingChange
            ? "The preview version changed. Review the updated preview before confirming your decision again."
            : "",
        );
      } catch (loadError) {
        if (requestRef.current !== requestId || signal?.aborted) return;
        const apiError = loadError as LaunchplaneApiError;
        setError(
          apiError.statusCode === 401 || apiError.statusCode === 403
            ? "This product review is unavailable for this session."
            : apiError.message || "The current product review is unavailable.",
        );
      } finally {
        if (requestRef.current === requestId && !signal?.aborted) setLoading(false);
      }
    },
    [fixtureMode, lookup.pullRequest, lookup.repository, lookup.valid],
  );

  useEffect(() => {
    setEvaluation(null);
    setDriftMessage("");
    setError("");
    if (!lookup.valid) return;
    const controller = new AbortController();
    void loadEvaluation(false, false, controller.signal);
    return () => controller.abort();
  }, [loadEvaluation, lookup.valid]);

  return (
    <section className="owner-review-page">
      <div className="owner-review-intro">
        <p className="eyebrow">Product decision</p>
        <h1 data-route-heading tabIndex={-1}>Review this change</h1>
        <p>
          Open each preview, then record your decision for every product listed.
          Delivery and operational actions remain separate.
        </p>
      </div>
      {!lookup.valid ? (
        <OwnerReviewState>
          This review link is incomplete or invalid. Return to the pull request and
          open its current Launchplane product-review link.
        </OwnerReviewState>
      ) : loading && !evaluation ? (
        <OwnerReviewState>Loading the current product review…</OwnerReviewState>
      ) : error ? (
        <OwnerReviewState tone="error">{error}</OwnerReviewState>
      ) : evaluation ? (
        <>
          <div className="owner-review-context">
            <span>{lookup.repository}</span>
            <span>PR #{lookup.pullRequest}</span>
            <span>Decision: {evaluation.decision.status.replaceAll("_", " ")}</span>
            <span>Evaluated {formatTime(evaluation.decision.evaluated_at)}</span>
          </div>
          {driftMessage ? (
            <p className="owner-review-alert" role="alert">{driftMessage}</p>
          ) : null}
          <OwnerBindingList
            decision={evaluation.decision}
            fixtureMode={fixtureMode}
            onRefreshAfterChange={() => loadEvaluation(true, false)}
            onRefreshAfterWrite={() => loadEvaluation(false, true)}
            viewerCapabilities={evaluation.viewer_capabilities}
          />
        </>
      ) : null}
    </section>
  );
}

function OwnerReviewState({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "error";
}) {
  return <p className="owner-review-state" data-tone={tone} role={tone === "error" ? "alert" : "status"}>{children}</p>;
}

function OwnerBindingList({
  decision,
  fixtureMode,
  onRefreshAfterChange,
  onRefreshAfterWrite,
  viewerCapabilities,
}: {
  decision: OwnerAcceptanceDecision;
  fixtureMode: DevFixtureMode;
  onRefreshAfterChange: () => Promise<void>;
  onRefreshAfterWrite: () => Promise<void>;
  viewerCapabilities: OwnerAcceptanceViewerCapabilities;
}) {
  if (!decision.products.length) {
    return decision.status === "not_required" ? (
      <OwnerReviewState>No product review is required for this change.</OwnerReviewState>
    ) : (
      <OwnerReviewState tone="error">
        The current product review is unavailable. No decision can be recorded.
      </OwnerReviewState>
    );
  }
  return (
    <div className="owner-review-bindings">
      {decision.products.map((product) => {
        const binding = product.binding;
        if (!binding) {
          return (
            <article className="owner-review-card" key={`${product.product}:${product.system}:${product.action}:${product.environment}`}>
              <OwnerBindingHeading product={product} />
              <OwnerReviewState tone="error">Current preview evidence is unavailable for this product.</OwnerReviewState>
            </article>
          );
        }
        const eligibility = viewerCapabilities.bindings.find(
          (candidate) => candidate.binding_sha256 === binding.binding_sha256,
        );
        return (
          <OwnerBindingCard
            binding={binding}
            decision={decision}
            eligibility={eligibility}
            eventWriteAuthorized={viewerCapabilities.event_write_authorized}
            fixtureMode={fixtureMode}
            key={binding.binding_sha256}
            onRefreshAfterChange={onRefreshAfterChange}
            onRefreshAfterWrite={onRefreshAfterWrite}
            product={product}
          />
        );
      })}
    </div>
  );
}

function OwnerBindingCard({
  binding,
  decision,
  eligibility,
  eventWriteAuthorized,
  fixtureMode,
  onRefreshAfterChange,
  onRefreshAfterWrite,
  product,
}: {
  binding: OwnerAcceptanceBinding;
  decision: OwnerAcceptanceDecision;
  eligibility: OwnerAcceptanceViewerBindingEligibility | undefined;
  eventWriteAuthorized: boolean;
  fixtureMode: DevFixtureMode;
  onRefreshAfterChange: () => Promise<void>;
  onRefreshAfterWrite: () => Promise<void>;
  product: OwnerAcceptanceProductDecision;
}) {
  const previewUrl = safeExternalUrl(binding.preview?.preview_url ?? "");
  const maySubmit = Boolean(
    eventWriteAuthorized && eligibility?.can_submit_event &&
      ((eligibility.can_accept && previewUrl) ||
        eligibility.can_request_changes ||
        eligibility.can_revoke),
  );
  return (
    <article className="owner-review-card" data-product={product.product}>
      <OwnerBindingHeading product={product} />
      <dl>
        <div><dt>Environment</dt><dd>{binding.environment}</dd></div>
        <div><dt>Current decision</dt><dd>{product.status.replaceAll("_", " ")}</dd></div>
      </dl>
      {previewUrl ? (
        <a className="button button-primary owner-review-preview" href={previewUrl.toString()} target="_blank" rel="noreferrer">
          Open preview <ExternalLink size={15} aria-hidden="true" />
        </a>
      ) : (
        <OwnerReviewState tone="error">A verified preview link is unavailable for this product.</OwnerReviewState>
      )}
      {maySubmit && eligibility ? (
        <OwnerReviewAction
          binding={binding}
          decision={decision}
          eligibility={eligibility}
          fixtureMode={fixtureMode}
          previewAvailable={Boolean(previewUrl)}
          onRefreshAfterChange={onRefreshAfterChange}
          onRefreshAfterWrite={onRefreshAfterWrite}
        />
      ) : (
        <OwnerReviewState>This product is read-only for the current session and binding.</OwnerReviewState>
      )}
    </article>
  );
}

function OwnerBindingHeading({ product }: { product: OwnerAcceptanceProductDecision }) {
  return <header><p className="eyebrow">Product</p><h2>{product.product}</h2></header>;
}

function OwnerReviewAction({
  binding,
  decision,
  eligibility,
  fixtureMode,
  onRefreshAfterChange,
  onRefreshAfterWrite,
  previewAvailable,
}: {
  binding: OwnerAcceptanceBinding;
  decision: OwnerAcceptanceDecision;
  eligibility: OwnerAcceptanceViewerBindingEligibility;
  fixtureMode: DevFixtureMode;
  onRefreshAfterChange: () => Promise<void>;
  onRefreshAfterWrite: () => Promise<void>;
  previewAvailable: boolean;
}) {
  const allowedActions = ownerAllowedActions(eligibility, previewAvailable);
  const [action, setAction] = useState<OwnerAcceptanceHumanAction>(allowedActions[0]);
  const [reason, setReason] = useState("");
  const [resolutionSummary, setResolutionSummary] = useState("");
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  const handledDriftRef = useRef<object | null>(null);
  const operation = useBrowserOperationController<OwnerAcceptanceEventEnvelope, OwnerAcceptanceEventMutationResponse>({
    scope: ownerAcceptanceOperationScope(binding),
    execute: async (payload, options) => {
      if (!fixtureMode) return writeOwnerAcceptanceEvent(payload, options);
      options.onDispatch?.();
      if (new URLSearchParams(window.location.search).get("scenario") === "drift") {
        throw new LaunchplaneApiError("The reviewed binding changed.", 409, "fixture-owner-drift", "owner_acceptance_binding_changed");
      }
      return ownerFixtureMutationResponse(decision, binding, payload, options.idempotencyKey);
    },
    failureFor: ownerAcceptanceFailure,
    failureCertainty: ownerAcceptanceFailureCertainty,
  });
  const restoredWithoutDraft = useRef(
    operation.state.requiresIdempotencyContinuity,
  ).current;
  const effectiveAction = (
    operation.state.requiresIdempotencyContinuity || allowedActions.includes(action)
      ? action
      : allowedActions[0]
  ) as OwnerAcceptanceHumanAction;
  useEffect(() => {
    if (operation.state.failure?.code === "owner_acceptance_binding_changed" && handledDriftRef.current !== operation.state.failure) {
      handledDriftRef.current = operation.state.failure;
      void onRefreshAfterChange();
    }
  }, [onRefreshAfterChange, operation.state.failure]);
  useEffect(() => {
    setAction(allowedActions[0]);
    setReason("");
    setResolutionSummary("");
    setConfirmRevoke(false);
  }, [binding.binding_sha256]);
  const allowedActionKey = allowedActions.join(":");
  useEffect(() => {
    if (
      operation.state.requiresIdempotencyContinuity ||
      action === effectiveAction
    ) {
      return;
    }
    if (operation.state.phase === "succeeded") operation.reset();
    setAction(effectiveAction);
    setReason("");
    setResolutionSummary("");
    setConfirmRevoke(false);
  }, [action, allowedActionKey, effectiveAction, operation.state.requiresIdempotencyContinuity]);
  const busy = operation.state.phase === "queued" || operation.state.phase === "submitting";
  const reasonRequired = effectiveAction !== "accepted";
  const currentProductEvent = decision.products.find(
    (productDecision) =>
      productDecision.binding?.binding_sha256 === binding.binding_sha256,
  )?.current_event;
  const resolutionRequired =
    effectiveAction === "accepted" &&
    currentProductEvent?.binding.binding_sha256 === binding.binding_sha256 &&
    currentProductEvent.action === "changes_requested";
  const resolvedEvidenceReferences = binding.preview
    ? [
        `preview:${binding.preview.preview_id}`,
        `preview-generation:${binding.preview.serving_generation_id}`,
      ]
    : [];
  const fieldsLocked = busy || operation.state.requiresIdempotencyContinuity;
  const canSubmit =
    Boolean(effectiveAction) &&
    allowedActions.includes(effectiveAction) &&
    !restoredWithoutDraft &&
    (!reasonRequired || reason.trim()) &&
    (!resolutionRequired ||
      (resolutionSummary.trim() && resolvedEvidenceReferences.length > 0)) &&
    (effectiveAction !== "revoked" || confirmRevoke) &&
    operation.state.phase !== "succeeded" &&
    !busy;
  const disabledHint = restoredWithoutDraft
    ? ""
    : reasonRequired && !reason.trim()
      ? "Feedback is required for this decision."
      : resolutionRequired && !resolutionSummary.trim()
        ? "A resolution explanation is required."
        : effectiveAction === "revoked" && !confirmRevoke
          ? "Confirm the revocation before recording it."
          : "";
  const clearCompletedReceipt = () => {
    if (operation.state.phase === "succeeded") operation.reset();
  };
  return (
    <section className="owner-review-action" aria-label={`Decision for ${binding.product}`}>
      <label><span>Decision</span><select value={effectiveAction} disabled={fieldsLocked} onChange={(event) => { clearCompletedReceipt(); setAction(event.target.value as OwnerAcceptanceHumanAction); setReason(""); setResolutionSummary(""); setConfirmRevoke(false); }}>
        {eligibility.can_accept && previewAvailable ? <option value="accepted">Accept product change</option> : null}
        {eligibility.can_request_changes ? <option value="changes_requested">Request product changes</option> : null}
        {eligibility.can_revoke ? <option value="revoked">Revoke prior acceptance</option> : null}
      </select></label>
      {reasonRequired ? <label><span>Feedback</span><textarea maxLength={4000} value={reason} disabled={fieldsLocked} onChange={(event) => { clearCompletedReceipt(); setReason(event.target.value); }} /></label> : null}
      {resolutionRequired ? <div className="owner-review-resolution"><p>Explain how the requested changes were resolved. Launchplane will attach this preview and its current version to the decision.</p><label><span>Resolution explanation</span><textarea maxLength={4000} value={resolutionSummary} disabled={fieldsLocked} onChange={(event) => { clearCompletedReceipt(); setResolutionSummary(event.target.value); }} /></label></div> : null}
      {effectiveAction === "revoked" ? <label className="owner-review-confirm"><input type="checkbox" checked={confirmRevoke} disabled={fieldsLocked} onChange={(event) => { clearCompletedReceipt(); setConfirmRevoke(event.target.checked); }} /><span>Confirm revocation for this product review.</span></label> : null}
      <div className="owner-review-action-buttons">
        <button className="button button-primary" type="button" disabled={!canSubmit} onClick={async () => {
          if (restoredWithoutDraft || operation.state.phase === "succeeded" || !allowedActions.includes(effectiveAction)) return;
          const response = await operation.run(
            ownerAcceptanceRequest(
              binding,
              effectiveAction,
              reason,
              resolutionRequired
                ? {
                    schema_version: 1,
                    summary: resolutionSummary.trim(),
                    resolved_evidence_references: resolvedEvidenceReferences,
                  }
                : null,
            ),
          );
          if (response) {
            await onRefreshAfterWrite();
          }
        }}>{busy ? "Recording…" : "Record decision"}</button>
        {busy ? <button className="button" type="button" onClick={operation.cancel}>Cancel wait</button> : null}
      </div>
      {disabledHint ? <p className="owner-review-state" role="status">{disabledHint}</p> : null}
      {operation.state.failure && operation.state.failure.code !== "owner_acceptance_binding_changed" ? <p className="owner-review-alert" role="alert">{operation.state.failure.message}</p> : null}
      {operation.state.receipt ? <p className="owner-review-success" role="status">Decision recorded.</p> : null}
      {operation.state.requiresIdempotencyContinuity ? <p className="owner-review-alert" role="status">{restoredWithoutDraft ? "A prior decision outcome is unknown, and this page cannot restore its feedback. Reconcile that outcome before recording another decision." : "Outcome unknown. Retry only this unchanged decision; its fields and idempotency key are preserved."}</p> : null}
    </section>
  );
}

function ownerAllowedActions(
  eligibility: OwnerAcceptanceViewerBindingEligibility,
  previewAvailable: boolean,
): OwnerAcceptanceHumanAction[] {
  const actions: OwnerAcceptanceHumanAction[] = [];
  if (eligibility.can_accept && previewAvailable) actions.push("accepted");
  if (eligibility.can_request_changes) actions.push("changes_requested");
  if (eligibility.can_revoke) actions.push("revoked");
  return actions;
}

function ownerFixtureMutationResponse(
  decision: OwnerAcceptanceDecision,
  binding: OwnerAcceptanceBinding,
  payload: OwnerAcceptanceEventEnvelope,
  idempotencyKey: string,
): OwnerAcceptanceEventMutationResponse {
  const occurredAt = new Date().toISOString();
  const record = {
    schema_version: 1,
    event_id: `fixture-${idempotencyKey}`,
    acceptance_id: `fixture-${binding.binding_sha256.slice(0, 32)}`,
    subject_sequence: 1,
    binding,
    action: payload.action,
    occurred_at: occurredAt,
    source_event_kind: "browser_api" as const,
    source_event_id: idempotencyKey,
    reason: payload.reason ?? "",
    resolution: payload.resolution ?? null,
    authorization: null,
  };
  return {
    status: "ok",
    trace_id: "fixture-owner-review-write",
    write_status: "written",
    record,
    semantics: {
      human_action_semantics: payload.action === "accepted" ? "product_review_accepted" : payload.action === "revoked" ? "product_review_revoked" : "product_review_changes_requested",
    },
    decision,
    replayed: false,
  };
}
