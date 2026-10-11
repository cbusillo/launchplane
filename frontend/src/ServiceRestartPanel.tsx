import { useEffect, useState } from "react";
import { LaunchplaneApiError, readProductActivity, restartLaneService } from "./api";
import type { LaneServiceRestartResponse, ProductActivityEvent, ProductEnvironmentDetail, RestartLaneServiceData } from "./generated/openapi.ts";

type PendingRestart = { payload: RestartLaneServiceData["body"]; key: string };

export function ServiceRestartPanel({ detail, fixtureMode, onRefresh }: {
  detail: ProductEnvironmentDetail; fixtureMode: boolean; onRefresh: () => void;
}) {
  const storageKey = `launchplane:service-restart:${detail.product}:${detail.context}:${detail.environment}`;
  const [service, setService] = useState("web");
  const [reason, setReason] = useState("");
  const [review, setReview] = useState<LaneServiceRestartResponse | null>(null);
  const [pending, setPending] = useState<PendingRestart | null>(() => {
    try {
      const value = sessionStorage.getItem(storageKey);
      if (!value) return null;
      const saved = JSON.parse(value) as PendingRestart;
      if (saved.payload.product === detail.product && saved.payload.context === detail.context
        && saved.payload.instance === detail.environment && saved.payload.mode === "apply"
        && saved.key && saved.payload.reviewed_plan_sha256) return saved;
    } catch { /* Storage may be unavailable. */ }
    return null;
  });
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(pending ? "The previous restart has not settled. Resume only that request." : "");
  const [confirmed, setConfirmed] = useState(false);
  const [recoveries, setRecoveries] = useState<ProductActivityEvent[]>([]);
  const [recoveryError, setRecoveryError] = useState("");
  useEffect(() => {
    if (fixtureMode) return;
    const controller = new AbortController();
    void readProductActivity(detail.product, controller.signal).then(response => {
      setRecoveries(response.activity.events.filter(event => event.context === detail.context
        && event.environment === detail.environment && event.restart_recovery));
    }).catch(error => { if (!controller.signal.aborted) setRecoveryError(error instanceof Error ? error.message : "Activity recovery is unavailable."); });
    return () => controller.abort();
  }, [detail.product, detail.context, detail.environment, fixtureMode]);
  const blocked = fixtureMode || busy || !!pending;

  function clearReview() { setReview(null); setConfirmed(false); setMessage(""); }

  async function inspect() {
    setBusy(true); setMessage("");
    try {
      setReview(await restartLaneService({ product: detail.product, context: detail.context,
        instance: detail.environment, service, reason, mode: "dry-run" }));
      setConfirmed(false);
    } catch (error) { setReview(null); setMessage(error instanceof Error ? error.message : "Restart inspection failed."); }
    finally { setBusy(false); }
  }

  async function restart() {
    if (!pending && (!review || !confirmed)) return;
    const attempt = pending ?? { key: crypto.randomUUID(), payload: {
      product: detail.product, context: detail.context, instance: detail.environment,
      service, reason: review!.result.plan.reason, mode: "apply", reviewed_plan_sha256: review!.result.plan_sha256,
    } } satisfies PendingRestart;
    setPending(attempt); setBusy(true); setMessage("Restarting; verifying the same artifact and health…");
    try { sessionStorage.setItem(storageKey, JSON.stringify(attempt)); } catch { /* Server also fences unknown effects. */ }
    try {
      const response = await restartLaneService({ ...attempt.payload, mode: pending ? "reconcile" : "apply" }, attempt.key);
      setMessage(response.result.status === "pass" ? "Restart verified. Same version; service healthy."
        : response.result.error_message || "Restart did not verify. Inspect product activity before another attempt.");
      setReview(null); setConfirmed(false); setPending(null);
      setRecoveries(events => events.filter(event => event.restart_recovery?.idempotency_key !== attempt.key));
      try { sessionStorage.removeItem(storageKey); } catch { /* Optional browser storage. */ }
      onRefresh();
    } catch (error) {
      const unavailableHandle = !!pending && error instanceof LaunchplaneApiError
        && (["restart_receipt_unavailable", "idempotency_key_reused"].includes(error.code)
          || error.statusCode === 403 && error.code === "authorization_denied");
      const refused = unavailableHandle || !pending && error instanceof LaunchplaneApiError && (
        [400, 401, 403, 404, 422].includes(error.statusCode)
        || ["restart_refused", "restart_identity_changed", "restart_target_busy", "idempotency_key_reused"].includes(error.code)
      );
      if (refused) {
        setPending(null); setReview(null); setConfirmed(false);
        if (unavailableHandle) setRecoveries(events => events.filter(event => event.restart_recovery?.idempotency_key !== attempt.key));
        try { sessionStorage.removeItem(storageKey); } catch { /* Optional browser storage. */ }
      }
      setMessage(`${error instanceof Error ? error.message : "Restart outcome is unknown."} ${unavailableHandle
        ? "This handle does not identify a recoverable restart for your account. Inspect activity and use the account that started it."
        : refused
        ? "The request was refused before a service change. Inspect again."
        : "Resume this request to read its result; do not start another restart."}`);
    } finally { setBusy(false); }
  }

  return <section className="promotion-control" aria-labelledby="service-restart-title">
    <header className="promotion-control-header"><div>
      <p className="eyebrow">Service recovery</p><h2 id="service-restart-title">Restart on the same version</h2>
      <p>Briefly interrupts this service. Launchplane keeps its current artifact, configuration and volumes, and refuses while a release holds the lane.</p>
    </div></header>
    {fixtureMode ? <p>Restart controls are off in fixture mode.</p> : null}
    <label className="promotion-field"><span>Service</span><input value={pending?.payload.service ?? service} disabled={blocked} onChange={event => { setService(event.target.value); clearReview(); }} /></label>
    <label className="promotion-field"><span>Reason</span><input value={pending?.payload.reason ?? reason} maxLength={1000} disabled={blocked} onChange={event => { setReason(event.target.value); clearReview(); }} /></label>
    <button className="secondary-button" type="button" disabled={blocked || !reason.trim() || !/^[a-z0-9][a-z0-9._-]{0,127}$/.test(service)} onClick={() => void inspect()}>Inspect restart</button>
    {review ? <div>
      <p className="service-restart-identity">Current artifact: {review.result.plan.artifact_id}. Service: {review.result.plan.service}. Container: {review.result.plan.before.container_id.slice(0, 12)}.</p>
      <label><input type="checkbox" checked={confirmed} disabled={blocked} onChange={event => setConfirmed(event.target.checked)} />I confirm this service interruption on {detail.environment}.</label>
      <button className="danger-button" type="button" disabled={blocked || !confirmed} onClick={() => void restart()}>Restart {service} (same version)</button>
    </div> : null}
    {pending ? <button className="secondary-button" type="button" disabled={fixtureMode || busy} onClick={() => void restart()}>Resume existing restart request</button> : null}
    {!pending ? recoveries.map(event => <div key={event.event_id}>
      <p>{event.title}. Resume with the account that started this request.</p>
      <button className="secondary-button" type="button" disabled={fixtureMode || busy} onClick={() => {
        const recovery = event.restart_recovery!;
        const attempt = { payload: recovery.request, key: recovery.idempotency_key };
        setPending(attempt); setReview(null); setConfirmed(false);
        setMessage("Original request restored from activity. Resume to read its result; no new restart will be dispatched.");
        try { sessionStorage.setItem(storageKey, JSON.stringify(attempt)); } catch { /* Activity retains the handle. */ }
      }}>Recover restart from activity</button>
    </div>) : null}
    {recoveryError ? <p>Activity recovery unavailable: {recoveryError}</p> : null}
    <p role="status">{message}</p>
  </section>;
}
