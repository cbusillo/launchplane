import { useEffect, useState, type ReactNode } from "react";
import { applyProductExpectedConfig, applyProductStableLaneRepair, readProductProfile, LaunchplaneApiError } from "./api";
import { recoverBrowserOperationState } from "./browser-operation";
import { productConfigFailureCertainty, productConfigOperationFailure } from "./product-config-operation";
import { InlineFormError, isOperationBusy, OperationNotice } from "./ProductConfigForms";
import { useBrowserOperationController } from "./use-browser-operation";
import { AppLink, productEnvironmentPath } from "./router";
import { declarationSetupRequest, environmentSetupRequest, reviewedSetupApply, verifySetupReadback, setupDeclarationDiff, type BindingDraft, type SetupKind, type SetupRequest } from "./product-setup-operation";
import type { AcceptedEvidenceResponse, LaunchplaneProductProfileRecord } from "./generated/openapi.ts";

export function ProductSetupPanel({ product, onApplied }: { product: string; onApplied: () => void }) {
  const [environmentOpen, setEnvironmentOpen] = useState(() => recoverBrowserOperationState(`${product}:setup:environment:apply`).requiresIdempotencyContinuity);
  const [declarationsOpen, setDeclarationsOpen] = useState(() => recoverBrowserOperationState(`${product}:setup:declarations:apply`).requiresIdempotencyContinuity);
  const [profile, setProfile] = useState<LaunchplaneProductProfileRecord | null>(null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [context, setContext] = useState("");
  const [instance, setInstance] = useState("testing");
  const [baseUrl, setBaseUrl] = useState("");
  const [environmentReason, setEnvironmentReason] = useState("");
  const [selectedLane, setSelectedLane] = useState("");
  const [bindings, setBindings] = useState<BindingDraft[]>([{ binding_key: "", integration: "runtime_environment" }]);
  const [declarationReason, setDeclarationReason] = useState("");
  const [savedLane, setSavedLane] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setError("");
    readProductProfile(product, controller.signal).then(payload => {
      setProfile(payload.profile);
      setContext(current => current || payload.profile.lanes[0]?.context || "");
    }).catch(cause => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "Product setup is unavailable."); });
    return () => controller.abort();
  }, [product, refresh]);
  function applied(lane: string) {
    setDeclarationsOpen(true);
    setSavedLane(lane);
    setSelectedLane(lane);
    setRefresh(value => value + 1);
    onApplied();
  }
  const lanes = profile?.lanes.filter(lane => !["prod", "production"].includes(lane.instance.toLowerCase())) ?? [];
  const lane = lanes.find(item => item.instance === selectedLane);
  const contexts = [...new Set(profile?.lanes.map(item => item.context) ?? [])];
  return <section className="product-setup-panel" aria-labelledby="product-setup-title">
    <p className="eyebrow">Administrator · Environment setup</p>
    <h2 id="product-setup-title">Set up a non-production environment</h2>
    <p>Add a prepared target to this product, then declare the managed-secret fields it needs. These operations update metadata without releasing or activating the product.</p>
    {error ? <InlineFormError message={error} /> : null}
    {!profile && !error ? <p role="status">Loading product setup…</p> : null}
    {profile ? <>
      <details open={environmentOpen} onToggle={event => setEnvironmentOpen(event.currentTarget.open)}>
        <summary>Add environment</summary>
        <p>The target records and HTTPS origin must already be prepared by engineering. The service verifies them before adding the environment.</p>
        <SetupChange key={`${product}:environment`} product={product} kind="environment" makeRequest={() => environmentSetupRequest(product, context, instance, baseUrl, environmentReason)} onApplied={request => { if ("instance" in request) applied(request.instance); }}>
          <label>Product context<select value={context} onChange={event => setContext(event.target.value)}>{contexts.map(value => <option key={value}>{value}</option>)}</select></label>
          <label>Environment name<input value={instance} onChange={event => setInstance(event.target.value)} required /></label>
          <label>Prepared target HTTPS origin<input value={baseUrl} onChange={event => setBaseUrl(event.target.value)} placeholder="https://testing.example.com" required /></label>
          <label>Reason<input value={environmentReason} onChange={event => setEnvironmentReason(event.target.value)} required /></label>
        </SetupChange>
      </details>
      <details open={declarationsOpen} onToggle={event => setDeclarationsOpen(event.currentTarget.open)}>
        <summary>Declare managed-secret fields</summary>
        <p>Declarations contain binding names and scope only. Enter values later on the environment's Managed secrets screen.</p>
        {!lanes.length ? <p role="status">Add a non-production environment before declaring its fields.</p> : <SetupChange key={`${product}:declarations`} product={product} kind="declarations" makeRequest={() => { if (!lane) throw new Error("Select a non-production environment."); return declarationSetupRequest(product, lane, bindings, declarationReason); }} onApplied={request => { const requirement = "managed_secret_bindings" in request ? request.managed_secret_bindings?.[0] : null; if (requirement?.instance) applied(requirement.instance); }}>
          <label>Environment<select value={selectedLane} onChange={event => setSelectedLane(event.target.value)} required><option value="">Select environment</option>{lanes.map(item => <option key={item.instance} value={item.instance}>{item.instance}</option>)}</select></label>
          {lane ? <p>Scope: <code>{lane.context} / {lane.instance}</code> · context_instance</p> : null}
          {bindings.map((binding, index) => <div className="setup-binding-row" key={index}>
            <label>Binding key {index + 1}<input value={binding.binding_key} onChange={event => setBindings(current => current.map((item, i) => i === index ? { ...item, binding_key: event.target.value } : item))} required /></label>
            <label>Integration {index + 1}<input value={binding.integration} onChange={event => setBindings(current => current.map((item, i) => i === index ? { ...item, integration: event.target.value } : item))} required /></label>
            {bindings.length > 1 ? <button className="button" type="button" onClick={() => setBindings(current => current.filter((_, i) => i !== index))}>Remove declaration {index + 1}</button> : null}
          </div>)}
          <button className="button" type="button" onClick={() => setBindings(current => [...current, { binding_key: "", integration: "runtime_environment" }])}>Add another declaration</button>
          <label>Reason<input value={declarationReason} onChange={event => setDeclarationReason(event.target.value)} required /></label>
        </SetupChange>}
      </details>
      {savedLane || lane ? <AppLink to={productEnvironmentPath(product, savedLane || lane!.instance, "managed-secrets")}>Open {savedLane || lane!.instance} managed secrets</AppLink> : null}
    </> : null}
  </section>;
}

function SetupChange({ product, kind, makeRequest, onApplied, children }: { product: string; kind: SetupKind; makeRequest: () => SetupRequest; onApplied: (request: SetupRequest) => void; children: ReactNode }) {
  const scope = `${product}:setup:${kind}:apply`;
  const storageKey = `launchplane:setup-draft:${scope}`;
  const [recovered] = useState<SetupRequest | null>(() => {
    if (!recoverBrowserOperationState(scope).requiresIdempotencyContinuity) return null;
    try { const value = JSON.parse(sessionStorage.getItem(storageKey) ?? "null") as SetupRequest | null; return value?.product === product && value.mode === "apply" ? value : null; } catch { return null; }
  });
  const [reviewed, setReviewed] = useState<{ request: SetupRequest; response: AcceptedEvidenceResponse } | null>(null);
  const [pending, setPending] = useState<SetupRequest | null>(recovered);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState("");
  const execute = async (request: SetupRequest, options: Parameters<typeof applyProductExpectedConfig>[1]) => {
    const response = kind === "environment" && "instance" in request
      ? await applyProductStableLaneRepair(request, options)
      : kind === "declarations" && "managed_secret_bindings" in request
        ? await applyProductExpectedConfig(request, options)
        : null;
    if (!response) throw new Error("The saved setup request does not match this operation.");
    if (request.mode === "apply") {
      try {
        const readback = await readProductProfile(product, options.signal);
        verifySetupReadback(kind, request, readback.profile);
      } catch (cause) {
        // The POST was accepted; a GET refusal cannot prove the write failed.
        throw new Error(`Apply accepted (${response.trace_id}); read-back could not verify it: ${cause instanceof Error ? cause.message : "unavailable"}`);
      }
    }
    return response;
  };
  const planOperation = useBrowserOperationController({ scope: `${scope}:plan`, execute, failureCertainty: productConfigFailureCertainty, failureFor: productConfigOperationFailure, readOnly: true });
  const applyOperation = useBrowserOperationController({ scope, execute, failureCertainty: (cause, dispatched) => {
    // This route replays its atomic receipt before comparing the plan, so stale
    // proves this exact request did not commit. A fresh auth denial cannot.
    if (kind === "environment" && cause instanceof LaunchplaneApiError && cause.statusCode === 409 && cause.code === "stale") return "settled";
    return productConfigFailureCertainty(cause, dispatched);
  }, failureFor: productConfigOperationFailure });
  const busy = isOperationBusy(planOperation.state) || isOperationBusy(applyOperation.state);
  const uncertain = applyOperation.state.requiresIdempotencyContinuity;
  let reviewMatchesDraft = false;
  try { reviewMatchesDraft = Boolean(reviewed && JSON.stringify(makeRequest()) === JSON.stringify(reviewed.request)); } catch { /* An incomplete edited draft invalidates review. */ }
  async function plan() {
    setError(""); setReviewed(null); setConfirmed(false);
    try {
      const request = makeRequest();
      applyOperation.reset();
      const response = await planOperation.run(request);
      if (response) { reviewedSetupApply(kind, request, response); setReviewed({ request, response }); }
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Invalid setup request."); }
  }
  async function apply(retry = false) {
    setError("");
    try {
      let request: SetupRequest;
      if (retry && uncertain && pending) request = pending;
      else {
        if (!reviewed || !confirmed || JSON.stringify(makeRequest()) !== JSON.stringify(reviewed.request)) throw new Error("The draft changed. Run a new dry-run and confirm its diff.");
        request = reviewedSetupApply(kind, reviewed.request, reviewed.response);
        const raw = JSON.stringify(request);
        sessionStorage.setItem(storageKey, raw);
        if (sessionStorage.getItem(storageKey) !== raw) throw new Error("The browser could not preserve this reviewed request for a safe retry.");
        setPending(request);
      }
      const response = await applyOperation.run(request);
      if (response) { sessionStorage.removeItem(storageKey); setPending(null); setReviewed(null); setConfirmed(false); onApplied(request); }
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Apply could not start."); }
  }
  return <div className="setup-change">
    <fieldset disabled={busy || uncertain} onChange={() => { setReviewed(null); setConfirmed(false); }}>
      {children}
    </fieldset>
    {error ? <InlineFormError message={error} /> : null}
    <OperationNotice state={planOperation.state} label="Setup dry-run" />
    <OperationNotice state={applyOperation.state} label="Setup apply" />
    <button className="button" type="button" disabled={busy || uncertain} onClick={() => void plan()}>Run dry-run</button>
    {reviewed && reviewMatchesDraft ? <div className="setup-review"><h3>Exact service diff</h3>{kind === "declarations" ? <div className="setup-diff-table"><table><thead><tr><th>Change</th><th>Binding key</th><th>Integration</th><th>Context / instance</th></tr></thead><tbody>{setupDeclarationDiff(reviewed.response).map(row => <tr key={`${row.binding_key}:${row.integration}`}><td>{row.change}</td><td>{row.binding_key}</td><td>{row.integration}</td><td>{row.context} / {row.instance}</td></tr>)}</tbody></table></div> : <p>Add <strong>{"instance" in reviewed.request ? reviewed.request.instance : ""}</strong> at {"base_url" in reviewed.request ? reviewed.request.base_url : ""}. The service result lists the existing environments it preserves.</p>}<details><summary>Full service evidence</summary><pre>{JSON.stringify(reviewed.response.result, null, 2)}</pre></details><p>Trace: <code>{reviewed.response.trace_id}</code></p><label className="setup-confirmation"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />I reviewed this environment and the exact metadata changes.</label><button className="button button-primary" type="button" disabled={!confirmed || busy} onClick={() => void apply()}>Apply reviewed change</button></div> : null}
    {uncertain ? <div role="alert"><p>The apply outcome is uncertain. Keep this request and its original operation key until it settles.</p>{pending ? <><pre>{JSON.stringify(pending, null, 2)}</pre><button className="button" type="button" disabled={busy} onClick={() => void apply(true)}>Retry original apply</button></> : <p>The original request is unavailable. Ask engineering to reconcile the operation before continuing.</p>}</div> : null}
  </div>;
}
