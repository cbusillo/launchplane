import { useEffect, useState } from "react";

import { applyIntegrationAllowances, readIntegrationAllowances } from "./api";
import type { ApplyIntegrationAllowancesData, ProductEnvironmentConfigStatus } from "./generated/openapi.ts";
import { productConfigFailureCertainty, productConfigOperationFailure } from "./product-config-operation";
import { useBrowserOperationController } from "./use-browser-operation";

type Request = ApplyIntegrationAllowancesData["body"];
type Allowance = NonNullable<Request["allowances"]>[number];

export function TestingIntegrationAllowancePanel({ config, fixtureMode }: {
  config: ProductEnvironmentConfigStatus; fixtureMode: boolean;
}) {
  const scope = { product: config.product, context: config.context, instance: config.environment };
  const operationScope = `${scope.product}:${scope.context}:${scope.instance}:integration-allowance`;
  const storageKey = `${operationScope}:request`;
  const [allowances, setAllowances] = useState<Allowance[] | null>(null);
  const [integration, setIntegration] = useState("");
  const [reason, setReason] = useState("");
  const [evidence, setEvidence] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [review, setReview] = useState<Request | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState<Request | null>(() => {
    try {
      const saved = JSON.parse(sessionStorage.getItem(storageKey) || "null");
      return saved?.product === scope.product && saved?.context === scope.context && saved?.instance === scope.instance && saved?.mode === "apply" ? saved : null;
    } catch { return null; }
  });
  const plan = useBrowserOperationController({ scope: `${operationScope}:plan`, execute: applyIntegrationAllowances, readOnly: true,
    failureCertainty: productConfigFailureCertainty, failureFor: productConfigOperationFailure });
  const apply = useBrowserOperationController({ scope: operationScope, execute: applyIntegrationAllowances,
    failureCertainty: productConfigFailureCertainty, failureFor: productConfigOperationFailure });
  const uncertain = apply.state.requiresIdempotencyContinuity;
  const busy = [plan.state.phase, apply.state.phase].some(phase => ["queued", "submitting"].includes(phase));
  const enabled = config.write_availability.managed_secrets.plan.enabled && !fixtureMode;

  async function readCurrent(signal?: AbortSignal) {
    try {
      const response = await readIntegrationAllowances(scope, signal);
      if (signal?.aborted) return;
      const result = response.result;
      if (!result || result.product !== scope.product || result.context !== scope.context || result.instance !== scope.instance || !Array.isArray(result.allowances) || result.allowances.some(item =>
        !item || typeof item.integration !== "string" || typeof item.kind !== "string" || typeof item.reason !== "string" || typeof item.evidence !== "string")) {
        setAllowances(null); setError("The stored allowance response does not match this lane."); return;
      }
      setAllowances(result.allowances.map(item => {
        return { integration: item.integration, kind: item.kind as Allowance["kind"], reason: item.reason, evidence: item.evidence };
      }));
      setReview(null); setConfirmed(false); setError("");
    } catch (cause) { if (!signal?.aborted) { setAllowances(null); setError(cause instanceof Error ? cause.message : "Allowance read unavailable."); } }
  }

  useEffect(() => {
    if (fixtureMode || config.driver_id !== "odoo" || scope.instance !== "testing") return;
    const controller = new AbortController();
    void readCurrent(controller.signal);
    return () => controller.abort();
  }, [scope.product, scope.context, scope.instance, fixtureMode, config.driver_id]);

  async function inspect() {
    if (!allowances || !integration.trim() || !reason.trim() || !evidence.trim()) return;
    const name = integration.trim().toLowerCase();
    const request: Request = { ...scope, mode: "dry-run", reason: reason.trim(), allowances: [
      ...allowances.filter(item => item.integration !== name),
      { integration: name, kind: "dev_store", reason: reason.trim(), evidence: evidence.trim() },
    ] };
    setReview(null); setConfirmed(false); setError(""); plan.reset();
    const response = await plan.run(request);
    if (response && (!Array.isArray(response.result?.changes) || response.result.changes.some(change =>
      !change || typeof change.integration !== "string" || (change.integration !== name && change.action !== "unchanged")))) {
      setError("Other lane allowances changed. Read stored testing allowances and review again.");
      return;
    }
    if (response && typeof response.result?.plan_sha256 === "string") {
      setReview({ ...request, mode: "apply", reviewed_plan_sha256: response.result.plan_sha256 });
    }
  }

  async function save() {
    const request = uncertain ? pending : confirmed ? review : null;
    if (!request) return;
    try {
      const encoded = JSON.stringify(request);
      sessionStorage.setItem(storageKey, encoded);
      if (sessionStorage.getItem(storageKey) !== encoded) { setError("The original allowance request could not be preserved. Nothing was sent."); return; }
      setPending(request);
      const response = await apply.run(request);
      if (response) { sessionStorage.removeItem(storageKey); setPending(null); await readCurrent(); }
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Allowance apply unavailable."); }
  }

  if (config.driver_id !== "odoo" || scope.instance !== "testing") return null;
  return <section className="product-config-panel" aria-labelledby="testing-integration-allowances-title">
    <h2 id="testing-integration-allowances-title">Testing integration allowances</h2>
    <p>Record a verified independent testing account. It must not be able to change a live business system. Enter account or device identifiers as evidence, never the key.</p>
    <ul>{allowances?.map(item => <li key={item.integration}>{item.integration}: {item.kind.replaceAll("_", " ")}</li>)}</ul>
    <fieldset className="product-config-field" disabled={!enabled || !allowances || busy || uncertain} onChange={() => { setReview(null); setConfirmed(false); }}>
      <label>Integration name<input value={integration} placeholder="integration" onChange={event => setIntegration(event.target.value)} /></label>
      <label>Testing account reason<input value={reason} onChange={event => setReason(event.target.value)} /></label>
      <label>Isolation evidence<input value={evidence} onChange={event => setEvidence(event.target.value)} /></label>
      <button className="button" type="button" disabled={!integration.trim() || !reason.trim() || !evidence.trim()} onClick={() => void inspect()}>Review testing allowance</button>
    </fieldset>
    {review && !uncertain ? <div><p>Record an independent testing account for {integration.trim().toLowerCase()}. Other allowances stay as reviewed.</p>
      <label><input type="checkbox" checked={confirmed} disabled={busy} onChange={event => setConfirmed(event.target.checked)} />I verified this account cannot affect a live business system.</label>
      <button className="button button-primary" type="button" disabled={!confirmed || busy || !config.write_availability.managed_secrets.apply.enabled} onClick={() => void save()}>Save testing allowance</button>
    </div> : null}
    <button className="button" type="button" disabled={busy || fixtureMode} onClick={() => void readCurrent()}>Read stored testing allowances</button>
    {uncertain ? <div><p role="alert">The allowance outcome is uncertain. Keep the original request and operation key.</p>
      {pending ? <button className="button" type="button" disabled={busy} onClick={() => void save()}>Retry original allowance apply</button> : <p>The original request is unavailable. Engineering must reconcile it.</p>}</div> : null}
    {plan.state.failure || apply.state.failure ? <p role="alert">{plan.state.failure?.message || apply.state.failure?.message}</p> : null}
    {error ? <p role="alert">{error}</p> : null}
  </section>;
}
