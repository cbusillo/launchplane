import { useEffect, useState } from "react";
import { LaunchplaneApiError, readProviderHoldAttention } from "./api";
import type { DevFixtureMode } from "./dev-fixture-loader";
import { formatTime } from "./format";
import type { ProviderHoldAttentionPage } from "./generated/openapi.ts";
import { providerAttentionQualifier } from "./provider-hold-attention";
import { emptyResource, type ResourceState } from "./resource";
import { useEvidenceRefresh } from "./use-evidence-refresh";

export function useProviderHoldAttention(admin: boolean, fixtureMode: DevFixtureMode, refreshToken: number) {
  const enabled = admin && !fixtureMode;
  const [resource, setResource] = useState<ResourceState<ProviderHoldAttentionPage>>(emptyResource());
  const [request, setRequest] = useState<{ cursor: string | null; token: number; refreshToken: number }>({ cursor: null, token: 0, refreshToken });
  const cursor = request.refreshToken === refreshToken ? request.cursor : null;
  const refresh = () => setRequest(current => ({ cursor: null, token: current.token + 1, refreshToken }));
  useEvidenceRefresh("service-operation-attention", enabled, resource, refresh, resource.data ? [resource.data.stale_after] : []);
  useEffect(() => {
    if (!enabled) { setResource(emptyResource()); return; }
    let active = true;
    const controller = new AbortController();
    setResource(current => ({ ...current, status: "loading", error: "", statusCode: 0 }));
    readProviderHoldAttention({ limit: 50, cursor }, controller.signal).then(payload => {
      if (active) setResource({ status: "ready", data: payload.attention, error: "", traceId: payload.trace_id, statusCode: 200 });
    }).catch((error: unknown) => {
      if (!active || controller.signal.aborted) return;
      const statusCode = error instanceof LaunchplaneApiError ? error.statusCode : 503;
      setResource(current => ({ status: "error", data: [401, 403].includes(statusCode) ? null : current.data,
        error: "Service operation attention could not be read.", statusCode,
        traceId: error instanceof LaunchplaneApiError ? error.traceId : "" }));
    });
    return () => { active = false; controller.abort(); };
  }, [enabled, refreshToken, cursor, request.token]);
  return {
    resource: enabled ? resource : emptyResource<ProviderHoldAttentionPage>(),
    refresh,
    next: () => {
      if (resource.status === "ready" && resource.data?.next_cursor) setRequest(current => ({
        cursor: resource.data!.next_cursor, token: current.token + 1, refreshToken,
      }));
    },
  };
}

export function ProviderHoldAttention({ admin, fixtureMode, resource, onNext, onRefresh }: {
  admin: boolean; fixtureMode: DevFixtureMode; resource: ResourceState<ProviderHoldAttentionPage>;
  onNext: () => void; onRefresh: () => void;
}) {
  const page = resource.data;
  const stale = resource.status !== "ready" || !page || !Number.isFinite(Date.parse(page.stale_after)) || Date.now() >= Date.parse(page.stale_after);
  return <section className="product-incident-overview provider-hold-attention" aria-label="Service operation attention" aria-busy={resource.status === "loading"}>
    <div className="product-incident-overview-head"><div>
      <p className="eyebrow">Retained work</p><h2>Service operation attention</h2>
      <p>{fixtureMode ? "Demo mode. Service operation attention is not loaded." : !admin ? "Service operation attention is unavailable for this session. Lane evidence covers authorized products only." : providerAttentionQualifier(resource)}</p>
      {page && stale ? <p role="status">Last received evidence is stale. Refresh before drawing a conclusion.</p> : null}
    </div></div>
    {admin && !fixtureMode && page ? <>
      <p className="muted">Observed {formatTime(page.observed_at)}. Showing this page only. Original scopes remain visible after a product or lane is removed.</p>
      <ul className="provider-hold-list">
        {page.items.map((item, index) => <li key={item.operation_reference ?? `${page.observed_at}:${index}`}>
          <strong>{item.original_scope ? `${item.original_scope.product} · ${item.original_scope.instance}` : "Original ownership unknown"}</strong>
          <span>{item.original_scope ? `Original context: ${item.original_scope.context}. ` : ""}{item.attribution === "corrupt" ? "Original ownership evidence is corrupt. " : item.attribution === "legacy" ? "Historical attribution; operation reference unavailable. " : ""}{item.state === "running" ? "Running operation" : "Reconciliation required"}</span>
          <small>Updated {formatTime(item.updated_at)} · {item.phase === "effect_recorded" ? "Effect recorded" : "Effect phase not recorded"}</small>
          {item.operation_reference ? <details><summary>Operation reference</summary><code>{item.operation_reference}</code></details> : null}
        </li>)}
      </ul>
      {!page.items.length && page.completeness !== "single_page" ? <p>No operations returned on this continuation page. Earlier pages and concurrent changes remain outside this read.</p> : null}
    </> : null}
    {admin && !fixtureMode ? <div className="provider-hold-controls">
      <button className="secondary-button" disabled={resource.status === "loading"} onClick={onRefresh}>Refresh operation attention</button>
      {page?.has_more && page.next_cursor ? <button className="secondary-button" disabled={resource.status !== "ready"} onClick={onNext}>Next operation page</button> : null}
      {page?.completeness === "partial" ? <span>Partial evidence. Refresh returns to the first page.</span> : null}
      {resource.traceId && resource.status === "error" ? <small>Trace: {resource.traceId}</small> : null}
    </div> : null}
  </section>;
}
