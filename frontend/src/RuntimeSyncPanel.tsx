import { useEffect, useId, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { applyLiveTargetRuntime, readAuthSession } from "./api";
import { recoverBrowserOperationState } from "./browser-operation";
import { productConfigOperationFailure } from "./product-config-operation";
import type { ProductEnvironmentConfigStatus, ProductEnvironmentDetail, RuntimeSyncReview, RuntimeSyncResponse } from "./generated/openapi.ts";
import type { ResourceState } from "./resource";
import { useBrowserOperationController } from "./use-browser-operation";
import {
  checkedRuntimeSyncReview, parsePendingRuntimeSync, runtimeSyncFailureCertainty,
  runtimeSyncReadbackMatches, runtimeSyncReceiptState, runtimeSyncRequest, runtimeSyncScope, runtimeSyncStorageKey,
  verifyRuntimeSyncReceipt, type PendingRuntimeSync, type RuntimeSyncRequest,
} from "./runtime-sync-operation";

export function RuntimeSyncPanel({ detail, configResource, fixtureMode, onRefresh, refreshToken }: {
  detail: ProductEnvironmentDetail; configResource: ResourceState<ProductEnvironmentConfigStatus>;
  fixtureMode: boolean; onRefresh: () => void; refreshToken: number;
}) {
  const id = useId();
  const scope = { product: detail.product, context: detail.context, instance: detail.environment };
  const operationScope = runtimeSyncScope(scope);
  const storageKey = runtimeSyncStorageKey(scope);
  const [pending, setPending] = useState<PendingRuntimeSync | null>(() => {
    try {
      if (!recoverBrowserOperationState(operationScope).requiresIdempotencyContinuity) {
        sessionStorage.removeItem(storageKey); return null;
      }
      return parsePendingRuntimeSync(sessionStorage.getItem(storageKey), scope);
    } catch { return null; }
  });
  const pendingRef = useRef(pending);
  const [admin, setAdmin] = useState(false);
  const [sessionError, setSessionError] = useState("");
  const [review, setReview] = useState<RuntimeSyncReview | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [accepted, setAccepted] = useState<RuntimeSyncResponse | null>(null);
  const [readback, setReadback] = useState<"idle" | "pending" | "verified" | "unavailable" | "different">("idle");
  const [message, setMessage] = useState("");
  const revision = useRef(0);
  const plan = useBrowserOperationController<RuntimeSyncRequest, RuntimeSyncResponse>({
    scope: `${operationScope}:plan`, readOnly: true, execute: applyLiveTargetRuntime,
    failureCertainty: runtimeSyncFailureCertainty, failureFor: productConfigOperationFailure,
  });
  const apply = useBrowserOperationController<RuntimeSyncRequest, RuntimeSyncResponse>({
    scope: operationScope, failureCertainty: runtimeSyncFailureCertainty, failureFor: productConfigOperationFailure,
    execute: async (request, options) => {
      const original = pendingRef.current;
      if (!original || JSON.stringify(original.request) !== JSON.stringify(request)) {
        throw new Error("The original reviewed request is unavailable. Nothing was sent.");
      }
      const response = await applyLiveTargetRuntime(request, { ...options, onDispatch: () => {
        options.onDispatch?.();
        const retained = recoverBrowserOperationState(operationScope);
        if (!retained.requiresIdempotencyContinuity || retained.identity?.idempotencyKey !== options.idempotencyKey) {
          throw new Error("The original request key could not be preserved. Keep its recovery request.");
        }
      } });
      verifyRuntimeSyncReceipt(response, scope, original);
      return response;
    },
  });
  const recovering = apply.state.requiresIdempotencyContinuity;
  const busy = readback === "pending" || [plan.state.phase, apply.state.phase].some(phase => ["queued", "submitting"].includes(phase));
  const config = configResource.data;
  const availability = configResource.status === "ready" && config?.product === scope.product && config.context === scope.context && config.environment === scope.instance
    ? config.runtime_sync : null;
  const planAvailable = !fixtureMode && availability?.plan.available === true;
  const applyAvailable = !fixtureMode && availability?.apply.available === true;
  const unavailable = fixtureMode ? "Runtime sync controls are off in fixture mode."
    : configResource.status === "error" ? `Runtime sync availability is unavailable. ${configResource.error}`
    : configResource.status === "idle" || configResource.status === "loading" ? "Checking runtime sync availability…"
    : !availability ? "Runtime sync availability has not been returned for this lane."
    : !availability.plan.available ? availability.plan.reason || "Runtime sync review is unavailable."
    : !availability.apply.available ? availability.apply.reason || "Runtime sync apply is unavailable." : "";

  // Current target prerequisites gate new operations, not recovery of an original key.
  useEffect(() => {
    setAdmin(false);
    if (fixtureMode) return;
    const controller = new AbortController();
    void readAuthSession(controller.signal).then(session => {
      if (!controller.signal.aborted) { setAdmin(session.is_administrator === true); setSessionError(""); }
    }).catch(error => {
      if (!controller.signal.aborted) setSessionError(error instanceof Error ? error.message : "Administrator identity unavailable.");
    });
    return () => controller.abort();
  }, [fixtureMode, refreshToken]);
  useEffect(() => {
    revision.current += 1; setReview(null); setConfirmed(false);
  }, [refreshToken, configResource.status, configResource.data]);
  useEffect(() => () => { revision.current += 1; }, []);
  useEffect(() => {
    if (apply.state.phase !== "failed" || recovering) return;
    setReview(null); setConfirmed(false); setPending(null); pendingRef.current = null;
    try { sessionStorage.removeItem(storageKey); } catch { /* This attempt settled without an uncertain effect. */ }
    if (apply.state.failure?.code === "stale") {
      setMessage("The reviewed runtime sync changed before any effect. Review the current lane again.");
    }
  }, [apply.state.phase, apply.state.failure, recovering, storageKey]);

  async function inspect() {
    if (!planAvailable || busy || recovering || !apply.reset()) return;
    setReview(null); setConfirmed(false); setMessage(""); setAccepted(null); setReadback("idle");
    const generation = ++revision.current;
    const response = await plan.run(runtimeSyncRequest(scope));
    if (!response || generation !== revision.current) return;
    try {
      const next = checkedRuntimeSyncReview(response, scope);
      if (next.applied) throw new Error("The review unexpectedly reports an applied sync.");
      setReview(next);
    } catch (error) { setMessage(error instanceof Error ? error.message : "Runtime sync review unavailable."); }
  }

  async function save() {
    if (busy || fixtureMode || (recovering ? !admin || !pending : !applyAvailable || !review || !confirmed)) return;
    const original = recovering && pending ? pending : { request: runtimeSyncRequest(scope, review!), review: review! };
    try {
      const serialized = JSON.stringify(original);
      sessionStorage.setItem(storageKey, serialized);
      if (sessionStorage.getItem(storageKey) !== serialized) throw new Error("Recovery storage unavailable.");
    } catch { setMessage("The original reviewed request could not be preserved. Nothing was sent."); return; }
    pendingRef.current = original; setPending(original);
    const response = await apply.run(original.request);
    if (!response) return;
    const completed = verifyRuntimeSyncReceipt(response, scope, original);
    setAccepted(response); setPending(null); pendingRef.current = null; setReview(null); setConfirmed(false);
    try { sessionStorage.removeItem(storageKey); } catch { /* A settled controller cannot recover a leftover request. */ }
    setReadback("pending");
    const outcome = runtimeSyncReceiptState(response) === "recovered"
      ? "Original reviewed values confirmed by readback" : "Runtime sync update and target settings verified";
    setMessage(`${outcome}; current target readback pending. No deployment was requested.`);
    const generation = revision.current;
    try {
      const currentResponse = await plan.run(runtimeSyncRequest(scope));
      if (generation !== revision.current) throw new Error("A newer evidence refresh superseded this readback.");
      if (!currentResponse) throw new Error("Current target readback unavailable.");
      const current = checkedRuntimeSyncReview(currentResponse, scope);
      if (runtimeSyncReadbackMatches(completed, current)) {
        setReadback("verified"); setMessage(`${outcome}; current target settings match. Website health has not been checked.`);
      } else {
        setReadback("different"); setMessage(`${outcome}; current target settings differ. Review current evidence before another decision.`);
      }
    } catch (error) {
      setReadback("unavailable");
      setMessage(`${outcome}; current target readback is unverified. ${error instanceof Error ? error.message : "Readback unavailable."}`);
    }
    onRefresh();
  }
  const failure = apply.state.failure ?? plan.state.failure;
  const recovered = accepted ? runtimeSyncReceiptState(accepted) === "recovered" : false;
  return <section className="promotion-control runtime-sync-panel" aria-labelledby={`${id}-title`} aria-busy={busy}
    data-operation-phase={apply.state.phase} data-recovery={recovering} data-readback={readback}
    data-outcome={accepted ? recovered ? "recovered" : "verified" : !recovering && apply.state.failure?.code === "runtime_sync_unverified" ? "unverified" : "pending"}>
    <header className="promotion-control-header"><span aria-hidden="true"><RefreshCw /></span><div>
      <p className="eyebrow">{detail.display_name} · {detail.environment}</p>
      <h2 id={`${id}-title`}>Runtime sync</h2>
      <p>Review the key names that need to match this lane’s recorded settings and secrets, then apply that exact review.</p>
    </div></header>
    <p>This updates target settings. It does not request a deployment or prove website health. Values are never displayed.</p>
    {unavailable && <p className="runtime-sync-availability" role="status">{unavailable}</p>}
    <button className="button button-secondary" type="button" disabled={!planAvailable || busy || recovering} onClick={() => void inspect()}>Review runtime sync</button>
    {review && <div className="runtime-sync-review" aria-label="Runtime sync review">
      <h3>{review.changed_keys.length ? "Ready for your review" : "Already matches; no runtime changes needed"}</h3>
      <p>Target: <strong>{review.target_name}</strong></p>
      <dl className="runtime-sync-counts"><div><dt>Recorded keys</dt><dd>{review.desired_key_count}</dd></div>
        <div><dt>Target keys</dt><dd>{review.live_key_count}</dd></div><div><dt>Unchanged</dt><dd>{review.unchanged_key_count}</dd></div>
        <div><dt>Changed</dt><dd>{review.changed_keys.length}</dd></div></dl>
      <div className="runtime-sync-keys"><KeyNames label="Missing keys" keys={review.missing_keys} /><KeyNames label="Different keys" keys={review.different_keys} />
        <KeyNames label="Retired keys still present" keys={review.retired_keys_present} /></div>
      <ReviewEvidence review={review} />
      {!!review.changed_keys.length && <><label className="confirmation-check"><input type="checkbox" checked={confirmed} disabled={!applyAvailable || busy || recovering}
        onChange={event => setConfirmed(event.target.checked)} />I reviewed this lane’s key names and want to sync its target settings without requesting a deployment.</label>
        <button className="button button-primary" type="button" disabled={!applyAvailable || busy || recovering || !confirmed} onClick={() => void save()}>Apply reviewed runtime sync</button></>}
    </div>}
    {recovering && <div className="runtime-sync-recovery" role="status"><strong>The original runtime sync outcome is unknown.</strong>
      <p>Keep the original request and key. A later refusal does not prove that the earlier sync did not finish.</p>
      {pending ? <button className="button button-primary" type="button" disabled={!admin || busy || fixtureMode} onClick={() => void save()}>Resume original runtime sync</button>
        : <p>The saved request is missing or invalid. This browser cannot resume it or start a replacement sync.</p>}
      {!admin && <p>{sessionError || "A verified administrator session is required to resume the original request."}</p>}
    </div>}
    {message && <p role="status">{message}</p>}
    {failure && <p role="alert">{failure.message}{failure.traceId && <> Trace: <code>{failure.traceId}</code></>}</p>}
    {accepted?.review && <details className="runtime-sync-receipt"><summary>Original runtime sync receipt</summary>
      <p>{recovered ? "Original reviewed values confirmed by readback." : accepted.replayed ? "Original verified sync receipt replayed." : "Runtime sync update and target settings verified."} Target: {accepted.review.target_name}.</p>
      <p>Trace: <code>{accepted.trace_id}</code>{accepted.original_trace_id && <> · Original trace: <code>{accepted.original_trace_id}</code></>}</p>
      <ReviewEvidence review={accepted.review} /></details>}
  </section>;
}

function KeyNames({ label, keys }: { label: string; keys: string[] }) {
  return <div><h4>{label} <span>({keys.length})</span></h4>{keys.length ? <ul>{keys.map(key => <li key={key}><code>{key}</code></li>)}</ul> : <p>None</p>}</div>;
}
function ReviewEvidence({ review }: { review: RuntimeSyncReview }) {
  return <details className="runtime-sync-evidence"><summary>Review evidence</summary>
    <p>Reviewed digest: <code>{review.plan_sha256}</code></p><p>Target fingerprint: <code>{review.target_fingerprint}</code></p>
    <p>Target: {review.target_type} · <code>{review.target_id}</code></p></details>;
}
