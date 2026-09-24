import { useEffect, useRef, useState, type SubmitEvent } from "react";

import { readOwnerSecretInputs, submitOwnerSecretInput } from "./api";
import { loadDevFixtures, type DevFixtureMode } from "./dev-fixture-loader";
import { formatTime } from "./format";
import type { OwnerSecretInputField, OwnerSecretInputResponse } from "./generated/openapi.ts";
import { useAppSearchParams } from "./router";

export function OwnerSecretInputsRoute({ fixtureMode }: { fixtureMode: DevFixtureMode }) {
  const params = useAppSearchParams();
  const product = params.get("product") || "";
  const environment = params.get("environment") || "";
  return <SecretRequests key={`${product}:${environment}`} product={product} environment={environment} fixtureMode={fixtureMode} />;
}

function SecretRequests({ product, environment, fixtureMode }: { product: string; environment: string; fixtureMode: DevFixtureMode }) {
  const [request, setRequest] = useState<OwnerSecretInputResponse | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    if (!product || !environment) {
      setError("Open the credential setup link sent to you.");
      return;
    }
    const read = fixtureMode
      ? loadDevFixtures().then(fixtures => fixtures.ownerSecretInputsForFixture(fixtureMode, product, environment))
      : readOwnerSecretInputs(product, environment, controller.signal);
    void read
      .then(result => { if (!controller.signal.aborted) setRequest(result); })
      .catch(() => { if (!controller.signal.aborted) setError("This credential request is unavailable. Ask the person who sent you the link to check it."); });
    return () => controller.abort();
  }, [product, environment, fixtureMode]);

  return <section className="owner-review-page">
    <div className="owner-review-intro">
      <p className="eyebrow">Credential setup</p>
      <h1 data-route-heading tabIndex={-1}>{request?.display_name || "Provide requested credentials"}</h1>
      {request ? <p>{request.environment} environment</p> : null}
      <p>Your submission is stored securely for the operator to apply to this product.</p>
    </div>
    {error ? <p role="alert">{error}</p> : !request ? <p role="status">Loading credential request…</p> : !request.fields.length ? <p>No credentials are requested here.</p> : request.fields.map(field => (
      <SecretInput key={field.request_revision} field={field} request={request} onSaved={setRequest} fixtureMode={fixtureMode} />
    ))}
  </section>;
}

function SecretInput({ field, request, onSaved, fixtureMode }: { field: OwnerSecretInputField; request: OwnerSecretInputResponse; onSaved: (response: OwnerSecretInputResponse) => void; fixtureMode: DevFixtureMode }) {
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    const element = input.current;
    return () => { if (element) element.value = ""; };
  }, []);

  async function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = input.current?.value || "";
    if (input.current) input.current.value = "";
    setError("");
    setSaved(false);
    if (!value.trim()) { setError("Enter the requested credential."); return; }
    setBusy(true);
    try {
      const response = fixtureMode
        ? await loadDevFixtures().then(fixtures => fixtures.ownerSecretInputsForFixture(fixtureMode, request.product, request.environment, true))
        : await submitOwnerSecretInput({ product: request.product, environment: request.environment, request_revision: field.request_revision, value });
      onSaved(response);
      setSaved(true);
    } catch {
      setError("The submission could not be confirmed. Refresh to check its receipt before entering the value again.");
    } finally {
      if (input.current) input.current.value = "";
      setBusy(false);
    }
  }

  return <form className="owner-review-card" onSubmit={event => void submit(event)}>
    <h2>{field.label}</h2>
    <p>Requested for: {field.environments.join(", ")}</p>
    <p>{field.instructions}</p>
    {field.submitted_at ? <p>Last received: {formatTime(field.submitted_at)}</p> : null}
    {request.can_submit ? <>
      <label className="owner-secret-field">
        <span>{field.label}</span>
        <input ref={input} type="password" autoComplete="new-password" maxLength={65536} disabled={busy} aria-label={field.label} />
      </label>
      <button className="button button-primary" type="submit" disabled={busy}>{busy ? "Saving…" : field.submitted_at ? "Replace credential" : "Save credential"}</button>
    </> : <p>Only this product’s named Owner can submit the requested credential.</p>}
    {saved ? <p role="status">Credential received. The operator can now apply it.</p> : null}
    {error ? <p role="alert">{error}</p> : null}
  </form>;
}
