import { useEffect, useId, useRef, useState } from "react";
import { Route } from "lucide-react";
import { LaunchplaneApiError, readAuthSession, repairDomainAuthority, repairRouteBinding } from "./api";
import { recoverBrowserOperationState, type BrowserOperationOptions, type BrowserOperationFailureCertainty } from "./browser-operation";
import { productConfigFailureCertainty, productConfigOperationFailure } from "./product-config-operation";
import { useBrowserOperationController } from "./use-browser-operation";
import type { ProductEnvironmentDetail } from "./generated/openapi.ts";
import {
  checkedRepairReview, parseStoredRepairRequest, repairOperationScope, repairReadbackMatches,
  repairRequest, repairStorageKey, verifyRepairReceipt,
  type RepairKind, type RepairRequest, type RepairResponse, type RepairReview, type RepairScope,
} from "./route-record-repair";

function executeRepair(request: RepairRequest, options: BrowserOperationOptions): Promise<RepairResponse> {
  return "operation" in request ? repairDomainAuthority(request, options) : repairRouteBinding(request, options);
}

function repairFailureCertainty(error: unknown, dispatched: boolean): BrowserOperationFailureCertainty {
  // Both routes persist terminal stale refusals against the same key as the atomic write.
  if (dispatched && error instanceof LaunchplaneApiError && error.statusCode === 409 && error.code === "stale") return "settled";
  return productConfigFailureCertainty(error, dispatched);
}

export function RouteRecordRepairPanel({ detail, fixtureMode, onRefresh, refreshToken }: {
  detail: ProductEnvironmentDetail; fixtureMode: boolean; onRefresh: () => void; refreshToken: number;
}) {
  const id = useId();
  const [admin, setAdmin] = useState(false);
  const [sessionError, setSessionError] = useState("");
  const [active, setActive] = useState({ domain: false, binding: false });
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (fixtureMode) return;
    const controller = new AbortController();
    void readAuthSession(controller.signal).then(session => {
      if (!controller.signal.aborted) { setAdmin(session.is_administrator === true); setSessionError(""); }
    }).catch(error => {
      if (!controller.signal.aborted) { setAdmin(false); setSessionError(error instanceof Error ? error.message : "Administrator identity unavailable."); }
    });
    return () => controller.abort();
  }, [fixtureMode, refreshToken]);
  const scope = { product: detail.product, context: detail.context, instance: detail.environment };
  return <section className="promotion-control route-repair-panel" aria-labelledby={`${id}-title`}>
    <header className="promotion-control-header"><span aria-hidden="true"><Route /></span><div>
      <p className="eyebrow">{detail.display_name} · {detail.environment}</p>
      <h2 id={`${id}-title`}>Repair route records</h2>
      <p>Should Launchplane recognize these domains and route for this lane? Review each decision separately. The destination stays fixed.</p>
    </div></header>
    <p>These controls update Launchplane records. They do not change the provider, DNS or TLS, or prove website health.</p>
    {fixtureMode ? <p>Route-record controls are off in fixture mode.</p> : !admin ? <p role="status">{sessionError || "A verified administrator session is required to review and apply route records."}</p> : null}
    <div className="route-repair-sections">
      {(["domain", "binding"] as const).map(kind => <RepairDecision key={kind} kind={kind} scope={scope}
        admin={admin && !fixtureMode} otherActive={active[kind === "domain" ? "binding" : "domain"]}
        setActive={value => setActive(current => current[kind] === value ? current : { ...current, [kind]: value })}
        refreshToken={`${refreshToken}:${revision}`} onSaved={() => { setRevision(value => value + 1); onRefresh(); }} />)}
    </div>
  </section>;
}

function RepairDecision({ kind, scope, admin, otherActive, setActive, refreshToken, onSaved }: {
  kind: RepairKind; scope: RepairScope; admin: boolean; otherActive: boolean;
  setActive: (value: boolean) => void; refreshToken: string; onSaved: () => void;
}) {
  const id = useId();
  const operationScope = repairOperationScope(scope, kind);
  const storageKey = repairStorageKey(operationScope);
  const title = kind === "domain" ? "Domain authority" : "Route binding";
  const [pending, setPending] = useState<RepairRequest | null>(() => {
    try {
      if (!recoverBrowserOperationState(operationScope).requiresIdempotencyContinuity) {
        sessionStorage.removeItem(storageKey); return null;
      }
      return parseStoredRepairRequest(sessionStorage.getItem(storageKey), scope, kind);
    } catch { return null; }
  });
  const [reason, setReason] = useState(pending?.reason ?? "");
  const [review, setReview] = useState<RepairReview | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [message, setMessage] = useState("");
  const [accepted, setAccepted] = useState<RepairResponse | null>(null);
  const [readback, setReadback] = useState<"idle" | "pending" | "verified" | "unavailable" | "different">("idle");
  const revision = useRef(0);
  const plan = useBrowserOperationController<RepairRequest, RepairResponse>({
    scope: `${operationScope}:plan`, readOnly: true, execute: executeRepair,
    failureCertainty: productConfigFailureCertainty, failureFor: productConfigOperationFailure,
  });
  const apply = useBrowserOperationController<RepairRequest, RepairResponse>({
    scope: operationScope, failureCertainty: repairFailureCertainty, failureFor: productConfigOperationFailure,
    execute: async (request, options) => {
      const response = await executeRepair(request, { ...options, onDispatch: () => {
        options.onDispatch?.();
        const retained = recoverBrowserOperationState(operationScope);
        if (!retained.requiresIdempotencyContinuity || retained.identity?.idempotencyKey !== options.idempotencyKey) {
          throw new Error("The original operation key could not be preserved. No apply was sent; keep its recovery request.");
        }
      } });
      verifyRepairReceipt(response, scope, kind, request);
      setAccepted(response);
      return response;
    },
  });
  const recovering = apply.state.requiresIdempotencyContinuity;
  const busy = readback === "pending" || [plan.state.phase, apply.state.phase].some(phase => phase === "queued" || phase === "submitting");
  const locked = !admin || busy || recovering || otherActive;
  useEffect(() => { setActive(busy || recovering); }, [busy, recovering]);
  useEffect(() => {
    revision.current += 1;
    if (!recovering) { setReview(null); setConfirmed(false); }
  }, [refreshToken]);
  useEffect(() => {
    if (!recovering && apply.state.failure?.statusCode === 409 && apply.state.failure.code === "stale") {
      revision.current += 1; setReview(null); setConfirmed(false); setPending(null); setAccepted(null);
      try { sessionStorage.removeItem(storageKey); } catch { /* The exact original operation settled without a write. */ }
      setMessage("The reviewed evidence changed before this apply. Review the current records again before making a new decision.");
    }
  }, [apply.state.failure, recovering, storageKey]);

  function edited(value: string) {
    revision.current += 1; setReason(value); setReview(null); setConfirmed(false); setMessage("");
  }
  async function inspect() {
    if (locked || !reason.trim() || !apply.reset()) return;
    setReview(null); setConfirmed(false); setMessage(""); setAccepted(null); setReadback("idle");
    const generation = ++revision.current;
    const response = await plan.run(repairRequest(scope, kind, reason));
    if (!response || generation !== revision.current) return;
    try {
      const next = checkedRepairReview(response, scope, kind);
      if (next.applied) throw new Error("The review unexpectedly reports an applied mutation.");
      setReview(next);
    } catch (error) { setMessage(error instanceof Error ? error.message : "Review unavailable."); }
  }
  async function save() {
    if (!admin || busy || otherActive || (recovering ? !pending : !review || review.status !== "ready" || !confirmed)) return;
    const request = recovering && pending ? pending : repairRequest(scope, kind, reason, review!);
    try {
      const serialized = JSON.stringify(request);
      sessionStorage.setItem(storageKey, serialized);
      if (sessionStorage.getItem(storageKey) !== serialized) throw new Error("Recovery storage unavailable.");
    } catch { setMessage("The original reviewed request could not be preserved. Nothing was sent."); return; }
    setPending(request);
    const response = await apply.run(request);
    if (!response) return;
    setReview(null); setPending(null); setConfirmed(false);
    try { sessionStorage.removeItem(storageKey); } catch { /* Settled state never recovers a leftover request. */ }
    // The verified receipt settles this apply. Later readback cannot make it uncertain again.
    setReadback("pending");
    const readbackGeneration = revision.current;
    setMessage("Apply accepted; current record readback pending. This is not a website health check.");
    try {
      const readRequest = repairRequest(scope, kind, request.reason);
      const readResponse = await plan.run(readRequest);
      if (readbackGeneration !== revision.current) {
        setReadback("unavailable");
        setMessage("Original apply completed; a newer evidence refresh superseded this readback. Review current evidence before another decision.");
        return;
      }
      if (!readResponse) throw new Error("Record readback unavailable. The original receipt still proves completion.");
      const current = checkedRepairReview(readResponse, scope, kind);
      if (current.applied) throw new Error("The readback was not an inert review.");
      if (repairReadbackMatches(verifyRepairReceipt(response, scope, kind, request), current)) {
        setReadback("verified");
        setMessage("Route record saved and current record readback verified. Website health has not been checked.");
      } else {
        setReadback("different");
        setMessage("Original apply completed; current records now differ. Review the current evidence before another decision.");
      }
    } catch (error) {
      setReadback("unavailable");
      setMessage(`Original apply completed; current readback is unverified. ${error instanceof Error ? error.message : "Record readback unavailable."}`);
    }
    onSaved();
  }
  const failure = apply.state.failure ?? plan.state.failure;
  return <section className="route-repair-decision" aria-labelledby={`${id}-title`} aria-busy={busy}
    data-operation-phase={apply.state.phase} data-readback={readback} data-recovery={recovering}>
    <h3 id={`${id}-title`}>{title}</h3>
    <p>{kind === "domain" ? "Compare stored domains with the verified provider hosts before recognizing them." : "Join recorded destination, ingress and TLS ownership without inventing missing evidence."}</p>
    <label className="product-config-field">Reason<input aria-label={`${title} reason`} value={reason} disabled={locked} onChange={event => edited(event.target.value)} /></label>
    <button type="button" className="button button-secondary" disabled={locked || !reason.trim()} onClick={() => void inspect()}>Review {title.toLowerCase()}</button>
    {review && <div className="product-owner-plan route-repair-review" aria-label={`${title} review`}>
      <p><strong>{review.status === "ready" ? "Ready for your decision" : review.status === "unchanged" ? "Already matches; no change needed" : "Evidence blocks this decision"}</strong></p>
      {review.operation === "repair-domain-authority" && <p>Destination: <strong>{review.target_name}</strong> · {review.target_type}. Primary domain after repair: <strong>{review.proposed_primary_domain}</strong>.</p>}
      <div className="route-repair-comparison"><DomainList label="Current stored domains" domains={review.current_domains} /><DomainList label="Proposed domains" domains={review.proposed_domains} /></div>
      {review.operation === "repair-domain-authority" ? <DomainList label="Observed provider hosts" domains={review.observed_domains} /> : <>
        <p>Current binding: {review.current_record ? "present" : "absent"}. Decision: {review.binding_operation}.</p>
        {review.proposed_record && <p>Destination: {review.proposed_record.provider_target.target_name}. Ingress: {review.proposed_record.ingress.provider} · {review.proposed_record.ingress.endpoint_key || "no endpoint"}. TLS ownership: {review.proposed_record.tls.owner}.</p>}
        {review.findings.length > 0 && <ul>{review.findings.map(finding => <li key={finding.code}>{finding.detail}</li>)}</ul>}
      </>}
      <details><summary>Investigate evidence</summary><p>Lane: {scope.product} / {scope.context} / {scope.instance}</p><p>Reviewed digest: <code>{review.plan_sha256}</code></p>
        {review.operation === "repair-domain-authority" ? <p>Target: <code>{review.target_id}</code></p> : <p>Current record: <code>{review.current_record_sha256 || "absent"}</code></p>}
        <ul>{review.source_records.map(source => <li key={source.identity}><code>{source.identity}</code> · {source.version}</li>)}</ul>
      </details>
      {review.status === "ready" && <><label className="confirmation-check"><input type="checkbox" disabled={locked} checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />I reviewed this lane’s {title.toLowerCase()} and want to update its Launchplane records.</label>
        <button className="button button-primary" type="button" disabled={locked || !confirmed} onClick={() => void save()}>Apply reviewed {title.toLowerCase()}</button></>}
      <button className="button button-secondary" type="button" disabled={busy || recovering} onClick={() => { revision.current += 1; setReview(null); setConfirmed(false); }}>Leave records unchanged</button>
    </div>}
    {recovering && <div role="status"><p>The result needs recovery. Keep the original reviewed request and key.</p>
      {pending ? <button className="button button-primary" disabled={!admin || busy || otherActive} onClick={() => void save()}>Resume original {title.toLowerCase()}</button>
        : <p>The saved request is missing or invalid. Recovery is unavailable here; a fresh apply is locked to avoid duplicating the original operation.</p>}</div>}
    {message && <p role="status">{message}</p>}
    {failure && <p role="alert">{failure.message}{failure.traceId && <> Trace: <code>{failure.traceId}</code></>}</p>}
    {accepted && <details><summary>Original apply receipt</summary><p>Trace: <code>{accepted.trace_id}</code></p>
      <p>{accepted.replayed ? "Replayed original apply" : "Apply accepted"}{accepted.original_trace_id && <> · Original trace: <code>{accepted.original_trace_id}</code></>}</p>
      {accepted.review?.operation === "repair-domain-authority" ? <p>Target domain record: {accepted.review.target_name} · <code>{accepted.review.target_id}</code>. Its provider-target projection was kept consistent.</p>
        : accepted.review?.operation === "reconcile-route-binding" && <p>Binding record: <code>{accepted.review.proposed_record?.record_sha256}</code></p>}
      {accepted.review && <p>Recorded domains: {accepted.review.current_domains.join(", ") || "none"} → {accepted.review.proposed_domains.join(", ") || "none"}.</p>}
      {accepted.review?.operation === "reconcile-route-binding" && <p>Recorded binding: {accepted.review.current_record ? "present" : "absent"} → {accepted.review.binding_operation}.
        {accepted.review.proposed_record && <> Destination: {accepted.review.proposed_record.provider_target.target_name}; ingress: {accepted.review.proposed_record.ingress.provider}; TLS ownership: {accepted.review.proposed_record.tls.owner}.</>}</p>}
      <p>This receipt records the original operation; it does not prove current website health.</p>
    </details>}
  </section>;
}

function DomainList({ label, domains }: { label: string; domains: string[] }) {
  return <div><h4>{label}</h4>{domains.length ? <ul>{domains.map(domain => <li key={domain}>{domain}</li>)}</ul> : <p>No domains recorded.</p>}</div>;
}
