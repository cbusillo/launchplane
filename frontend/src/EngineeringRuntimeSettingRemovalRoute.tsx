import { KeyRound } from "lucide-react";
import { useEffect, useState } from "react";

import { LaunchplaneApiError, removeRuntimeSettings } from "./api";
import { EngineeringRouteFrame } from "./EngineeringRouteUi";
import { InlineFormError, OperationNotice } from "./ProductConfigForms";
import { productConfigFailureCertainty, productConfigOperationFailure } from "./product-config-operation";
import { recoverRemovalRecord, recoverRemovalRequest, removalRecordScope, type RemovalRecord, type RemovalResponse } from "./runtime-setting-removal";
import { useBrowserOperationController } from "./use-browser-operation";
import type { RuntimeSettingRemovalRequest } from "./generated/openapi.ts";
import "./service-delivery.css";

export function EngineeringRuntimeSettingRemovalRoute({ admin, actorId }: { admin: boolean; actorId: number }) {
  return <EngineeringRouteFrame view="runtime-setting-removal" title="Remove stored settings" icon={KeyRound}
    description="Remove selected plain settings from one stored record. Review runtime consumers and inherited values before approving a removal.">
    {admin ? <RemovalRecordSelector key={actorId} actorId={actorId} /> : <p role="alert">Sign in as an administrator to review and remove stored settings.</p>}
  </EngineeringRouteFrame>;
}

function RemovalRecordSelector({ actorId }: { actorId: number }) {
  const storageKey = `launchplane:runtime-setting-removal:${actorId}:selection`;
  const [selected, setSelected] = useState<RemovalRecord | null>(() => recoverRemovalRecord(storageKey));
  const [target, setTarget] = useState<RemovalRecord>(() => selected ?? { product: "", scope: "context", context: "", instance: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const valid = target.product.trim() && (target.scope === "global" || target.context?.trim()) && (target.scope !== "instance" || target.instance?.trim());
  function selectRecord() {
    if (busy || !valid) return;
    const record = { ...target, product: target.product.trim(), context: target.context?.trim() || "", instance: target.instance?.trim() || "" };
    try { sessionStorage.setItem(storageKey, JSON.stringify(record)); }
    catch { setError("Recovery storage is unavailable. Nothing was sent; enable session storage before selecting a record."); return; }
    setError(""); setSelected(record);
  }
  return <>
    <section className="service-delivery-panel" aria-label="Select stored record">
      <h2>Select the stored record</h2>
      <p>A pending removal stays with its record. You can select another record while an outcome is uncertain, then return to the original request.</p>
      <fieldset disabled={busy}>
        <label>Product<input value={target.product} maxLength={256} onChange={event => setTarget({ ...target, product: event.target.value })} /></label>
        <label>Stored scope<select value={target.scope} onChange={event => setTarget({ ...target, scope: event.target.value as RemovalRecord["scope"],
          context: "", instance: "", ...(event.target.value === "global" ? { product: "launchplane" } : {}) })}>
          <option value="global">Global</option><option value="context">Context</option><option value="instance">Instance</option>
        </select></label>
        {target.scope !== "global" ? <label>Context<input value={target.context} maxLength={256} onChange={event => setTarget({ ...target, context: event.target.value })} /></label> : null}
        {target.scope === "instance" ? <label>Instance<input value={target.instance} maxLength={256} onChange={event => setTarget({ ...target, instance: event.target.value })} /></label> : null}
      </fieldset>
      {error ? <InlineFormError message={error} /> : null}
      <button type="button" className="button" disabled={busy || !valid} onClick={selectRecord}>Select record</button>
    </section>
    {selected ? <RemovalForm key={JSON.stringify(selected)} actorId={actorId} target={selected} onBusy={setBusy} /> : null}
  </>;
}

function RemovalForm({ actorId, target, onBusy }: { actorId: number; target: RemovalRecord; onBusy: (busy: boolean) => void }) {
  const scope = removalRecordScope(actorId, target);
  const storageKey = `launchplane:${scope}:request`;
  const [saved, setSaved] = useState(() => recoverRemovalRequest(storageKey));
  const [draft, setDraft] = useState<RuntimeSettingRemovalRequest>(() => ({
    ...target, mode: "dry-run", keys: saved?.keys ?? [], reason: saved?.reason ?? "",
  }));
  const [keysText, setKeysText] = useState(draft.keys.join("\n"));
  const [review, setReview] = useState<RemovalResponse | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState("");
  const [completion, setCompletion] = useState<RemovalResponse | null>(null);
  const [readback, setReadback] = useState<RemovalResponse | null>(null);
  const [checkingRecord, setCheckingRecord] = useState(false);
  const operationOptions = {
    execute: removeRuntimeSettings,
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
  const busy = checkingRecord || [planOperation, applyOperation, readOperation].some(operation => ["queued", "submitting"].includes(operation.state.phase));
  const uncertain = applyOperation.state.requiresIdempotencyContinuity;
  const committed = Boolean(completion);
  useEffect(() => { onBusy(busy); return () => onBusy(false); }, [busy, onBusy]);
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
    } catch { setError("Recovery storage is unavailable. Nothing was sent; enable session storage before reviewing."); return; }
    applyOperation.reset(); setSaved(null);
    setError(""); setReview(null); setConfirmed(false); setCompletion(null); setReadback(null);
    const request: RuntimeSettingRemovalRequest = { ...draft, mode: "dry-run", review_id: "", reviewed_plan_sha256: "",
      keys: keysText.split(/[\s,]+/).filter(Boolean) };
    const response = await planOperation.run(request);
    if (response) { setDraft(request); setReview(response); }
  }

  async function checkReadback(request: RuntimeSettingRemovalRequest) {
    if (checkingRecord) return;
    setCheckingRecord(true);
    setReadback(null);
    try {
      setReadback(await readOperation.run({ ...request, mode: "readback" }));
    } finally { setCheckingRecord(false); }
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
    setCompletion(result); setReview(null); setConfirmed(false);
    try { sessionStorage.removeItem(storageKey); }
    catch { setError("Removal committed. Its local recovery request could not be cleared; the service receipt remains authoritative."); }
    await checkReadback(request);
  }

  const plan = review?.result;
  return <section className="service-delivery-panel" aria-label="Stored setting removal">
    <h2>One exact stored record</h2>
    <p>This changes Launchplane records. It does not remove provider settings, restart a lane, or deploy a release.
      Removing an override can expose an inherited value. Check every affected lane and its recovery path first.</p>
    <p><strong>{target.product} · {target.scope}{target.context ? `:${target.context}` : ""}{target.instance ? `/${target.instance}` : ""}</strong></p>
    {uncertain && saved && saved.product !== target.product ? <p role="alert">The pending request belongs to {saved.product}. Its original product and key are preserved.</p> : null}
    <fieldset disabled={locked}>
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
      {plan.instance_plain_setting_effects ? <section aria-label="Context fallback comparison">
        <h4>Stored settings after removing the lane override</h4>
        <ul>{plan.instance_plain_setting_effects.map(effect => <li key={effect.key}>
          <code>{effect.key}</code>: {effect.source_after === "context"
            ? effect.effect === "unchanged" ? "The context supplies the same value." : "The context supplies a different value."
            : "No context value remains."}
        </li>)}</ul>
        <p>This compares stored plain settings only. Runtime use, preview templates, target overrides and secrets need separate qualification.</p>
      </section> : null}
      {plan.consumer_inventory ? <section aria-label="Global setting consumer inventory">
        <h4>Stored consumers across products</h4>
        <p>{plan.consumer_inventory.runtime_record_count} runtime records, {plan.consumer_inventory.product_profile_count} product profiles,
          {" "}{plan.consumer_inventory.target_record_count} target overlays and {plan.consumer_inventory.secret_binding_count} secret bindings were inspected.</p>
        <p>This inventory is a separate observation, outside the saved removal digest. It does not verify runtime consumption or qualify a removal.</p>
        <p>Showing {plan.consumer_inventory.consumers.length} of {plan.consumer_inventory.total_consumers} source entries,
          {" "}{plan.consumer_inventory.authority_records.length} of {plan.consumer_inventory.authority_record_count} authority records. Matching names appear first.</p>
        {plan.consumer_inventory.truncated || plan.consumer_inventory.authority_records_truncated
          ? <p role="alert">The displayed inventory is incomplete. Narrow review through the owning records before proceeding.</p> : null}
        <ul>{plan.consumer_inventory.consumers.map((consumer, index) => <li key={index}>
          {consumer.kind}: {consumer.product || "No product attribution"} · {consumer.context || "Global or unspecified context"}{consumer.instance ? `/${consumer.instance}` : ""}
          {consumer.scope ? ` (${consumer.scope})` : ""}: {consumer.selected_keys == null
            ? "Selected names not evaluated for this entry" : consumer.selected_keys.join(", ") || "No selected names in this source"}
        </li>)}</ul>
        <h4>Saved authority records</h4>
        <p>These records need review through their original operations. Their presence does not establish which selected names they captured.</p>
        <ul>{plan.consumer_inventory.authority_records.map((record, index) => <li key={index}>
          {record.kind}: {record.product} · {record.context || "Context unverified"}/{record.instance} · {record.state}
        </li>)}</ul>
        <p>Still unqualified: {plan.consumer_inventory.unqualified_populations.join(", ")}.</p>
      </section> : null}
      {plan.requires_compatible_readers_and_rollback ? <p role="alert">Before applying, deploy compatible readers to every consumer and replica and retain a compatible rollback build. This review does not prove that qualification.</p> : null}
      <p>Review <code>{plan.review_id}</code></p>
      <label className="service-delivery-check"><input type="checkbox" checked={confirmed} disabled={locked} onChange={event => setConfirmed(event.target.checked)} />
        I reviewed the exact keys, runtime consumers, inherited values and rollback requirements, and have authority for this removal.</label>
    </div> : null}
    {uncertain ? <p role="alert">Apply outcome is uncertain. The original request and operation key are retained. Retry only that same Apply; no new review or edit is allowed.
      {!saved ? " The retained request is unavailable. Keep the operation key and resolve the original request before continuing." : ""}</p> : null}
    {readback && !committed ? <p role="status">Current record {readback.result.readback_matches ? "matches" : "does not match"} the reviewed removal.
      This does not confirm the original Apply; retry its original request to read the receipt.</p> : null}
    {uncertain && applyOperation.state.identity ? <p>Retained operation key <code>{applyOperation.state.identity.idempotencyKey}</code></p> : null}
    {committed || checkingRecord ? <p role="status">
      {committed ? "Removal committed. " : ""}
      {checkingRecord ? "Checking current record…" :
        readback ? readback.result.readback_matches ? "Current stored record matches the reviewed removal." :
          "Current stored record does not match the reviewed removal. Review the changed record before another removal." :
          "Current readback is unverified. Check again or review the current record before another removal."}
    </p> : null}
    {readback ? <p>Check reference: <code aria-label="Current record check reference">{readback.trace_id}</code></p> : null}
    <div className="product-config-actions">
      <button type="button" className="button" disabled={locked || !canPlan} onClick={() => void preview()}>Review removal</button>
      <button type="button" className="button button-primary" disabled={busy || (uncertain ? !saved : !plan || !confirmed)} onClick={() => void apply()}>{uncertain ? "Retry original removal" : "Remove reviewed settings"}</button>
      {saved && (committed || uncertain) ? <button type="button" className="button" disabled={busy} onClick={() => void checkReadback(saved)}>Check current record</button> : null}
    </div>
  </section>;
}
