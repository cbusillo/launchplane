import { useEffect, useRef, useState } from "react";
import { Globe } from "lucide-react";
import { applyProductWebsiteCleanup, readProductProfile, readProductProfileMutationReceipt } from "./api";
import { recoverBrowserOperationState } from "./browser-operation";
import { type DevFixtureMode } from "./dev-fixture-loader";
import { productConfigFailureCertainty, productConfigOperationFailure } from "./product-config-operation";
import { InlineFormError, isOperationBusy, OperationNotice, ReasonField } from "./ProductConfigForms";
import { useBrowserOperationController } from "./use-browser-operation";
import { profileApplyFailureCertainty } from "./ProductOwnerPanel";
import type { AcceptedEvidenceResponse, ApplyProductWebsiteCleanupData } from "./generated/openapi.ts";

type CleanupRequest = ApplyProductWebsiteCleanupData["body"];
interface Change { path: string; before: string | boolean; after: string | boolean }
interface Review { request: CleanupRequest; changes: Change[]; changed: boolean }

function readReview(response: AcceptedEvidenceResponse, product: string, reason: string): Review | null {
  const result = response.result;
  if (!result || result.product !== product || result.public_website_after !== "none" ||
      typeof result.changed !== "boolean" || typeof result.plan_sha256 !== "string" ||
      !/^[a-f0-9]{64}$/.test(result.plan_sha256) || !Array.isArray(result.changes)) return null;
  const changes: Change[] = [];
  for (const change of result.changes) {
    if (!change || typeof change !== "object" || typeof change.path !== "string" ||
        !["string", "boolean"].includes(typeof change.before) ||
        !["string", "boolean"].includes(typeof change.after)) return null;
    changes.push(change as Change);
  }
  return { changed: result.changed, changes, request: {
    schema_version: 1, mode: "apply", reason, reviewed_plan_sha256: result.plan_sha256,
  } };
}

export function ProductWebsiteCleanupPanel({ product, fixtureMode, onSaved }: {
  product: string; fixtureMode: DevFixtureMode; onSaved: () => void;
}) {
  const scope = `${product}:website-cleanup:apply`;
  const storageKey = `launchplane:product-profile-draft:${product}:website-cleanup`;
  const [review, setReview] = useState<Review | null>(() => {
    try {
      if (!recoverBrowserOperationState(scope).requiresIdempotencyContinuity) {
        sessionStorage.removeItem(storageKey); return null;
      }
      const parsed = JSON.parse(sessionStorage.getItem(storageKey) ?? "null") as Review | null;
      if (!parsed || parsed.request.mode !== "apply" || typeof parsed.request.reason !== "string" ||
          !/^[a-f0-9]{64}$/.test(parsed.request.reviewed_plan_sha256 ?? "") ||
          typeof parsed.changed !== "boolean" || !Array.isArray(parsed.changes) ||
          !parsed.changes.every(change => change && typeof change.path === "string" &&
            ["string", "boolean"].includes(typeof change.before) &&
            ["string", "boolean"].includes(typeof change.after))) return null;
      return parsed;
    } catch { return null; }
  });
  const [reason, setReason] = useState(review?.request.reason ?? "");
  const [current, setCurrent] = useState<string | null>(null);
  const [eligible, setEligible] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [checking, setChecking] = useState(false);
  const readAbort = useRef<AbortController | null>(null);
  const preview = useBrowserOperationController<CleanupRequest, AcceptedEvidenceResponse>({
    scope: `${product}:website-cleanup:plan`, readOnly: true,
    execute: (request, options) => applyProductWebsiteCleanup(product, request, options),
    failureCertainty: productConfigFailureCertainty, failureFor: productConfigOperationFailure,
  });
  const apply = useBrowserOperationController<CleanupRequest, AcceptedEvidenceResponse>({
    scope,
    execute: (request, options) => applyProductWebsiteCleanup(product, request, options),
    failureCertainty: profileApplyFailureCertainty,
    failureFor: productConfigOperationFailure,
  });
  const busy = checking || isOperationBusy(preview.state) || isOperationBusy(apply.state);
  const locked = busy || apply.state.requiresIdempotencyContinuity;
  const matches = Boolean(review && review.request.reason === reason.trim());

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

  async function refresh(signal: AbortSignal): Promise<boolean> {
    const payload = await readProductProfile(product, signal);
    if (signal.aborted) return false;
    setCurrent(payload.profile.public_website ?? "required");
    setEligible(Boolean(payload.profile.lanes?.length) &&
      payload.profile.lanes!.every(lane => lane.health_monitoring?.monitoring_intent === "private"));
    return payload.profile.public_website === "none";
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

  async function previewCleanup() {
    setError(""); setNotice(""); setReview(null); apply.reset();
    const response = await preview.run({ mode: "dry-run", reason: reason.trim() });
    if (!response) return;
    const next = readReview(response, product, reason.trim());
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
    setReview(null);
    try { sessionStorage.removeItem(storageKey); } catch { /* The operation is settled. */ }
    await readBack(true);
  }
  async function readBack(applied = false) {
    setChecking(true);
    const controller = startRead();
    try {
      if (await refresh(controller.signal)) {
        setNotice(`${applied ? "Applied and read back" : "Read back"}: no public website. Private health and incidents still determine lane status.`);
        onSaved();
      } else if (applied) setError("Apply completed, but the current profile does not record no public website. Read the profile again before another change.");
      else setNotice("Read back: public website required.");
    } catch (failure) {
      if (!controller.signal.aborted) setError(`${applied ? "Apply completed; read-back" : "Profile read"} could not be confirmed. ${failure instanceof Error ? failure.message : "Read failed."}`);
    } finally { if (!controller.signal.aborted) setChecking(false); }
  }
  async function checkOriginal() {
    const identity = apply.state.identity;
    if (!identity || checking) return;
    setChecking(true); setError(""); setNotice("");
    const controller = startRead();
    try {
      const receipt = await readProductProfileMutationReceipt(product, "website-cleanup", identity.idempotencyKey, controller.signal);
      if (controller.signal.aborted) return;
      if (receipt.state !== "completed" || receipt.product !== product || receipt.field !== "website-cleanup" ||
          receipt.idempotency_key !== identity.idempotencyKey || !receipt.original_trace_id) {
        setNotice("No matching completed receipt. Keep this tab and key; another change remains locked."); return;
      }
      await refresh(controller.signal);
      if (controller.signal.aborted) return;
      if (apply.reconcile(identity, { trace_id: receipt.trace_id, original_trace_id: receipt.original_trace_id, replayed: true })) {
        setReview(null);
        try { sessionStorage.removeItem(storageKey); } catch { /* The completed receipt already settled the operation. */ }
        setNotice("Original cleanup completed. The current profile was read again."); onSaved();
      } else setNotice("The retained operation changed while checking. Keep its key and check again.");
    } catch (failure) {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "Original operation could not be checked.");
    } finally { if (!controller.signal.aborted) setChecking(false); }
  }

  return <section className="product-config-panel" aria-labelledby="product-website-cleanup-title">
    <header className="product-config-panel-header"><span aria-hidden="true"><Globe /></span><div>
      <p className="eyebrow">Product profile</p><h2 id="product-website-cleanup-title">Public website</h2>
      <p>For a product with no website, review and withdraw its stale public URLs and public HTTP checks together. Private endpoints, checks, Client and release settings are preserved.</p>
      <p>Withdraw only obsolete URLs. This also removes release testing links and URLs used by public probes, Odoo diagnostics, web restart, production restore and ingress route edits. This panel has no undo; restoring public declarations requires product-profile configuration.</p>
    </div></header>
    <p>Current: {current === null ? fixtureMode ? "Unavailable in fixtures" : "Reading profile…" : current === "none" ? "No public website" : "Public website required"}</p>
    {current !== "none" && current !== null && !eligible ? <p>Cleanup requires private monitoring intent on every recorded lane. Public or unknown intent needs review.</p> : null}
    <fieldset disabled={locked || !eligible || Boolean(fixtureMode)}>
      <ReasonField reason={reason} onChange={value => { setReason(value); setNotice(""); }} />
    </fieldset>
    <OperationNotice state={preview.state} label="Preview" />
    <OperationNotice state={apply.state} label="Apply" />
    {error ? <InlineFormError message={error} /> : null}
    {notice ? <p role="status">{notice}</p> : null}
    {review && matches ? <div className="product-owner-plan product-website-cleanup-plan">
      <p>{review.changed ? "Reviewed changes" : "No changes needed"}</p>
      <ul>{review.changes.map(change => <li key={change.path}>
        <strong>{change.path}</strong>: {String(change.before) || "Empty"} → {String(change.after) || "Empty"}
      </li>)}</ul>
    </div> : null}
    <div className="product-config-actions">
      <button className="button" type="button" disabled={locked || !eligible || !reason.trim() || Boolean(fixtureMode)} onClick={() => void previewCleanup()}>Preview no-website cleanup</button>
      <button className="button button-primary" type="button" disabled={busy || !matches || !review?.changed} onClick={() => void save()}>
        {apply.state.requiresIdempotencyContinuity ? "Retry original cleanup" : "Apply no-website cleanup"}
      </button>
      {apply.state.requiresIdempotencyContinuity ? <button className="button" type="button" disabled={busy} onClick={() => void checkOriginal()}>Check original operation</button> : null}
      {current !== null ? <button className="button" type="button" disabled={busy || Boolean(fixtureMode)} onClick={() => void readBack()}>Read profile again</button> : null}
    </div>
  </section>;
}
