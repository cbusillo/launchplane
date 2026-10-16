import { useState, type ChangeEvent } from "react";

import { deliverTestingLocalConsumer } from "./api";
import type { ConsumerDeliveryResponse, ConsumerRequest } from "./generated/openapi.ts";

export function LocalConsumerDeliveryPanel({ product, context, environment }: {
  product: string; context: string; environment: string;
}) {
  const [request, setRequest] = useState<ConsumerRequest | null>(null);
  const [result, setResult] = useState<ConsumerDeliveryResponse | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (environment !== "testing") return null;

  async function load(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    setRequest(null); setResult(null); setReviewed(false); setError("");
    if (!file) return;
    try {
      if (file.size > 65536) throw new Error();
      const candidate = JSON.parse(await file.text()) as ConsumerRequest;
      if (candidate.product !== product || candidate.context !== context ||
          candidate.environment !== "testing" || candidate.service_origin !== window.location.origin ||
          candidate.kind !== "agent_session_token_file_v1") throw new Error();
      setRequest(candidate);
    } catch {
      setError("Choose the helper’s request.json for this testing environment and this Launchplane service.");
    }
  }

  async function send(apply: boolean) {
    if (!request || (apply && (!result || !reviewed))) return;
    setBusy(true); setError("");
    try {
      setResult(await deliverTestingLocalConsumer({
        request, mode: apply ? "apply" : "dry-run",
        reviewed_plan_digest: apply ? result!.plan_digest : "",
      }));
    } catch {
      setError(apply
        ? "Delivery is unconfirmed. Retry the original delivery here; keep its request and destination. A changed or expired request needs a new private testing directory."
        : "Delivery is not ready. Check the testing bridge binding, HTTPS endpoint and request expiry. Enter no token here.");
    } finally { setBusy(false); }
  }

  function download() {
    if (!result?.package) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(result.package)], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url; link.download = "testing-local-delivery.json"; link.click();
    URL.revokeObjectURL(url);
  }

  return <section className="product-config-panel local-consumer-panel">
    <h2>Deliver to a testing client</h2>
    <p>Start the local helper in a new private testing directory, then choose its request file.
      Review the destination and compare its fingerprint with the helper before delivery.
      The download is encrypted for that local recipient; no token is displayed.</p>
    <label>Local recipient request
      <input type="file" accept=".json,application/json" disabled={busy || result !== null}
        onChange={event => void load(event)} />
    </label>
    {request ? <>
      <dl>
        <dt>Environment</dt><dd>{product} / testing</dd>
        <dt>Bridge</dt><dd>{request.server_url}</dd>
        <dt>Private local directory</dt><dd>{request.directory}</dd>
      </dl>
      {!result ? <button className="button" disabled={busy} onClick={() => void send(false)}>Run delivery dry-run</button> : <>
        <dl><dt>Recipient fingerprint</dt><dd>{result.request_fingerprint}</dd>
          <dt>Managed version</dt><dd>{result.version_id}</dd></dl>
        {!result.ready_to_deliver ? <>
          <p role="status">Destination verified. The isolated testing credential is not ready; complete its managed-secret setup, then recheck.</p>
          <button className="button" disabled={busy} onClick={() => void send(false)}>Recheck original destination</button>
        </> : !result.package ? <>
          <label><input type="checkbox" checked={reviewed} disabled={busy}
            onChange={event => setReviewed(event.target.checked)} />
            I compared the fingerprint and verified this private testing destination.</label>
          <button className="button button-primary" disabled={busy || !reviewed} onClick={() => void send(true)}>
            Deliver to this testing client
          </button>
        </> : <>
          <p role="status">Encrypted delivery prepared. Run the local receiver to verify and install it.</p>
          <button className="button" onClick={download}>Download encrypted delivery</button>
        </>}
      </>}
    </> : null}
    {error ? <p role="alert">{error}</p> : null}
  </section>;
}
