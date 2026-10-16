import { useEffect, useRef, useState } from "react";
import { Layers } from "lucide-react";
import { applyProductGenericWebTransition, readAuthSession, readProductProfile, readProductProfileMutationReceipt } from "./api";
import { recoverBrowserOperationState } from "./browser-operation";
import { type DevFixtureMode } from "./dev-fixture-loader";
import { productConfigFailureCertainty, productConfigOperationFailure } from "./product-config-operation";
import { InlineFormError, isOperationBusy, OperationNotice, ReasonField } from "./ProductConfigForms";
import { useBrowserOperationController } from "./use-browser-operation";
import { profileApplyFailureCertainty } from "./ProductOwnerPanel";
import type { AcceptedEvidenceResponse, ApplyProductGenericWebTransitionData, LaunchplaneProductProfileRecord } from "./generated/openapi.ts";

type TransitionRequest = ApplyProductGenericWebTransitionData["body"];
type Declarations = Pick<TransitionRequest, "lanes" | "preview">;
interface Review { request: TransitionRequest; changed: boolean; driverBefore: string }

function declarations(profile: LaunchplaneProductProfileRecord): Declarations {
  return { lanes: profile.lanes.map(lane => ({ context: lane.context, instance: lane.instance, extensions: lane.generic_web_extensions ?? { maintenance: [], schedules: [] } })), preview: {
    data_transport_mode: profile.preview.data_transport_mode,
    generic_web_extensions: profile.preview.generic_web_extensions,
    transport_extension: profile.preview.transport_extension ?? "",
    transport_database_prefix: profile.preview.transport_database_prefix ?? "",
    app_name_suffix: profile.preview.app_name_suffix ?? "",
  } };
}
function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => `${JSON.stringify(key)}:${stable(item)}`).join(",")}}`;
  return JSON.stringify(value) ?? "null";
}
function parseDeclarations(raw: string): Declarations {
  const value: unknown = JSON.parse(raw);
  if (!value || typeof value !== "object" || Array.isArray(value) ||
      !Array.isArray((value as Declarations).lanes) ||
      Object.keys(value).some(key => key !== "lanes" && key !== "preview")) throw new Error("Supply lane declarations and optional preview transport only.");
  // The typed service validates every field before returning a review.
  return value as Declarations;
}
function readReview(response: AcceptedEvidenceResponse, product: string, reason: string): Review | null {
  const result = response.result;
  if (!result || result.product !== product || result.driver_after !== "generic-web" || typeof result.driver_before !== "string" ||
      typeof result.changed !== "boolean" || typeof result.plan_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(result.plan_sha256) ||
      !Array.isArray(result.lanes) || !result.preview || typeof result.preview !== "object") return null;
  return { changed: result.changed, driverBefore: result.driver_before, request: {
    schema_version: 1, mode: "apply", reason, lanes: result.lanes as TransitionRequest["lanes"],
    preview: result.preview as TransitionRequest["preview"], reviewed_plan_sha256: result.plan_sha256,
  } };
}

export function ProductGenericWebTransitionPanel({ product, fixtureMode, onSaved }: {
  product: string; fixtureMode: DevFixtureMode; onSaved: () => void;
}) {
  const scope = `${product}:generic-web-transition:apply`;
  const storageKey = `launchplane:product-profile-draft:${product}:generic-web-transition`;
  const [review, setReview] = useState<Review | null>(() => {
    try {
      if (!recoverBrowserOperationState(scope).requiresIdempotencyContinuity) { sessionStorage.removeItem(storageKey); return null; }
      const parsed = JSON.parse(sessionStorage.getItem(storageKey) ?? "null") as Review | null;
      if (!parsed || parsed.request.mode !== "apply" || typeof parsed.request.reason !== "string" ||
          !/^[a-f0-9]{64}$/.test(parsed.request.reviewed_plan_sha256 ?? "") || !Array.isArray(parsed.request.lanes) ||
          typeof parsed.changed !== "boolean" || typeof parsed.driverBefore !== "string") return null;
      return parsed;
    } catch { return null; }
  });
  const [definition, setDefinition] = useState(review ? JSON.stringify({ lanes: review.request.lanes, preview: review.request.preview }, null, 2) : "");
  const [reviewedDefinition, setReviewedDefinition] = useState(review ? definition : "");
  const [reason, setReason] = useState(review?.request.reason ?? "");
  const [admin, setAdmin] = useState(false);
  const [current, setCurrent] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [checking, setChecking] = useState(false);
  const readAbort = useRef<AbortController | null>(null);
  const preview = useBrowserOperationController<TransitionRequest, AcceptedEvidenceResponse>({
    scope: `${product}:generic-web-transition:plan`, readOnly: true,
    execute: (request, options) => applyProductGenericWebTransition(product, request, options),
    failureCertainty: productConfigFailureCertainty, failureFor: productConfigOperationFailure,
  });
  const apply = useBrowserOperationController<TransitionRequest, AcceptedEvidenceResponse>({
    scope, execute: (request, options) => applyProductGenericWebTransition(product, request, options),
    failureCertainty: profileApplyFailureCertainty, failureFor: productConfigOperationFailure,
  });
  const busy = checking || isOperationBusy(preview.state) || isOperationBusy(apply.state);
  const locked = busy || !admin || apply.state.requiresIdempotencyContinuity;
  const matches = Boolean(review && review.request.reason === reason.trim() && definition === reviewedDefinition);
  function startRead(): AbortController { readAbort.current?.abort(); const controller = new AbortController(); readAbort.current = controller; return controller; }
  async function refresh(signal: AbortSignal): Promise<LaunchplaneProductProfileRecord | null> {
    const payload = await readProductProfile(product, signal);
    if (signal.aborted) return null;
    setCurrent(payload.profile.driver_id);
    return payload.profile;
  }
  useEffect(() => {
    if (fixtureMode) return;
    const controller = startRead();
    void Promise.all([readAuthSession(controller.signal), refresh(controller.signal)]).then(([session, profile]) => {
      if (controller.signal.aborted) return;
      setAdmin(session.identity.role === "admin");
      if (profile && !review) setDefinition(JSON.stringify(declarations(profile), null, 2));
    }).catch(failure => { if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "Profile could not be read."); });
    return () => { controller.abort(); readAbort.current?.abort(); };
  }, [product, fixtureMode]);
  useEffect(() => {
    if (apply.state.failure?.code === "stale" && !apply.state.requiresIdempotencyContinuity) {
      setReview(null); setReviewedDefinition("");
      try { sessionStorage.removeItem(storageKey); } catch { /* Refusal is settled. */ }
      const controller = startRead();
      void refresh(controller.signal).catch(failure => { if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "Profile refresh failed."); });
    }
  }, [apply.state.failure, apply.state.requiresIdempotencyContinuity, storageKey]);
  async function reviewTransition() {
    setError(""); setNotice(""); setReview(null); apply.reset();
    let input: Declarations;
    try { input = parseDeclarations(definition); } catch (failure) { setError(failure instanceof Error ? failure.message : "Declarations are invalid."); return; }
    const response = await preview.run({ ...input, mode: "dry-run", reason: reason.trim() });
    if (!response) return;
    const next = readReview(response, product, reason.trim());
    if (!next) { setError("Launchplane returned a review this page cannot read."); return; }
    setReview(next); setReviewedDefinition(definition);
  }
  async function readBack(expected?: TransitionRequest) {
    setChecking(true); const controller = startRead();
    try {
      const profile = await refresh(controller.signal);
      if (!profile) return;
      if (expected && (profile.driver_id !== "generic-web" || stable(declarations(profile)) !== stable({ lanes: expected.lanes, preview: expected.preview }))) {
        setError("Apply completed, but the current declarations differ. Read the profile before another change.");
      } else { setNotice(expected ? "Shared-web profile applied and read back. Testing qualification is still required; no production deployment was started." : `Read back: ${profile.driver_id}.`); onSaved(); }
    } catch (failure) { if (!controller.signal.aborted) setError(`Profile read-back could not be confirmed. ${failure instanceof Error ? failure.message : "Read failed."}`); }
    finally { if (!controller.signal.aborted) setChecking(false); }
  }
  async function save() {
    if (!review || !matches) return;
    setError(""); setNotice("");
    try { const raw = JSON.stringify(review); sessionStorage.setItem(storageKey, raw); if (sessionStorage.getItem(storageKey) !== raw) throw new Error("Draft unavailable"); }
    catch { setError("The reviewed draft could not be preserved. Nothing was sent."); return; }
    const response = await apply.run(review.request);
    if (!response) return;
    const expected = review.request; setReview(null);
    try { sessionStorage.removeItem(storageKey); } catch { /* The operation is settled. */ }
    await readBack(expected);
  }
  async function checkOriginal() {
    const identity = apply.state.identity;
    if (!identity || checking) return;
    setChecking(true); setError(""); setNotice(""); const controller = startRead();
    try {
      const receipt = await readProductProfileMutationReceipt(product, "generic-web-transition", identity.idempotencyKey, controller.signal);
      if (controller.signal.aborted) return;
      if (receipt.state !== "completed" || receipt.product !== product || receipt.field !== "generic-web-transition" || receipt.idempotency_key !== identity.idempotencyKey || !receipt.original_trace_id) {
        setNotice("No matching completed receipt. Keep this tab and key; another change remains locked."); return;
      }
      const profile = await refresh(controller.signal);
      if (!profile || controller.signal.aborted) return;
      const matchesOriginal = review && profile.driver_id === "generic-web" && stable(declarations(profile)) === stable({ lanes: review.request.lanes, preview: review.request.preview });
      if (apply.reconcile(identity, { trace_id: receipt.trace_id, original_trace_id: receipt.original_trace_id, replayed: true })) {
        setReview(null); try { sessionStorage.removeItem(storageKey); } catch { /* Receipt settled the operation. */ }
        if (!matchesOriginal) {
          setError("The original transition completed, but current declarations differ from its review. Review the current profile before another change.");
          setNotice("");
        } else setNotice("Original profile transition completed. Current profile read again; testing qualification remains separate.");
        onSaved();
      }
    } catch (failure) { if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "Original operation could not be checked."); }
    finally { if (!controller.signal.aborted) setChecking(false); }
  }
  return <section className="product-config-panel product-generic-web-panel" aria-labelledby="product-generic-web-title">
    <header className="product-config-panel-header"><span aria-hidden="true"><Layers /></span><div>
      <p className="eyebrow">Product profile</p><h2 id="product-generic-web-title">Shared web driver</h2>
      <p>Review application maintenance, recurring schedules and preview transport before switching this product to the shared web driver.</p>
    </div></header>
    <p>Current driver: <output>{current ?? (fixtureMode ? "Unavailable in fixtures" : "Reading profile…")}</output></p>
    <p>Apply changes profile declarations. The next testing reconcile qualifies the declared lifecycle. Production keeps its recorded deployment until an accepted release.</p>
    {!admin && current !== null && !fixtureMode ? <p>A signed-in administrator reviews and applies these declarations.</p> : null}
    <fieldset disabled={locked || Boolean(fixtureMode) || current === null}>
      <div className="product-config-field"><label htmlFor="product-generic-web-declarations">Lane and preview declarations</label><textarea id="product-generic-web-declarations" rows={12} value={definition} onChange={event => { setDefinition(event.target.value); setNotice(""); }} /></div>
      <ReasonField reason={reason} onChange={value => { setReason(value); setNotice(""); }} />
    </fieldset>
    <OperationNotice state={preview.state} label="Review" /><OperationNotice state={apply.state} label="Apply" />
    {error ? <InlineFormError message={error} /> : null}{notice ? <p role="status">{notice}</p> : null}
    {review && matches ? <div className="product-owner-plan"><p>{review.driverBefore} → generic-web</p>
      {review.request.lanes.map(lane => <div key={`${lane.context}:${lane.instance}`}><strong>{lane.instance} · {lane.context}</strong><p>Verification: {lane.extensions.verification_extension || "none"}</p>
        {lane.extensions.maintenance?.map(command => <p key={command.name}>{command.name}: <code>{command.command}</code></p>)}
        {lane.extensions.schedules?.map(schedule => <p key={schedule.name}>{schedule.name}: <code>{schedule.command}</code> · {schedule.cron_expression} · {schedule.enabled ? "enabled" : "disabled"}</p>)}
      </div>)}<p>Preview transport: {review.request.preview?.transport_extension || review.request.preview?.data_transport_mode}; application suffix: {review.request.preview?.app_name_suffix || "none"}</p>
    </div> : null}
    <div className="product-config-actions">
      <button className="button" type="button" disabled={locked || current === null || !definition || !reason.trim() || Boolean(fixtureMode)} onClick={() => void reviewTransition()}>Review shared-web transition</button>
      <button className="button button-primary" type="button" disabled={busy || !admin || !matches || !review?.changed} onClick={() => void save()}>{apply.state.requiresIdempotencyContinuity ? "Retry original transition" : "Apply shared-web profile"}</button>
      {apply.state.requiresIdempotencyContinuity ? <button className="button" type="button" disabled={busy} onClick={() => void checkOriginal()}>Check original operation</button> : null}
      {current !== null ? <button className="button" type="button" disabled={busy || Boolean(fixtureMode)} onClick={() => void readBack()}>Read profile again</button> : null}
    </div>
  </section>;
}
