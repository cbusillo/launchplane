import { useEffect, useId, useState } from "react";
import { applyTestingHold, readAuthSession, readTestingHold } from "./api";
import { recoverBrowserOperationState } from "./browser-operation";
import { InlineFormError, isOperationBusy, OperationNotice } from "./ProductConfigForms";
import { productConfigFailureCertainty, productConfigOperationFailure, ProductConfigRecoveryStorageUnavailable } from "./product-config-operation";
import { useBrowserOperationController } from "./use-browser-operation";
import { restoredTestingHoldRequest, testingHoldFromResponse, testingHoldIssueLink, testingHoldPlanFromResponse,
  type TestingHold, type TestingHoldRequest, type TestingHoldReview, type TestingHoldScope } from "./testing-hold";
import type { ApplyTestingHoldResponse, ProductEnvironmentDetail } from "./generated/openapi.ts";

export function TestingHoldPanel({ detail, fixtureMode, refreshToken, onRefresh }: {
  detail: ProductEnvironmentDetail; fixtureMode: boolean; refreshToken: number; onRefresh: () => void;
}) {
  const [administrator, setAdministrator] = useState<number | null>(null);
  useEffect(() => {
    if (fixtureMode) return;
    const controller = new AbortController();
    void readAuthSession(controller.signal).then(session => {
      if (!controller.signal.aborted) setAdministrator(session.is_administrator === true ? session.identity.github_id : null);
    }).catch(() => { if (!controller.signal.aborted) setAdministrator(null); });
    return () => controller.abort();
  }, [fixtureMode, refreshToken]);
  if (fixtureMode || administrator === null || detail.environment !== "testing") return null;
  return <TestingHoldControl key={`${detail.product}:${detail.context}:${administrator}`} actor={administrator}
    scope={{ product: detail.product, context: detail.context, instance: "testing" }} refreshToken={refreshToken} onRefresh={onRefresh} />;
}

function TestingHoldControl({ scope, actor, refreshToken, onRefresh }: {
  scope: TestingHoldScope; actor: number; refreshToken: number; onRefresh: () => void;
}) {
  const id = useId();
  const operationScope = `testing-hold:${actor}:${scope.product}:${scope.context}:${scope.instance}`;
  const storageKey = `${operationScope}:request`;
  const historyKey = `${operationScope}:unverified-keys`;
  const [earlierKeys, setEarlierKeys] = useState<string[]>(() => {
    try {
      const value: unknown = JSON.parse(sessionStorage.getItem(historyKey) ?? "[]");
      return Array.isArray(value) ? value.filter((key): key is string => typeof key === "string") : [];
    } catch { return []; }
  });
  const [pending, setPending] = useState<TestingHoldRequest | null>(() => {
    try {
      return recoverBrowserOperationState(operationScope).requiresIdempotencyContinuity
        ? restoredTestingHoldRequest(sessionStorage.getItem(storageKey), scope) : null;
    } catch { return null; }
  });
  const [hold, setHold] = useState<TestingHold | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [reason, setReason] = useState(pending?.reason ?? "");
  const [review, setReview] = useState<TestingHoldReview | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [reload, setReload] = useState(0);
  const plan = useBrowserOperationController<TestingHoldRequest, ApplyTestingHoldResponse>({
    scope: `${operationScope}:plan`, readOnly: true, execute: applyTestingHold,
    failureCertainty: productConfigFailureCertainty, failureFor: productConfigOperationFailure,
  });
  const apply = useBrowserOperationController<TestingHoldRequest, ApplyTestingHoldResponse>({
    scope: operationScope, allowUncertainAbandon: true, execute: async (request, options) => {
      const response = await applyTestingHold(request, { ...options, onDispatch: () => {
        options.onDispatch?.();
        const retained = recoverBrowserOperationState(operationScope);
        if (!retained.requiresIdempotencyContinuity || retained.identity?.idempotencyKey !== options.idempotencyKey) {
          throw new ProductConfigRecoveryStorageUnavailable("The operation key could not be preserved. No lift was sent.");
        }
      } });
      const result = response.result;
      if (!result || result.product !== scope.product || result.context !== scope.context || result.instance !== scope.instance ||
          result.read_back_matches !== true || result.reconcile_requested !== true || result.read_back != null) {
        throw new Error("The lift receipt could not be verified. Retain the original request.");
      }
      return response;
    }, failureCertainty: productConfigFailureCertainty, failureFor: productConfigOperationFailure,
  });
  const recovering = apply.state.requiresIdempotencyContinuity;
  const busy = isOperationBusy(plan.state) || isOperationBusy(apply.state);
  useEffect(() => {
    const controller = new AbortController();
    setLoaded(false); setReview(null); setConfirmed(false);
    void readTestingHold(scope, controller.signal).then(response => {
      if (controller.signal.aborted) return;
      setHold(testingHoldFromResponse(response, scope)); setLoaded(true); setError("");
    }).catch(error => {
      if (!controller.signal.aborted) { setLoaded(false); setError(error instanceof Error ? error.message : "Testing hold unavailable."); }
    });
    return () => controller.abort();
  }, [scope.product, scope.context, refreshToken, reload]);

  async function inspect() {
    if (!loaded || !hold || busy || recovering || !reason.trim()) return;
    setPending(null); setReview(null); setConfirmed(false); setError("");
    const response = await plan.run({ ...scope, mode: "dry-run", hold: false, reason: reason.trim() });
    if (response) {
      try { const next = testingHoldPlanFromResponse(response, scope); setHold(next.before); setReview(next); }
      catch (error) { setError(error instanceof Error ? error.message : "Lift review unavailable."); }
    }
  }
  async function lift() {
    if (busy || recovering && !pending || !recovering && (!review || !confirmed)) return;
    const request = recovering ? pending! : { ...scope, mode: "apply", hold: false, reason: reason.trim(), reviewed_plan_sha256: review!.plan_sha256 } satisfies TestingHoldRequest;
    try {
      const saved = JSON.stringify(request);
      sessionStorage.setItem(storageKey, saved);
      if (sessionStorage.getItem(storageKey) !== saved) throw new Error();
    } catch { setError("The original lift request could not be preserved in this tab. Nothing was sent."); return; }
    setPending(request); setError("");
    const response = await apply.run(request);
    setReview(null); setConfirmed(false);
    if (response) {
      setPending(null); setHold(null);
      try { sessionStorage.removeItem(storageKey); } catch { /* Settled controller ignores old drafts. */ }
      onRefresh();
    } else {
      setReload(value => value + 1);
    }
  }
  function separateReview() {
    const key = apply.state.identity?.idempotencyKey;
    if (!recovering || busy || !loaded || !key) return;
    const keys = [...new Set([...earlierKeys, key])];
    try {
      const saved = JSON.stringify(keys);
      sessionStorage.setItem(historyKey, saved);
      if (sessionStorage.getItem(historyKey) !== saved) throw new Error();
    } catch { setError("The earlier operation key could not be preserved. Keep the original request."); return; }
    if (!apply.abandon()) return;
    setEarlierKeys(keys); setPending(null); setReview(null); setConfirmed(false); setError("");
    setReload(value => value + 1);
  }
  const issueLink = hold ? testingHoldIssueLink(hold) : null;
  return <section className="promotion-control" aria-labelledby={`${id}-title`}>
    <h2 id={`${id}-title`}>Testing hold</h2>
    <p>Lifting this hold resumes testing reconciliation and can deploy the newest verified build.</p>
    {error ? <InlineFormError message={error} /> : null}
    {!loaded && !error ? <p role="status">Reading the current testing hold…</p> : null}
    {loaded && !hold ? <p>No testing hold is currently recorded.</p> : null}
    {hold ? <dl className="evidence-fact-list">
      <div><dt>Reason</dt><dd>{hold.reason}</dd></div>
      <div><dt>Set by</dt><dd>{hold.recorded_by || "Not recorded"}</dd></div>
      <div><dt>Set at</dt><dd>{hold.recorded_at ? <time dateTime={hold.recorded_at}>{new Date(hold.recorded_at).toLocaleString("en-US", { timeZone: "America/New_York", timeZoneName: "short" })}</time> : "Not recorded"}</dd></div>
      <div><dt>Linked issue</dt><dd>{issueLink ? <a href={issueLink} target="_blank" rel="noreferrer"><u>Linked issue</u></a> : "Not recorded"}</dd></div>
    </dl> : null}
    {hold?.purpose === "isolation_staging" ? <p>This is an isolation staging interlock. Use its supported activation flow.</p> : null}
    {loaded && hold && hold.purpose !== "isolation_staging" || recovering ? <>
      <label className="promotion-field"><span>Reason for lifting</span><input value={reason} maxLength={1000}
        disabled={busy || recovering} onChange={event => { setReason(event.target.value); setReview(null); setConfirmed(false); }} /></label>
      {!recovering ? <button className="button" disabled={busy || !loaded || !reason.trim()} type="button" onClick={() => void inspect()}>Review lift</button> : null}
      {review && !recovering ? <div>
        <label><input type="checkbox" disabled={busy} checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />I confirm testing can resume and deploy.</label>
        <button className="button button-primary" disabled={busy || !confirmed} type="button" onClick={() => void lift()}>Lift testing hold</button>
      </div> : null}
      {recovering ? <>
        <p>The previous lift has not settled. Retain its original request and operation key.</p>
        {pending ? <button className="button" disabled={busy} type="button" onClick={() => void lift()}>Retry original lift</button>
          : <p>The reviewed request is unavailable in this tab. Keep its operation key; the earlier outcome remains unverified.</p>}
        <p>A separate review reads the current hold again. It does not resolve the earlier lift's outcome.</p>
        <button className="button" disabled={busy || !loaded} type="button" onClick={separateReview}>Review current hold separately</button>
      </> : null}
    </> : null}
    <OperationNotice state={plan.state} label="Review lift" />
    <OperationNotice state={apply.state} label="Lift" />
    {apply.state.phase === "succeeded" ? <p role="status">Testing hold lifted. Testing reconciliation requested. Audit trace: {apply.state.receipt?.traceId}</p> : null}
    {earlierKeys.map(key => <p key={key}>Earlier lift outcome remains unverified. Original operation key: <code>{key}</code></p>)}
    {!busy && !recovering ? <button className="button" type="button" onClick={() => setReload(value => value + 1)}>Refresh hold</button> : null}
  </section>;
}
