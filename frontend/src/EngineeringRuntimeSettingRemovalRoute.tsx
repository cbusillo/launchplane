import { KeyRound } from "lucide-react";
import { useEffect, useState } from "react";
import type { BrowserOperationOptions } from "./browser-operation";

import { LaunchplaneApiError, removeRuntimeSettings } from "./api";
import { EngineeringRouteFrame } from "./EngineeringRouteUi";
import { InlineFormError, OperationNotice } from "./ProductConfigForms";
import { productConfigFailureCertainty, productConfigOperationFailure } from "./product-config-operation";
import { recoverRemovalRequest, type RemovalResponse } from "./runtime-setting-removal";
import { useBrowserOperationController } from "./use-browser-operation";
import type { RuntimeSettingRemovalRequest } from "./generated/openapi.ts";
import "./service-delivery.css";

export function EngineeringRuntimeSettingRemovalRoute({ admin, actorId }: { admin: boolean; actorId: number }) {
  return <EngineeringRouteFrame view="runtime-setting-removal" title="Remove stored settings" icon={KeyRound}
    description="Remove selected plain settings from one stored record. Review runtime consumers and inherited values before approving a removal.">
    {admin ? <RemovalForm key={actorId} actorId={actorId} /> : <p role="alert">Sign in as an administrator to review and remove stored settings.</p>}
  </EngineeringRouteFrame>;
}

function RemovalForm({ actorId }: { actorId: number }) {
  const scope = `runtime-setting-removal:${actorId}`;
  const storageKey = `launchplane:${scope}:request`;
  const [recordedCompletion, setRecordedCompletion] = useState(() => {
    try { return sessionStorage.getItem(`${storageKey}:committed`) === "true"; } catch { return false; }
  });
  const [saved, setSaved] = useState(() => recoverRemovalRequest(storageKey));
  const [draft, setDraft] = useState<RuntimeSettingRemovalRequest>(() => saved ? { ...saved, mode: "dry-run" } : {
    mode: "dry-run", product: "", scope: "context", context: "", instance: "", keys: [], reason: "",
  });
  const [keysText, setKeysText] = useState(draft.keys.join("\n"));
  const [review, setReview] = useState<RemovalResponse | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState("");
  const [completion, setCompletion] = useState<RemovalResponse | null>(null);
  const [readback, setReadback] = useState<RemovalResponse | null>(null);
  const operationOptions = {
    execute: async (request: RuntimeSettingRemovalRequest, options: BrowserOperationOptions) => {
      const response = await removeRuntimeSettings(request, options);
      if (request.mode === "apply") sessionStorage.setItem(`${storageKey}:committed`, "true");
      return response;
    },
    failureFor: productConfigOperationFailure,
    failureCertainty: (failure: unknown, dispatched: boolean): "definitive" | "uncertain" | "settled" =>
      failure instanceof LaunchplaneApiError && failure.code === "runtime_setting_removal_stale"
        ? "settled" : productConfigFailureCertainty(failure, dispatched),
  };
  const planOperation = useBrowserOperationController({ ...operationOptions, scope: `${scope}:plan`, readOnly: true });
  const applyOperation = useBrowserOperationController({ ...operationOptions, scope: `${scope}:apply` });
  useEffect(() => {
    if (applyOperation.state.phase === "failed") { setReview(null); setConfirmed(false); }
  }, [applyOperation.state.phase]);
  const readOperation = useBrowserOperationController({ ...operationOptions, scope: `${scope}:readback`, readOnly: true });
  const busy = [planOperation, applyOperation, readOperation].some(operation => ["queued", "submitting"].includes(operation.state.phase));
  const uncertain = applyOperation.state.requiresIdempotencyContinuity;
  const committed = Boolean(completion || (saved && recordedCompletion));
  const locked = busy || uncertain;
  const canPlan = Boolean(draft.product.trim() && draft.reason.trim() && keysText.trim() &&
    (draft.scope === "global" || draft.context?.trim()) && (draft.scope !== "instance" || draft.instance?.trim()));

  function edit(change: Partial<RuntimeSettingRemovalRequest>) {
    setDraft(current => ({ ...current, ...change }));
    setReview(null); setConfirmed(false); setError("");
  }

  async function preview() {
    if (locked || !canPlan) return;
    try {
      sessionStorage.removeItem(storageKey);
      sessionStorage.removeItem(`${storageKey}:committed`);
    } catch { setError("Recovery storage is unavailable. Nothing was sent; enable session storage before reviewing."); return; }
    applyOperation.reset(); setSaved(null); setRecordedCompletion(false);
    setError(""); setReview(null); setConfirmed(false); setCompletion(null); setReadback(null);
    const request: RuntimeSettingRemovalRequest = { ...draft, mode: "dry-run", review_id: "", reviewed_plan_sha256: "",
      keys: keysText.split(/[\s,]+/).filter(Boolean) };
    const response = await planOperation.run(request);
    if (response) { setDraft(request); setReview(response); }
  }

  async function checkReadback(request: RuntimeSettingRemovalRequest) {
    const result = await readOperation.run({ ...request, mode: "readback" });
    setReadback(result);
  }

  async function apply() {
    if (busy) return;
    const request: RuntimeSettingRemovalRequest | null = uncertain ? saved : review && confirmed ? {
      ...draft, mode: "apply", review_id: review.result.review_id, reviewed_plan_sha256: review.result.plan_sha256,
    } : null;
    if (!request) { setError("Review and approve this exact selection before removal."); return; }
    try {
      sessionStorage.setItem(storageKey, JSON.stringify(request));
      if (sessionStorage.getItem(storageKey) !== JSON.stringify(request)) throw new Error("Request was not retained.");
    } catch { setError("Recovery storage is unavailable. Nothing was sent; enable session storage before applying."); return; }
    setSaved(request); setError(""); setReadback(null);
    const result = await applyOperation.run(request);
    if (!result) return;
    setRecordedCompletion(true); setCompletion(result); setReview(null); setConfirmed(false);
    await checkReadback(request);
  }

  const plan = review?.result;
  return <section className="service-delivery-panel" aria-label="Stored setting removal">
    <h2>One exact stored record</h2>
    <p>This changes Launchplane records. It does not remove provider settings, restart a lane, or deploy a release.
      Removing an override can expose an inherited value. Check every affected lane and its recovery path first.</p>
    <fieldset disabled={locked}>
      <label>Product<input value={draft.product} maxLength={256} onChange={event => edit({ product: event.target.value })} /></label>
      <label>Stored scope<select value={draft.scope} onChange={event => edit({ scope: event.target.value as RuntimeSettingRemovalRequest["scope"],
        context: "", instance: "", ...(event.target.value === "global" ? { product: "launchplane" } : {}) })}>
        <option value="global">Global</option><option value="context">Context</option><option value="instance">Instance</option>
      </select></label>
      {draft.scope !== "global" ? <label>Context<input value={draft.context} maxLength={256} onChange={event => edit({ context: event.target.value })} /></label> : null}
      {draft.scope === "instance" ? <label>Instance<input value={draft.instance} maxLength={256} onChange={event => edit({ instance: event.target.value })} /></label> : null}
      <label>Setting names<textarea value={keysText} onChange={event => { setKeysText(event.target.value); edit({}); }} placeholder="One name per line" /></label>
      <label>Removal reason<textarea value={draft.reason} maxLength={1000} onChange={event => edit({ reason: event.target.value })} /></label>
    </fieldset>
    <OperationNotice label="Review" state={planOperation.state} />
    <OperationNotice label="Apply" state={applyOperation.state} />
    <OperationNotice label="Readback" state={readOperation.state} />
    {error ? <InlineFormError message={error} /> : null}
    {plan ? <div className="service-delivery-review">
      <h3>Review {plan.product} · {plan.scope}{plan.context ? `:${plan.context}` : ""}{plan.instance ? `/${plan.instance}` : ""}</h3>
      <p>Remove: {plan.removed_keys.join(", ")}</p>
      <p>Keep: {plan.surviving_keys.join(", ") || "No printable setting names"}</p>
      <p>{plan.surviving_value_count} stored values and {plan.retirement_intent_count} provider retirement intents remain.
        {plan.record_deleted ? " The empty record will be deleted." : " The record will remain."}</p>
      {plan.unprintable_survivor_count ? <p role="alert">{plan.unprintable_survivor_count} surviving names cannot be displayed. Name coverage is partial.</p> : null}
      {plan.requires_compatible_readers_and_rollback ? <p role="alert">Before applying, deploy compatible readers to every consumer and replica and retain a compatible rollback build. This review does not prove that qualification.</p> : null}
      <p>Review <code>{plan.review_id}</code></p>
      <label className="service-delivery-check"><input type="checkbox" checked={confirmed} disabled={locked} onChange={event => setConfirmed(event.target.checked)} />
        I reviewed the exact keys, runtime consumers, inherited values and rollback requirements, and have authority for this removal.</label>
    </div> : null}
    {uncertain ? <p role="alert">Apply outcome is uncertain. The original request and operation key are retained. Retry only that same Apply; no new review or edit is allowed.
      {!saved ? " The retained request is unavailable. Keep the operation key and resolve the original request before continuing." : ""}</p> : null}
    {committed ? <p role="status">Removal committed. {readback?.result.readback_matches ? "Current stored record matches the reviewed removal." : "Current readback is unverified. Check again or review the current record before another removal."}</p> : null}
    <div className="product-config-actions">
      <button type="button" className="button" disabled={locked || !canPlan} onClick={() => void preview()}>Review removal</button>
      <button type="button" className="button button-primary" disabled={busy || (uncertain ? !saved : !plan || !confirmed)} onClick={() => void apply()}>{uncertain ? "Retry original removal" : "Remove reviewed settings"}</button>
      {saved && (committed || uncertain) ? <button type="button" className="button" disabled={busy} onClick={() => void checkReadback(saved)}>Check current record</button> : null}
    </div>
  </section>;
}
