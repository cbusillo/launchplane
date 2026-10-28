import { useState } from "react";

import { readOdooSecretReferences, reconcileOdooSecretReferences } from "./api";
import { AppLink } from "./router";
import type { ProductEnvironmentConfigStatus, SecretReferenceRequest, SecretReferenceResponse } from "./generated/openapi.ts";
import { productConfigFailureCertainty, productConfigOperationFailure } from "./product-config-operation";
import { useBrowserOperationController } from "./use-browser-operation";

export function OdooSecretParameterPanel({ config, fixtureMode }: {
  config: ProductEnvironmentConfigStatus; fixtureMode: boolean;
}) {
  const [parameter, setParameter] = useState("");
  const [review, setReview] = useState<SecretReferenceResponse | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState("");
  const scope = `${config.product}:${config.environment}:odoo-secret-parameter`;
  const storageKey = `${scope}:request`;
  const [pending, setPending] = useState<SecretReferenceRequest | null>(() => {
    try { return JSON.parse(sessionStorage.getItem(storageKey) || "null"); } catch { return null; }
  });
  const execute = (request: SecretReferenceRequest, options: Parameters<typeof reconcileOdooSecretReferences>[3]) =>
    reconcileOdooSecretReferences(config.product, config.environment, request, options);
  const plan = useBrowserOperationController({ scope: `${scope}:plan`, execute, readOnly: true,
    failureCertainty: productConfigFailureCertainty, failureFor: productConfigOperationFailure });
  const apply = useBrowserOperationController({ scope: `${scope}:apply`, execute,
    failureCertainty: productConfigFailureCertainty, failureFor: productConfigOperationFailure });
  const uncertain = apply.state.requiresIdempotencyContinuity;
  const busy = [plan.state.phase, apply.state.phase].some(phase => ["queued", "submitting"].includes(phase));
  const key = parameter.trim().toLowerCase();
  const bindingKey = `ODOO_OVERRIDE_SECRET__CONFIG_PARAM__${key.toUpperCase().replaceAll(".", "__")}`;
  const declared = config.managed_secrets.some(secret => secret.binding_key === bindingKey && secret.integration === "runtime_environment");
  const enabled = config.write_availability.managed_secrets.plan.enabled && !fixtureMode;
  const reviewMatches = review?.result.entries.length === 1 && review.result.entries[0].key === key;

  async function runPlan() {
    setError(""); setReview(null); setConfirmed(false);
    if (!/^[a-z][a-z0-9_.]{0,127}$/.test(key)) { setError("Enter a valid Odoo parameter name."); return; }
    plan.reset();
    const result = await plan.run({ mode: "dry-run", keys: [key], create_missing: true });
    if (result) setReview(result);
  }

  async function runApply() {
    if (!reviewMatches || !review || !confirmed || uncertain) return;
    setError("");
    const request: SecretReferenceRequest = { mode: "apply", keys: [key], create_missing: true,
      review_digest: review.result.review_digest, confirmation: `APPLY ${config.product}/${config.environment}` };
    try {
      const encoded = JSON.stringify(request);
      sessionStorage.setItem(storageKey, encoded);
      if (sessionStorage.getItem(storageKey) !== encoded) {
        setError("The browser could not preserve this reviewed metadata request.");
        return;
      }
      setPending(request);
      const result = await apply.run(request);
      if (result) { setReview(result); setConfirmed(false); sessionStorage.removeItem(storageKey); setPending(null); }
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Reference apply is unavailable."); }
  }

  async function readBack() {
    setError("");
    try {
      const result = await readOdooSecretReferences(config.product, config.environment);
      setReview(result); setConfirmed(false);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Reference readback is unavailable."); }
  }

  async function retryOriginal() {
    if (!pending) return;
    const result = await apply.run(pending);
    if (result) { setReview(result); setConfirmed(false); sessionStorage.removeItem(storageKey); setPending(null); }
  }

  if (config.driver_id !== "odoo" || config.environment !== "testing") return null;
  return <section className="product-config-panel odoo-secret-parameter-panel" aria-labelledby="odoo-secret-parameter-title">
    <h2 id="odoo-secret-parameter-title">Use a secret in Odoo</h2>
    <p>Bind an existing private testing credential to an Odoo parameter. This saves a reference. Runtime sync, Odoo delivery and database presence verification are separate steps.</p>
    <fieldset className="product-config-field" disabled={!enabled || busy || uncertain}>
      <label>Odoo parameter name<input type="text" value={parameter} placeholder="integration.api_key" onChange={event => { setParameter(event.target.value); setReview(null); setConfirmed(false); }} /></label>
      {key ? <p>Required binding: <code>{bindingKey}</code></p> : null}
      {key && !declared ? <p><AppLink to={`/ui/products/${encodeURIComponent(config.product)}`}>Declare this managed-secret field in product setup</AppLink>, then enter its value privately above.</p> : null}
      <p>Verify the provider account cannot affect a live business system and record its testing integration allowance before binding.</p>
      <button className="button" disabled={!key || !declared} type="button" onClick={() => void runPlan()}>Review Odoo reference</button>
    </fieldset>
    {review ? <div><ul>{review.result.entries.map(entry => <li key={entry.key}><code>{entry.key}</code>: {entry.action.replaceAll("_", " ")}</li>)}</ul>
      <p>Trace: <code>{review.trace_id}</code></p>
      {reviewMatches && !review.result.read_back_matches && !review.replayed && !uncertain ? <>
        <label><input type="checkbox" checked={confirmed} disabled={busy} onChange={event => setConfirmed(event.target.checked)} />I reviewed this testing lane and reference.</label>
        <button className="button button-primary" disabled={!confirmed || busy || !config.write_availability.managed_secrets.apply.enabled} type="button" onClick={() => void runApply()}>Save Odoo reference</button>
      </> : null}
      {review.result.read_back_matches ? <p role="status">Reference saved and read back. Odoo delivery is still separate.</p> : null}
      {review.replayed ? <p role="status">The original reference operation completed. Read stored references for current metadata.</p> : null}
    </div> : null}
    <button className="button" disabled={busy || fixtureMode} type="button" onClick={() => void readBack()}>Read stored Odoo references</button>
    {uncertain ? <div><p role="alert">The apply outcome is uncertain. Keep the original request and operation key.</p>
      {pending ? <button className="button" disabled={busy} type="button" onClick={() => void retryOriginal()}>Retry original reference apply</button> : <p>The saved request is unavailable. Engineering must reconcile the original operation.</p>}</div> : null}
    {plan.state.failure || apply.state.failure ? <p role="alert">{plan.state.failure?.message || apply.state.failure?.message}</p> : null}
    {error ? <p role="alert">{error}</p> : null}
  </section>;
}
