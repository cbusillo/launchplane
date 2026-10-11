import { useEffect, useState } from "react";
import { LaunchplaneApiError, readPreviewHistory, readProductReconcileRequests } from "./api";
import { formatTime } from "./format";
import type { PreviewGenerationRecord, ProductReconcileRequestView, ProductSiteOverview } from "./generated/openapi.ts";
import { emptyResource, type ResourceState } from "./resource";
import { AppLink, productActivityPath } from "./router";
import { safeExternalUrl } from "./url";
import { useEvidenceRefresh } from "./use-evidence-refresh";
import "./preview-inventory.css";

function useReadEvidence<T extends { trace_id: string }>(
  key: string, enabled: boolean, refreshToken: number,
  read: (key: string, signal?: AbortSignal) => Promise<T>,
) {
  const [resource, setResource] = useState<ResourceState<T>>(emptyResource());
  const [retry, setRetry] = useState(0);
  const refresh = () => setRetry(value => value + 1);
  useEvidenceRefresh(key, enabled, resource, refresh);
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    let active = true;
    setResource(current => ({ ...current, status: "loading", error: "", traceId: "", statusCode: 0 }));
    read(key, controller.signal).then(payload => {
      if (active) setResource({ status: "ready", data: payload, error: "", traceId: payload.trace_id, statusCode: 200 });
    }).catch((error: unknown) => {
      if (!active || controller.signal.aborted) return;
      const statusCode = error instanceof LaunchplaneApiError ? error.statusCode : 503;
      setResource(current => ({ status: "error", data: [401, 403].includes(statusCode) ? null : current.data,
        error: error instanceof Error ? error.message : "Recorded evidence is unavailable.",
        traceId: error instanceof LaunchplaneApiError ? error.traceId : "", statusCode }));
    });
    return () => { active = false; controller.abort(); };
  }, [enabled, key, read, refreshToken, retry]);
  return { resource, refresh };
}

export function PreviewInventory({ product, refreshToken }: { product: ProductSiteOverview; refreshToken: number }) {
  const [selectedId, setSelectedId] = useState("");
  const preview = product.preview;
  const records = preview.records ?? [];
  const selected = records.find(record => record.preview_id === selectedId);
  const reconcile = useReadEvidence(product.product, Boolean(selected), refreshToken, readProductReconcileRequests);
  if (!preview.enabled) return null;
  return <section className="preview-records" aria-labelledby="preview-records-title">
    <p className="eyebrow">Recorded previews</p>
    <h2 id="preview-records-title">Inspect individual previews</h2>
    <p>Identity and lifecycle records do not prove current provider presence. Select a change to read its history.</p>
    {preview.records_status === "authorization_denied" ? <p role="status">Individual preview reads need the existing preview.read grant for this context. Product access alone does not supply it.</p>
      : preview.records_status !== "available" ? <p role="status">Individual preview inventory is unavailable in this response.</p>
      : !records.length ? <p role="status">No retained preview identities were returned. This is not verified provider absence.</p>
      : <ul className="preview-record-list">
        {records.map(record => <li key={record.preview_id}>
          <div className="preview-record-header">
          <button type="button" className="preview-record-selector" aria-expanded={record.preview_id === selectedId}
            aria-controls={record.preview_id === selectedId ? "selected-preview-evidence" : undefined}
            onClick={() => setSelectedId(value => value === record.preview_id ? "" : record.preview_id)}>
            <strong>Change #{record.change_number}</strong>
            <span>Recorded {record.recorded_state}</span><small>Updated {formatTime(record.updated_at)}</small>
          </button>
          {safeExternalUrl(record.change_url) ? <a href={safeExternalUrl(record.change_url)!.href} target="_blank" rel="noreferrer">Open change #{record.change_number}</a> : null}
          </div>
          {record.preview_id === selectedId ? <PreviewDetails key={record.preview_id} product={product}
            previewId={record.preview_id} changeNumber={record.change_number} refreshToken={refreshToken} reconcile={reconcile} /> : null}
        </li>)}
      </ul>}
    {preview.records_truncated ? <p role="status">Only part of the recorded inventory is displayed; the summary count includes the remaining identities.</p> : null}
  </section>;
}

function PreviewDetails({ product, previewId, changeNumber, refreshToken, reconcile }: {
  product: ProductSiteOverview; previewId: string; changeNumber: number; refreshToken: number;
  reconcile: ReturnType<typeof useReadEvidence<Awaited<ReturnType<typeof readProductReconcileRequests>>>>;
}) {
  const history = useReadEvidence(previewId, true, refreshToken, readPreviewHistory);
  const data = history.resource.data;
  const matches = data?.preview.preview_id === previewId && data.preview.context === product.preview.context;
  const generations = matches ? data.generations.filter(generation => generation.preview_id === previewId) : [];
  const latest = matches ? generations.find(generation => generation.generation_id === data.preview.latest_generation_id) : undefined;
  const serving = matches ? generations.find(generation => generation.generation_id === data.preview.serving_generation_id) : undefined;
  const requests = reconcile.resource.data?.product === product.product ? reconcile.resource.data.requests : [];
  const request = requests.find(value => value.target_kind === "preview" && value.pull_request_number === changeNumber);
  const previewUrl = matches ? safeExternalUrl(data.preview.canonical_url) : null;
  return <div className="preview-record-evidence" id="selected-preview-evidence">
    <button type="button" className="button button-secondary" disabled={history.resource.status === "loading" || reconcile.resource.status === "loading"}
      onClick={() => { history.refresh(); reconcile.refresh(); }}>Refresh preview evidence</button>
    <ReadStatus resource={history.resource} deniedAction="preview.read" />
    {data && !matches ? <p role="alert">History does not match the selected preview and context.</p> : null}
    {matches ? <>
      <p><strong>Provider presence unknown</strong> · These are recorded outcomes, not a current provider inspection.</p>
      <dl className="preview-evidence-facts">
        <div><dt>Preview identity</dt><dd>{data.preview.preview_id}</dd></div>
        <div><dt>Recorded lifecycle</dt><dd>{data.preview.state} · {formatTime(data.preview.updated_at)}</dd></div>
        <div><dt>Serving generation reference</dt><dd>{data.preview.serving_generation_id || "Not recorded"}</dd></div>
        <div><dt>Latest generation reference</dt><dd>{data.preview.latest_generation_id || "Not recorded"}</dd></div>
      </dl>
      <div className="preview-evidence-links">
        {previewUrl ? <a href={previewUrl.href} target="_blank" rel="noreferrer">Open recorded preview URL</a> : null}
      </div>
      <GenerationEvidence label="Latest recorded generation" generation={latest} />
      <GenerationEvidence label="Recorded serving generation" generation={serving} />
    </> : null}
    <ReadStatus resource={reconcile.resource} deniedAction="product_profile.read" />
    {request ? <ReconcileEvidence request={request} /> : reconcile.resource.status === "ready" ?
      <p>No matching reconciliation was returned. Cleanup outcome remains unknown.</p> : null}
    <p>Next safe action: inspect lifecycle activity and the recorded failure before choosing an existing supported reviewed recovery operation.</p>
    <AppLink to={productActivityPath(product.product)}>Inspect lifecycle activity</AppLink>
    <p>No refresh, destroy or reconciliation mutation control is enabled here.</p>
  </div>;
}

function GenerationEvidence({ label, generation }: { label: string; generation?: PreviewGenerationRecord }) {
  return <section className="preview-generation" aria-label={label}>
    <h3>{label}</h3>
    {generation ? <dl className="preview-evidence-facts">
      <div><dt>Generation</dt><dd>{generation.sequence} · {generation.state}</dd></div>
      <div><dt>Change head</dt><dd>{generation.anchor_summary.head_sha}</dd></div>
      <div><dt>Build artifact</dt><dd>{generation.artifact_id || "Not recorded"}</dd></div>
      <div><dt>Recorded health</dt><dd>{generation.overall_health_status}</dd></div>
      <div><dt>Recorded deployment / verification</dt><dd>{generation.deploy_status} / {generation.verify_status}</dd></div>
      <div><dt>Runtime identity declaration</dt><dd>{generation.runtime_identity ? `${generation.runtime_identity.source_git_ref} · ${generation.runtime_identity.artifact_id}` : "Not recorded"}</dd>
        <small>A declaration is not observed runtime identity verification.</small></div>
      <div><dt>Recorded time</dt><dd>{formatTime(generation.finished_at || generation.ready_at || generation.requested_at)}</dd></div>
    </dl> : <p>Generation evidence is missing. Another historical generation is not substituted for this reference.</p>}
  </section>;
}

function ReconcileEvidence({ request }: { request: ProductReconcileRequestView }) {
  const action = typeof request.last_plan.action === "string" ? request.last_plan.action : "unknown";
  const reason = typeof request.last_plan.reason === "string" ? request.last_plan.reason : "Not recorded";
  const outcome = typeof request.last_plan.preview_result_status === "string" ? request.last_plan.preview_result_status : request.state;
  return <section className="preview-reconcile" aria-label="Recorded reconciliation">
    <h3>{action === "destroy" ? "Cleanup reconciliation" : "Preview reconciliation"}</h3>
    <p>{request.last_plan.held === true ? "Retries held" : `Recorded outcome: ${outcome}`} · {formatTime(request.updated_at)}</p>
    <p>Action: {action} · Reason: {reason}</p>
    {request.last_error ? <p>Recorded failure: {request.last_error}</p> : null}
    <p>Request state: {request.state}. A done request does not prove a destroyed preview or absent provider resource.</p>
  </section>;
}

function ReadStatus({ resource, deniedAction }: { resource: ResourceState<unknown>; deniedAction: string }) {
  if (resource.status === "loading") return <p role="status">Reading recorded evidence…</p>;
  if (resource.status !== "error") return null;
  return <div role="alert">
    <p>{[401, 403].includes(resource.statusCode) ? `Read denied: ${deniedAction}. Existing standing read authority is missing.` : resource.error}</p>
    {resource.data ? <p>Showing the last recorded response; the fresh read failed.</p> : null}
    {resource.traceId ? <small>Trace: {resource.traceId}</small> : null}
  </div>;
}
