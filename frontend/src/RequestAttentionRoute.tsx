import { Inbox } from "lucide-react";
import { useState } from "react";

import { LaunchplaneApiError, readRequestAttention } from "./api";
import { EngineeringResourceControls, EngineeringResourceGate, EngineeringRouteFrame } from "./EngineeringRouteUi";
import { useEngineeringResource } from "./engineering-resource";
import { formatTime } from "./format";
import type { AttentionSource, WaitingRequest } from "./generated/openapi.ts";
import { safeExternalUrl } from "./url";

interface AttentionSnapshot {
  requests: WaitingRequest[];
  gaps: string[];
  sources: AttentionSource[];
  observedAt: string;
}

async function loadRequests(signal: AbortSignal): Promise<AttentionSnapshot> {
  const inventory = await readRequestAttention(undefined, 1, signal);
  const requests = new Map<string, WaitingRequest>();
  const gaps: string[] = [];
  const queue = [...inventory.sources];
  let denied = false;
  async function readSources() {
    while (queue.length && !signal.aborted && !denied) {
      const source = queue.shift()!;
      if (!source.available) {
        gaps.push(`${source.repository}: source is not tracked.`);
        continue;
      }
      try {
        let page: number | null = 1;
        const pages = new Set<number>();
        while (page !== null && !signal.aborted && !denied) {
          if (pages.has(page)) {
            gaps.push(`${source.repository}: the source repeated a page.`);
            break;
          }
          pages.add(page);
          const response = await readRequestAttention(source.repository, page, signal);
          for (const request of response.requests) requests.set(request.source_id, request);
          if (response.truncated) gaps.push(`${source.repository}: more requests remain beyond the page limit.`);
          page = response.next_page;
        }
      } catch (failure) {
        if (signal.aborted) throw failure;
        if (failure instanceof LaunchplaneApiError && [401, 403].includes(failure.statusCode)) {
          denied = true;
          throw failure;
        }
        gaps.push(`${source.repository}: requests could not be read.`);
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(4, queue.length) }, () => readSources()));
  return {
    requests: [...requests.values()].sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at)),
    gaps: gaps.sort(),
    sources: inventory.sources,
    observedAt: inventory.observed_at,
  };
}

export function RequestAttentionRoute() {
  const resource = useEngineeringResource(loadRequests, "request-attention");
  const snapshot = resource.state.data;
  const [product, setProduct] = useState("");
  const [supervisorOnly, setSupervisorOnly] = useState(false);

  const products = [...new Set(snapshot?.sources.flatMap(source => source.products) ?? [])].sort();
  const requests = snapshot?.requests.filter(request =>
    (!product || request.products.includes(product)) && (!supervisorOnly || !request.owning_agent)) ?? [];
  const unowned = snapshot?.requests.filter(request => !request.owning_agent).length ?? 0;
  return (
    <EngineeringRouteFrame
      icon={Inbox}
      title="Waiting requests"
      description="Requests stay here while their original work is open. A reply or acknowledgement does not clear them."
      view="request-attention"
      actions={<EngineeringResourceControls state={resource.state} refresh={resource.refresh} cancel={resource.cancel} refreshLabel="Refresh requests" />}
    >
      <EngineeringResourceGate noun="waiting requests" state={resource.state} refresh={resource.refresh}>{snapshot => <>
        <p>Source coverage: {snapshot.sources.length} tracked or product-linked repositories. Sources outside this inventory are not covered.</p>
        <p>Read at {formatTime(snapshot.observedAt)}. Ages are measured at the source read.</p>
        {snapshot.gaps.length ? <div role="alert"><strong>Coverage is incomplete.</strong><ul>{snapshot.gaps.map(gap => <li key={gap}>{gap}</li>)}</ul></div> : null}
        <div className="topics-toolbar">
          <label>Product <select aria-label="Filter requests by product" value={product} onChange={event => setProduct(event.target.value)}>
            <option value="">All products and repositories</option>
            {products.map(name => <option key={name} value={name}>{name}</option>)}
          </select></label>
          <label><input type="checkbox" checked={supervisorOnly} onChange={event => setSupervisorOnly(event.target.checked)} /> Supervisor ({unowned} unowned)</label>
          <p role="status">{requests.length} waiting {requests.length === 1 ? "request" : "requests"}</p>
        </div>
        {!requests.length ? <p>{snapshot.gaps.length ? "No requests are visible in the sources read successfully." : "No waiting requests in the selected sources."}</p> :
          <ul className="topics-list" aria-label="Waiting requests">{requests.map(request => {
            const source = safeExternalUrl(request.source_url);
            return <li className="owner-review-card" key={request.source_id}>
              <h2>{source ? <a href={source.href} target="_blank" rel="noopener noreferrer">{request.title} <span aria-hidden="true">↗</span></a> : request.title}</h2>
              <p>{request.products.join(", ") || "Product not recorded"} · {request.repository} · {request.source_kind === "change" ? "Change" : "Request"}</p>
              <dl className="attention-details">
                <dt>Requester</dt><dd>{request.requester}</dd>
                <dt>Owning agent</dt><dd>{request.owning_agent ?? "Supervisor · needs an agent"}</dd>
                <dt>Waiting</dt><dd>{request.age_seconds < 3600 ? "Less than an hour" : request.age_seconds < 86400 ? `${Math.floor(request.age_seconds / 3600)} hours` : `${Math.floor(request.age_seconds / 86400)} days`} · <time dateTime={request.created_at}>{formatTime(request.created_at)}</time></dd>
                <dt>Next action</dt><dd>{request.next_action}</dd>
                <dt>Next actor</dt><dd>{request.next_actor}</dd>
              </dl>
            </li>;
          })}</ul>}
      </>}</EngineeringResourceGate>
    </EngineeringRouteFrame>
  );
}
