import { useEffect, useRef, useState } from "react";
import { Pencil } from "lucide-react";
import { applyProductDisplayName, readProductProfile, readProductProfileMutationReceipt } from "./api";
import { recoverBrowserOperationState } from "./browser-operation";
import { type DevFixtureMode } from "./dev-fixture-loader";
import { productConfigFailureCertainty, productConfigOperationFailure } from "./product-config-operation";
import { InlineFormError, isOperationBusy, OperationNotice, ReasonField } from "./ProductConfigForms";
import { useBrowserOperationController } from "./use-browser-operation";
import { profileApplyFailureCertainty } from "./ProductOwnerPanel";
import type { AcceptedEvidenceResponse, ApplyProductDisplayNameData } from "./generated/openapi.ts";

type DisplayNameRequest = ApplyProductDisplayNameData["body"];
interface Change { path: string; before: string; after: string }
interface Review { request: DisplayNameRequest; changes: Change[]; changed: boolean }

function readReview(response: AcceptedEvidenceResponse, product: string, reason: string, displayName: string): Review | null {
  const result = response.result;
  if (!result || result.product !== product || result.display_name_after !== displayName ||
      typeof result.display_name_before !== "string" || typeof result.changed !== "boolean" ||
      typeof result.plan_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(result.plan_sha256)) return null;
  return { changed: result.changed,
    changes: [{ path: "Display name", before: result.display_name_before, after: displayName }],
    request: { schema_version: 1, mode: "apply", reason, display_name: displayName,
      reviewed_plan_sha256: result.plan_sha256 } };
}

export function productDisplayNameDraftKey(product: string): string {
  return `launchplane:product-profile-draft:${product}:display-name`;
}

export function ProductDisplayNamePanel({ product, fixtureMode, onSaved }: {
  product: string; fixtureMode: DevFixtureMode; onSaved: () => void;
}) {
  const scope = `${product}:display-name:apply`;
  const storageKey = productDisplayNameDraftKey(product);
  const [review, setReview] = useState<Review | null>(() => {
    try {
      if (!recoverBrowserOperationState(scope).requiresIdempotencyContinuity) {
        sessionStorage.removeItem(storageKey); return null;
      }
      const parsed = JSON.parse(sessionStorage.getItem(storageKey) ?? "null") as Review | null;
      if (!parsed || parsed.request.mode !== "apply" || typeof parsed.request.reason !== "string" || typeof parsed.request.display_name !== "string" ||
          !/^[a-f0-9]{64}$/.test(parsed.request.reviewed_plan_sha256 ?? "") ||
          typeof parsed.changed !== "boolean" || !Array.isArray(parsed.changes) ||
          !parsed.changes.every(change => change && typeof change.path === "string" &&
            typeof change.before === "string" &&
            typeof change.after === "string")) return null;
      return parsed;
    } catch { return null; }
  });
  const [displayName, setDisplayName] = useState(review?.request.display_name ?? "");
  const [reason, setReason] = useState(review?.request.reason ?? "");
  const [current, setCurrent] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [checking, setChecking] = useState(false);
  const readAbort = useRef<AbortController | null>(null);
  const preview = useBrowserOperationController<DisplayNameRequest, AcceptedEvidenceResponse>({
    scope: `${product}:display-name:plan`, readOnly: true,
    execute: (request, options) => applyProductDisplayName(product, request, options),
    failureCertainty: productConfigFailureCertainty, failureFor: productConfigOperationFailure,
  });
  const apply = useBrowserOperationController<DisplayNameRequest, AcceptedEvidenceResponse>({
    scope,
    execute: (request, options) => applyProductDisplayName(product, request, options),
    failureCertainty: profileApplyFailureCertainty,
    failureFor: productConfigOperationFailure,
  });
  const busy = checking || isOperationBusy(preview.state) || isOperationBusy(apply.state);
  const locked = busy || apply.state.requiresIdempotencyContinuity;
  const matches = Boolean(review && review.request.reason === reason.trim() && review.request.display_name === displayName.trim());

  useEffect(() => {
    if (apply.state.failure?.code === "stale" && !apply.state.requiresIdempotencyContinuity) {
      setReview(null);
      try { sessionStorage.removeItem(storageKey); } catch { /* A settled refusal has no recovery draft. */ }
      const controller = startRead();
      void refresh(controller.signal).catch(failure => {
        if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "Profile refresh failed.");
      });
    }
  }, [apply.state.failure, apply.state.requiresIdempotencyContinuity, storageKey]);

  async function refresh(signal: AbortSignal): Promise<string | false> {
    const payload = await readProductProfile(product, signal);
    if (signal.aborted) return false;
    setCurrent(payload.profile.display_name);
    return payload.profile.display_name;
  }
  function startRead(): AbortController {
    readAbort.current?.abort();
    const controller = new AbortController(); readAbort.current = controller;
    return controller;
  }
  useEffect(() => {
    if (fixtureMode) return;
    const controller = startRead();
    void refresh(controller.signal).catch(failure => {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "Profile could not be read.");
    });
    return () => { controller.abort(); readAbort.current?.abort(); };
  }, [product, fixtureMode]);

  async function previewRename() {
    setError(""); setNotice(""); setReview(null); apply.reset();
    const response = await preview.run({ mode: "dry-run", reason: reason.trim(), display_name: displayName.trim() });
    if (!response) return;
    const next = readReview(response, product, reason.trim(), displayName.trim());
    if (!next) { setError("Launchplane returned a preview this page cannot read."); return; }
    setReview(next);
  }
  async function save() {
    if (!review || !matches) return;
    setError(""); setNotice("");
    try {
      const raw = JSON.stringify(review);
      sessionStorage.setItem(storageKey, raw);
      if (sessionStorage.getItem(storageKey) !== raw) throw new Error("Draft unavailable");
    } catch { setError("The reviewed draft could not be preserved. Nothing was sent."); return; }
    const response = await apply.run(review.request);
    if (!response) return;
    const expected = review.request.display_name;
    setReview(null);
    try { sessionStorage.removeItem(storageKey); } catch { /* The operation is settled. */ }
    await readBack(expected);
  }
  async function readBack(expected?: string) {
    setChecking(true);
    const controller = startRead();
    try {
      const name = await refresh(controller.signal);
      if (controller.signal.aborted) return;
      if (expected && name !== expected) setError("Apply completed, but the current name differs. Read the profile again before another change.");
      else setNotice(`${expected ? "Applied and read back" : "Read back"}: ${name}.`);
      onSaved();
    } catch (failure) {
      if (!controller.signal.aborted) setError(`${expected ? "Apply completed; read-back" : "Profile read"} could not be confirmed. ${failure instanceof Error ? failure.message : "Read failed."}`);
    } finally { if (!controller.signal.aborted) setChecking(false); }
  }
  async function checkOriginal() {
    const identity = apply.state.identity;
    if (!identity || checking) return;
    setChecking(true); setError(""); setNotice("");
    const controller = startRead();
    try {
      const receipt = await readProductProfileMutationReceipt(product, "display-name", identity.idempotencyKey, controller.signal);
      if (controller.signal.aborted) return;
      if (receipt.state !== "completed" || receipt.product !== product || receipt.field !== "display-name" ||
          receipt.idempotency_key !== identity.idempotencyKey || !receipt.original_trace_id) {
        setNotice("No matching completed receipt. Keep this tab and key; another change remains locked."); return;
      }
      await refresh(controller.signal);
      if (controller.signal.aborted) return;
      if (apply.reconcile(identity, { trace_id: receipt.trace_id, original_trace_id: receipt.original_trace_id, replayed: true })) {
        setReview(null);
        try { sessionStorage.removeItem(storageKey); } catch { /* The completed receipt already settled the operation. */ }
        setNotice("Original rename completed. The current profile was read again."); onSaved();
      } else setNotice("The retained operation changed while checking. Keep its key and check again.");
    } catch (failure) {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "Original operation could not be checked.");
    } finally { if (!controller.signal.aborted) setChecking(false); }
  }

  return <section className="product-config-panel" aria-labelledby="product-display-name-title">
    <header className="product-config-panel-header"><span aria-hidden="true"><Pencil /></span><div>
      <p className="eyebrow">Product profile</p><h2 id="product-display-name-title">Display name</h2>
      <p>Choose the name shown in the sidebar and product workspace.</p>
    </div></header>
    <p>Current: <output aria-label="Current display name">{current ?? (fixtureMode ? "Unavailable in fixtures" : "Reading profile…")}</output></p>
    <fieldset disabled={locked || Boolean(fixtureMode) || current === null}>
      <div className="product-config-field"><label htmlFor="product-display-name-value">New display name</label><input type="text" id="product-display-name-value" value={displayName} onChange={event => { setDisplayName(event.target.value); setNotice(""); }} /></div>
      <ReasonField reason={reason} onChange={value => { setReason(value); setNotice(""); }} />
    </fieldset>
    <OperationNotice state={preview.state} label="Preview" />
    <OperationNotice state={apply.state} label="Apply" />
    {error ? <InlineFormError message={error} /> : null}
    {notice ? <p role="status">{notice}</p> : null}
    {review && matches ? <div className="product-owner-plan">
      <p>{review.changed ? "Reviewed name" : "No changes needed"}</p>
      <p>{review.changes[0]?.before} → {review.request.display_name}</p>
    </div> : null}
    <div className="product-config-actions">
      <button className="button" type="button" disabled={locked || current === null || !displayName.trim() || !reason.trim() || Boolean(fixtureMode)} onClick={() => void previewRename()}>Preview name change</button>
      <button className="button button-primary" type="button" disabled={busy || !matches || !review?.changed} onClick={() => void save()}>
        {apply.state.requiresIdempotencyContinuity ? "Retry original name change" : "Apply name change"}
      </button>
      {apply.state.requiresIdempotencyContinuity ? <button className="button" type="button" disabled={busy} onClick={() => void checkOriginal()}>Check original operation</button> : null}
      {current !== null ? <button className="button" type="button" disabled={busy || Boolean(fixtureMode)} onClick={() => void readBack()}>Read profile again</button> : null}
    </div>
  </section>;
}
