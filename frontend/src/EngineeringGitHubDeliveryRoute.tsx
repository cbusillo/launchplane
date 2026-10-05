import { KeyRound } from "lucide-react";
import { useCallback, useState, type ReactNode } from "react";

import { configureServiceGitHubDelivery, LaunchplaneApiError, readServiceGitHubDelivery, retireServiceGitHubTokens } from "./api";
import type { BrowserOperationOptions } from "./browser-operation";
import { EngineeringResourceControls, EngineeringResourceGate, EngineeringRouteFrame } from "./EngineeringRouteUi";
import { useEngineeringResource } from "./engineering-resource";
import type { DeliveryGitHubAppConfigurationRequest, DeliveryGitHubAppConfigurationResponse, ServiceGitHubDeliveryStatus, ServiceTokenRetirementRequest, ServiceTokenRetirementResponse } from "./generated/openapi.ts";
import { InlineFormError, OperationNotice } from "./ProductConfigForms";
import { productConfigOperationFailure } from "./product-config-operation";
import { useBrowserOperationController } from "./use-browser-operation";
import "./service-delivery.css";

export function EngineeringGitHubDeliveryRoute({ actorId }: { actorId: number }) {
  const loader = useCallback((signal: AbortSignal) => readServiceGitHubDelivery(signal), []);
  const resource = useEngineeringResource(loader, `github-delivery:${actorId}`);
  return <EngineeringRouteFrame view="github-delivery" title="GitHub delivery" icon={KeyRound}
    description="Launchplane service integration. The Director approves App selection and obsolete service-token retirement."
    actions={<EngineeringResourceControls state={resource.state} refresh={resource.refresh} cancel={resource.cancel} refreshLabel="Refresh metadata" />}>
    <EngineeringResourceGate state={resource.state} noun="service GitHub delivery metadata" refresh={resource.refresh}>{status => <>
      <section className="service-delivery-summary" aria-label="Current service selection">
        <h2>Current service selection</h2>
        <dl><div><dt>Delivery App id</dt><dd>{status.app_id || "Not selected"}</dd></div>
          <div><dt>Managed key integration</dt><dd>{status.integration || "Not selected"}</dd></div>
          <div><dt>Advisory App id</dt><dd>{status.advisory_app_id || "Not configured"}</dd></div></dl>
        <p>Metadata only. An App id and key selection do not prove installation permissions or delivery.</p>
      </section>
      <DeliverySelection status={status} actorId={actorId} onApplied={resource.refresh} stale={resource.state.stale || resource.state.refreshing} />
      <TokenRetirement status={status} actorId={actorId} onApplied={resource.refresh} stale={resource.state.stale || resource.state.refreshing} />
    </>}</EngineeringResourceGate>
  </EngineeringRouteFrame>;
}

type ReviewRequest = { mode?: "dry-run" | "apply"; expected_plan_digest?: string; reason: string };
type ReviewResponse = { trace_id: string; plan_digest: string };

function recoverRequest<T extends ReviewRequest>(storageKey: string): T | null {
  try {
    const candidate = JSON.parse(sessionStorage.getItem(storageKey) || "null") as T | null;
    return candidate?.mode === "apply" && /^[a-f0-9]{64}$/.test(candidate.expected_plan_digest || "") ? candidate : null;
  } catch { return null; }
}

function useReviewedChange<T extends ReviewRequest, R extends ReviewResponse>({ scope, draft, execute, readback, onApplied }: {
  scope: string; draft: T; execute: (payload: T, options: BrowserOperationOptions) => Promise<R>;
  readback: (status: ServiceGitHubDeliveryStatus, payload: T) => boolean; onApplied: () => void;
}) {
  const storageKey = `launchplane:service-delivery-draft:${scope}`;
  const [saved, setSaved] = useState<T | null>(() => recoverRequest<T>(storageKey));
  const [review, setReview] = useState<{ key: string; result: R } | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const operationOptions = { execute, failureFor: productConfigOperationFailure,
    failureCertainty: (error: unknown, dispatched: boolean): "definitive" | "uncertain" =>
      !dispatched || (error instanceof LaunchplaneApiError && error.statusCode < 500) ? "definitive" : "uncertain" };
  const planOperation = useBrowserOperationController<T, R>({ ...operationOptions, scope: `${scope}:plan`, readOnly: true });
  const applyOperation = useBrowserOperationController<T, R>({ ...operationOptions, scope: `${scope}:apply` });
  const busy = [planOperation.state.phase, applyOperation.state.phase].some(phase => ["queued", "submitting"].includes(phase));
  const uncertain = applyOperation.state.requiresIdempotencyContinuity;
  const locked = busy || uncertain;
  const draftKey = JSON.stringify(draft);
  const matches = Boolean(review && review.key === draftKey);
  async function preview() {
    setError(""); setNotice(""); setReview(null); setConfirmed(false);
    const response = await planOperation.run(draft);
    if (response) setReview({ key: draftKey, result: response });
  }
  async function apply() {
    setError(""); setNotice("");
    const payload = uncertain ? saved : review && matches && confirmed ? {
      ...draft, mode: "apply", expected_plan_digest: review.result.plan_digest,
      ...("secret_ids" in draft ? { director_confirmed: true } : {}),
    } as T : null;
    if (!payload) { setError("Review and confirm this exact change before applying it."); return; }
    try {
      sessionStorage.setItem(storageKey, JSON.stringify(payload));
      if (sessionStorage.getItem(storageKey) !== JSON.stringify(payload)) throw new Error("Draft was not saved.");
    } catch { setError("The reviewed draft could not be saved for recovery. Nothing was sent; allow session storage and retry."); return; }
    setSaved(payload);
    const response = await applyOperation.run(payload);
    if (!response) return;
    setReview(null); setConfirmed(false);
    try { sessionStorage.removeItem(storageKey); } catch { /* Settled operation is not recovered. */ }
    setSaved(null);
    onApplied();
    try {
      const status = await readServiceGitHubDelivery();
      if (!readback(status, payload)) { setError("Apply completed, but current metadata does not match. Refresh metadata and inspect before another change."); return; }
      setNotice(`Applied and read back. Trace ${response.trace_id}`);
    } catch (error) { setError(`Apply completed; read-back is unavailable. Refresh metadata before another change. ${error instanceof Error ? error.message : ""}`); }
  }
  return { preview, apply, saved, review: matches ? review?.result : null, confirmed: matches && confirmed, setConfirmed, locked, busy, uncertain,
    canApply: !busy && (uncertain ? Boolean(saved) : matches && confirmed), error, notice, planOperation, applyOperation,
    edit: () => { setConfirmed(false); setReview(null); setError(""); setNotice(""); } };
}

function ReviewActions<T extends ReviewRequest, R extends ReviewResponse>({ change, canPlan, stale, children, confirmation }: {
  change: ReturnType<typeof useReviewedChange<T, R>>; canPlan: boolean; stale: boolean;
  children: ReactNode; confirmation: string;
}) {
  return <>
    <OperationNotice label="Dry run" state={change.planOperation.state} />
    <OperationNotice label="Apply" state={change.applyOperation.state} />
    {change.error ? <InlineFormError message={change.error} /> : null}
    {change.notice ? <p role="status">{change.notice}</p> : null}
    {change.review ? <div className="service-delivery-review">
      <h3>Review the change</h3>{children}<p>Review digest <code>{change.review.plan_digest}</code></p>
      <label className="service-delivery-check"><input type="checkbox" checked={change.confirmed} disabled={change.locked}
        onChange={event => change.setConfirmed(event.target.checked)} />{confirmation}</label>
    </div> : null}
    <div className="product-config-actions">
      <button type="button" className="button" disabled={change.locked || stale || !canPlan} onClick={() => void change.preview()}>Dry run</button>
      <button type="button" className="button button-primary" disabled={!change.canApply || (!change.uncertain && stale)} onClick={() => void change.apply()}>{change.uncertain ? "Retry Apply" : "Apply"}</button>
    </div>
    {change.uncertain ? <p>The result is uncertain. The reviewed request and operation key are retained; retry the same Apply.</p> : null}
  </>;
}

function DeliverySelection({ status, actorId, onApplied, stale }: { status: ServiceGitHubDeliveryStatus; actorId: number; onApplied: () => void; stale: boolean }) {
  const [appId, setAppId] = useState(status.app_id);
  const [integration, setIntegration] = useState(status.integration);
  const [reason, setReason] = useState("");
  const draft: DeliveryGitHubAppConfigurationRequest = { app_id: Number(appId), integration, reason, mode: "dry-run" };
  const change = useReviewedChange<DeliveryGitHubAppConfigurationRequest, DeliveryGitHubAppConfigurationResponse>({ scope: `github:${actorId}:delivery-selection`, draft, execute: configureServiceGitHubDelivery, onApplied,
    readback: (current, payload) => current.app_id === String(payload.app_id) && current.integration === payload.integration });
  return <section className="service-delivery-panel" aria-label="Select Delivery App">
    <h2>Select Delivery App</h2><p>Choose the existing managed key for the Delivery App. The Director verifies the App identity before approving.</p>
    {!status.existing_keys.length ? <p role="status">No eligible configured service-context private_key binding. Selection is blocked until an existing managed binding is available.</p> : null}
    {change.uncertain && change.saved ? <p>Retained selection: App {change.saved.app_id}, existing key integration {change.saved.integration}.</p> : null}
    <fieldset disabled={change.locked || stale} onChange={change.edit}>
      <label>Delivery App id<input inputMode="numeric" value={appId} onChange={event => setAppId(event.target.value)} /></label>
      <label>Existing managed key<select value={integration} onChange={event => setIntegration(event.target.value)}>
        <option value="">Select an existing binding</option>{status.existing_keys.map(key => <option value={key.integration} key={key.binding_id}>{key.integration} · private_key · {key.context}</option>)}
      </select></label>
      <label>Selection reason<textarea value={reason} maxLength={2000} onChange={event => setReason(event.target.value)} /></label>
    </fieldset>
    <ReviewActions change={change} canPlan={/^[1-9][0-9]*$/.test(appId) && Number.isSafeInteger(Number(appId)) && Boolean(reason.trim()) && status.existing_keys.some(key => key.integration === integration)} stale={stale}
      confirmation="I am the Director approving this App id and existing key selection.">
      <p>Delivery App id: <strong>{change.review?.app_id}</strong></p><p>Existing key integration: <strong>{change.review?.integration}</strong></p>
    </ReviewActions>
  </section>;
}

function TokenRetirement({ status, actorId, onApplied, stale }: { status: ServiceGitHubDeliveryStatus; actorId: number; onApplied: () => void; stale: boolean }) {
  const [secretIds, setSecretIds] = useState<string[]>([]);
  const [reason, setReason] = useState("");
  const [advisoryCheck, setAdvisoryCheck] = useState("");
  const [deliveryComment, setDeliveryComment] = useState("");
  const [releaseIssue, setReleaseIssue] = useState("");
  const [consumerEvidence, setConsumerEvidence] = useState("");
  const draft: ServiceTokenRetirementRequest = { mode: "dry-run", secret_ids: [...secretIds].sort(), reason,
    advisory_check_url: advisoryCheck, delivery_comment_url: deliveryComment, delivery_release_issue_url: releaseIssue, consumer_check_evidence: consumerEvidence };
  const change = useReviewedChange<ServiceTokenRetirementRequest, ServiceTokenRetirementResponse>({ scope: `github:${actorId}:service-token-retirement`, draft, execute: retireServiceGitHubTokens, onApplied,
    readback: (current, payload) => payload.secret_ids.every(id => current.obsolete_tokens.some(token => token.secret_id === id && token.status === "disabled")) });
  const ready = /^[1-9][0-9]*$/.test(status.app_id) && /^[1-9][0-9]*$/.test(status.advisory_app_id) && status.existing_keys.some(key => key.integration === status.integration);
  return <section className="service-delivery-panel" aria-label="Retire obsolete service tokens">
    <h2>Retire obsolete service tokens</h2><p>Select the exact launchplane_service GITHUB_TOKEN records to disable. Global records are shared by service contexts; disabling one affects every context that inherits it.</p>
    <p>Encrypted versions and audit history stay in Launchplane. GitHub PAT revocation is a separate Director step.</p>
    {change.uncertain && change.saved ? <p>Retained retirement: {change.saved.secret_ids.join(", ")}.</p> : null}
    {!ready ? <p role="status">Blocked: configure the Delivery App and Advisory App before retirement.</p> : null}
    {!status.obsolete_tokens.length ? <p role="status">No eligible service-token records found.</p> : null}
    <fieldset disabled={change.locked || stale || !ready} onChange={change.edit}>
      <legend>Service-token records</legend>
      {status.obsolete_tokens.map(token => <label key={token.secret_id} className="service-delivery-check">
        <input type="checkbox" disabled={token.status === "disabled"} checked={secretIds.includes(token.secret_id)} onChange={event => setSecretIds(ids => event.target.checked ? [...ids, token.secret_id] : ids.filter(id => id !== token.secret_id))} />
        <span><strong>{token.secret_id}</strong><small>{token.scope === "global" ? "Global — shared across service contexts" : `Context: ${token.context}`} · {token.status}</small></span>
      </label>)}
      <p>After checking the next App receipts, paste their links. These are your attestations; Launchplane does not automatically verify these GitHub links.</p>
      <label>Advisory check receipt URL<input type="url" value={advisoryCheck} maxLength={1000} onChange={event => setAdvisoryCheck(event.target.value)} /></label>
      <label>Delivery comment receipt URL<input type="url" value={deliveryComment} maxLength={1000} onChange={event => setDeliveryComment(event.target.value)} /></label>
      <label>Delivery release issue receipt URL<input type="url" value={releaseIssue} maxLength={1000} onChange={event => setReleaseIssue(event.target.value)} /></label>
      <label>Remaining-consumer check evidence<textarea value={consumerEvidence} maxLength={2000} onChange={event => setConsumerEvidence(event.target.value)} /></label>
      <label>Retirement reason<textarea value={reason} maxLength={2000} onChange={event => setReason(event.target.value)} /></label>
    </fieldset>
    <ReviewActions change={change} canPlan={ready && secretIds.length > 0 && [reason, advisoryCheck, deliveryComment, releaseIssue, consumerEvidence].every(value => Boolean(value.trim()))} stale={stale}
      confirmation={`I am the Director. I verified Advisory App ${change.review?.advisory_app_id || status.advisory_app_id} on the current PR commit, Delivery App ${change.review?.app_id || status.app_id} on the comment and release issue, checked remaining consumers, and approve disabling these records and their bindings.`}>
      <p>Reviewed Delivery App: <strong>{change.review?.app_id}</strong> · Advisory App: <strong>{change.review?.advisory_app_id}</strong></p>
      <p>Disable only these records and their GITHUB_TOKEN bindings:</p>
      <ul>{change.review?.tokens.map(token => <li key={token.secret_id}><strong>{token.secret_id}</strong> · {token.scope === "global" ? "global, shared across service contexts" : token.context}</li>)}</ul>
    </ReviewActions>
  </section>;
}
