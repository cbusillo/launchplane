import { useEffect, useId, useRef, useState } from "react";
import { applyProductHealthMonitoring, LaunchplaneApiError, readAuthSession, readProductProfile } from "./api";
import type { ApplyProductHealthMonitoringData, ProductEnvironmentDetail, ProductHealthMonitoringPlan, ProductLaneProfile } from "./generated/openapi.ts";
import { productConfigFailureCertainty, productConfigOperationFailure } from "./product-config-operation";
import { useBrowserOperationController } from "./use-browser-operation";
import { recoverBrowserOperationState } from "./browser-operation";
import type { BrowserOperationFailureCertainty } from "./browser-operation";

type MonitoringRequest = ApplyProductHealthMonitoringData["body"];

export function HealthMonitoringPanel({ detail, fixtureMode, onRefresh, refreshToken }: {
  detail: ProductEnvironmentDetail; fixtureMode: boolean; onRefresh: () => void; refreshToken: number;
}) {
  const id = useId();
  const scope = `${detail.product}:${detail.context}:${detail.environment}:health-monitoring:apply`;
  const storageKey = `launchplane:monitoring-request:${scope}`;
  const [lane, setLane] = useState<ProductLaneProfile | null>(null);
  const [admin, setAdmin] = useState(false);
  const [loading, setLoading] = useState(!fixtureMode);
  const [checkName, setCheckName] = useState("");
  const checkNameRef = useRef(checkName);
  checkNameRef.current = checkName;
  const profileRevisionRef = useRef(0);
  const refreshedStaleOperationRef = useRef("");
  const [intent, setIntent] = useState<MonitoringRequest["monitoring_intent"]>("prelaunch");
  const [enabled, setEnabled] = useState(false);
  const [identityRequired, setIdentityRequired] = useState(false);
  const [reason, setReason] = useState("");
  const [review, setReview] = useState<ProductHealthMonitoringPlan | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [message, setMessage] = useState("");
  const [profileError, setProfileError] = useState("");
  const [pending, setPending] = useState<MonitoringRequest | null>(() => {
    try {
      if (!recoverBrowserOperationState(scope).requiresIdempotencyContinuity) {
        sessionStorage.removeItem(storageKey);
        return null;
      }
      const value = sessionStorage.getItem(storageKey);
      if (!value) return null;
      const request = JSON.parse(value) as MonitoringRequest;
      return request.product === detail.product && request.context === detail.context
        && request.instance === detail.environment && request.mode === "apply" ? request : null;
    } catch { return null; }
  });
  const planOperation = useBrowserOperationController({
    scope: `${scope}:plan`, readOnly: true, execute: applyProductHealthMonitoring,
    failureCertainty: productConfigFailureCertainty, failureFor: productConfigOperationFailure,
  });
  const applyOperation = useBrowserOperationController({
    scope, execute: applyProductHealthMonitoring,
    failureCertainty: monitoringFailureCertainty, failureFor: productConfigOperationFailure,
  });
  const uncertain = applyOperation.state.requiresIdempotencyContinuity;
  const busy = [planOperation.state.phase, applyOperation.state.phase].some(phase => ["queued", "submitting"].includes(phase));
  const locked = fixtureMode || loading || !admin || busy || uncertain;
  const check = lane?.health_monitoring?.checks.find(item => item.name === checkName);
  const supported = check?.kind === "public_http" || check?.kind === "private_http";

  useEffect(() => {
    if (fixtureMode) return;
    profileRevisionRef.current += 1;
    setLoading(true); setReview(null); setConfirmed(false); setLane(null); setAdmin(false); setProfileError("");
    const controller = new AbortController();
    void Promise.all([readAuthSession(controller.signal), readProductProfile(detail.product, controller.signal)])
      .then(([session, response]) => {
        const current = response.profile.lanes.find(item => item.context === detail.context && item.instance === detail.environment);
        if (!current) throw new Error("This environment has no recorded monitoring profile.");
        setLane(current);
        setAdmin(session.identity.role === "admin");
        const supportedChecks = current.health_monitoring?.checks.filter(item => item.kind === "public_http" || item.kind === "private_http");
        const selected = supportedChecks?.find(item => item.name === checkNameRef.current) ?? supportedChecks?.[0];
        setCheckName(selected?.name ?? "");
        setIntent(current.health_monitoring?.monitoring_intent ?? "prelaunch");
        setEnabled(selected?.enabled ?? false);
        setIdentityRequired(selected?.require_runtime_identity ?? false);
      }).catch(error => {
        if (!controller.signal.aborted) setProfileError(error instanceof Error ? error.message : "Monitoring profile unavailable.");
      }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [detail.product, detail.context, detail.environment, fixtureMode, refreshToken]);

  function edited() { setReview(null); setConfirmed(false); setMessage(""); }

  async function inspect() {
    if (locked || !supported || !check) return;
    if (!applyOperation.reset()) return;
    edited();
    const reviewedProfileRevision = profileRevisionRef.current;
    const response = await planOperation.run({
      product: detail.product, context: detail.context, instance: detail.environment,
      check_name: check.name, check_kind: check.kind as "public_http" | "private_http",
      monitoring_intent: intent, enabled, require_runtime_identity: identityRequired,
      private_endpoint_key: check.private_endpoint_key, reason: reason.trim(), mode: "dry-run",
    });
    if (response?.result) {
      if (reviewedProfileRevision !== profileRevisionRef.current) return;
      if (response.result.operation === "create" || response.result.current_check_kind !== response.result.requested_check_kind) {
        setMessage("The stored check changed or was removed. Refresh this environment before editing monitoring.");
        return;
      }
      if (response.result.current_private_endpoint_key !== response.result.private_endpoint_key) {
        setMessage("The stored private endpoint changed. Refresh this environment before editing monitoring.");
        return;
      }
      setReview(response.result);
    }
  }

  async function apply() {
    if (fixtureMode || !admin || busy || (uncertain ? !pending : !review || !confirmed)) return;
    const request = uncertain && pending ? pending : {
      product: review!.product, context: review!.context, instance: review!.instance,
      check_name: review!.check_name, check_kind: review!.requested_check_kind,
      monitoring_intent: review!.requested_monitoring_intent, enabled: review!.requested_enabled,
      require_runtime_identity: review!.requested_require_runtime_identity,
      private_endpoint_key: review!.private_endpoint_key, reason: review!.reason,
      mode: "apply", reviewed_plan_sha256: review!.plan_sha256,
    } satisfies MonitoringRequest;
    try {
      // Store the exact, non-secret request before dispatch; the controller keeps its key/fingerprint.
      sessionStorage.setItem(storageKey, JSON.stringify(request));
    } catch {
      setMessage("Browser recovery storage is unavailable. No monitoring request was sent.");
      return;
    }
    setPending(request);
    const response = await applyOperation.run(request);
    if (!response?.result?.applied) return;
    sessionStorage.removeItem(storageKey);
    setPending(null); setReview(null); setConfirmed(false);
    setMessage("Monitoring policy saved. Reading back the stored check…");
    try {
      const readback = await readProductProfile(detail.product);
      const saved = readback.profile.lanes.find(item => item.context === detail.context && item.instance === detail.environment);
      const savedCheck = saved?.health_monitoring?.checks.find(item => item.name === request.check_name);
      if (!saved || !savedCheck || saved.health_monitoring?.monitoring_intent !== request.monitoring_intent
        || savedCheck.enabled !== request.enabled || savedCheck.require_runtime_identity !== request.require_runtime_identity
        || savedCheck.kind !== request.check_kind || savedCheck.private_endpoint_key !== request.private_endpoint_key
        || savedCheck.kind === "public_http" && (savedCheck.url || saved.health_url || saved.base_url) !== response.result.resolved_url) {
        throw new Error("Stored monitoring differs from this request. Refresh and inspect the current profile.");
      }
      setLane(saved); setIntent(request.monitoring_intent); setEnabled(request.enabled ?? true);
      setIdentityRequired(request.require_runtime_identity ?? false);
      setMessage("Monitoring policy saved and read back. Health and runtime identity remain unverified until the monitor records fresh evidence.");
    } catch (error) {
      setMessage(`Monitoring policy saved; readback not confirmed. ${error instanceof Error ? error.message : "Refresh to inspect the recorded policy."}`);
    }
    onRefresh();
  }

  useEffect(() => {
    if (applyOperation.state.phase === "failed" && !uncertain) {
      sessionStorage.removeItem(storageKey); setPending(null); setReview(null); setConfirmed(false);
      const key = applyOperation.state.identity?.idempotencyKey ?? "";
      if (applyOperation.state.failure?.code === "stale" && key && refreshedStaleOperationRef.current !== key) {
        refreshedStaleOperationRef.current = key;
        setMessage("The reviewed profile changed. Refreshing stored monitoring before a new review; no successful apply is claimed.");
        onRefresh();
      }
    }
  }, [applyOperation.state.phase, applyOperation.state.identity?.idempotencyKey, applyOperation.state.failure?.code, uncertain, storageKey]);

  const failure = applyOperation.state.failure ?? planOperation.state.failure;
  return <section className="promotion-control monitoring-control" aria-labelledby={`${id}-title`} aria-busy={loading || busy}>
    <header className="promotion-control-header"><div>
      <p className="eyebrow">{detail.display_name} · {detail.environment}</p>
      <h2 id={`${id}-title`}>Health monitoring</h2>
      <p>Review one stored check and this lane’s monitoring intent. Apply changes monitoring policy; fresh health evidence comes from the next monitor run.</p>
    </div></header>
    {fixtureMode ? <p>Monitoring controls are off in fixture mode.</p> : loading ? <p role="status">Loading recorded monitoring…</p>
      : !admin ? <p>Sign in as an administrator to edit monitoring.</p> : null}
    {!loading && lane && !lane.health_monitoring?.checks.length ? <p>No monitoring checks are recorded for this lane.</p> : null}
    <label className="promotion-field"><span>Stored check</span><select disabled={locked} value={checkName} onChange={event => {
      const selected = lane?.health_monitoring?.checks.find(item => item.name === event.target.value);
      setCheckName(event.target.value); setEnabled(selected?.enabled ?? false);
      setIdentityRequired(selected?.require_runtime_identity ?? false); edited();
    }}><option value="" disabled>Select a check</option>{lane?.health_monitoring?.checks.map(item => <option key={item.name} value={item.name} disabled={item.kind === "provider"}>{item.name} ({item.kind})</option>)}</select></label>
    <p className="monitoring-endpoint">Stored endpoint: <code>{check?.kind === "private_http" ? `Registered private endpoint: ${check.private_endpoint_key}` : check?.url || lane?.health_url || lane?.base_url || "Not recorded"}</code></p>
    <label className="promotion-field"><span>Monitoring intent</span><select disabled={locked} value={intent} onChange={event => { setIntent(event.target.value as MonitoringRequest["monitoring_intent"]); edited(); }}>
      <option value="public">Public</option><option value="private">Private</option><option value="prelaunch">Prelaunch</option>
    </select></label>
    <label className="monitoring-checkbox"><input type="checkbox" disabled={locked || !supported} checked={enabled} onChange={event => { setEnabled(event.target.checked); if (!event.target.checked) setIdentityRequired(false); edited(); }} />Check enabled</label>
    <label className="monitoring-checkbox"><input type="checkbox" disabled={locked || !supported || !enabled} checked={identityRequired} onChange={event => { setIdentityRequired(event.target.checked); edited(); }} />Require runtime identity</label>
    <label className="promotion-field"><span>Reason</span><input disabled={locked} value={reason} onChange={event => { setReason(event.target.value); edited(); }} /></label>
    <button className="secondary-button" disabled={locked || !supported || !reason.trim()} onClick={() => void inspect()}>Review monitoring change</button>
    {review ? <div className="monitoring-review">
      <h3>Reviewed monitoring change</h3>
      <p>{review.product} · {review.instance} · {review.check_name}</p>
      <dl>
        <div><dt>Monitoring intent</dt><dd>{review.current_monitoring_intent} → {review.requested_monitoring_intent}</dd></div>
        <div><dt>Check enabled</dt><dd>{String(review.current_enabled)} → {String(review.requested_enabled)}</dd></div>
        <div><dt>Require runtime identity</dt><dd>{String(review.current_require_runtime_identity)} → {String(review.requested_require_runtime_identity)}</dd></div>
        <div><dt>Endpoint preserved</dt><dd>{review.requested_check_kind === "private_http" ? `Registered private endpoint: ${review.private_endpoint_key}` : review.resolved_url || "No public endpoint recorded"}</dd></div>
      </dl>
      <label className="monitoring-checkbox"><input type="checkbox" disabled={locked} checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />I confirm this monitoring change on {detail.environment}.</label>
      <button className="primary-button" disabled={locked || !confirmed} onClick={() => void apply()}>Apply reviewed monitoring</button>
    </div> : null}
    {uncertain ? <div role="status"><p>The previous apply has not settled. Keep its original request and operation key.</p>
      {pending ? <button className="secondary-button" disabled={fixtureMode || !admin || busy} onClick={() => void apply()}>Retry original monitoring request</button>
        : <p>Original request unavailable in browser storage. Keep this change unresolved for administrator diagnosis.</p>}
    </div> : null}
    {failure ? <p role="alert">{failure.message} {failure.traceId ? `Trace: ${failure.traceId}` : ""}</p> : null}
    {profileError ? <p role="alert">{profileError}</p> : null}
    {message ? <p role="status" aria-label="Monitoring change result">{message}</p> : null}
  </section>;
}

function monitoringFailureCertainty(error: unknown, dispatched: boolean): BrowserOperationFailureCertainty {
  // Same-key replay precedes stale checks, and profile CAS prevents a late conflicting write.
  // Refresh the current policy after this refusal without treating it as successful apply.
  if (dispatched && error instanceof LaunchplaneApiError && error.statusCode === 409 && error.code === "stale") return "settled";
  return productConfigFailureCertainty(error, dispatched);
}
