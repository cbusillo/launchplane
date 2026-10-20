import { useEffect, useState } from "react";
import { LaunchplaneApiError, readDiscussionTopics } from "./api";
import type { DiscussionTopicsResponse } from "./generated/openapi.ts";
import { formatTime } from "./format";
import { AppLink, productPath } from "./router";
import { safeExternalUrl } from "./url";

export function ProductTopicsRoute({ product, admin }: { product: string; admin: boolean }) {
  const [response, setResponse] = useState<DiscussionTopicsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setResponse(null);
    setLoading(true);
    setError("");
    void readDiscussionTopics(product, controller.signal).then(payload => {
      if (!controller.signal.aborted) setResponse(payload);
    }).catch((failure: unknown) => {
      if (controller.signal.aborted) return;
      setError(failure instanceof LaunchplaneApiError && (failure.statusCode === 403 || failure.statusCode === 404)
        ? "These topics are not available to you. Open the link for your product."
        : "Topics could not be loaded. Try refreshing in a moment.");
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [product, refresh]);

  return (
    <section className="owner-review-page topics-page">
      <div className="owner-review-intro">
        <p className="eyebrow">Topics to discuss</p>
        <h1 data-route-heading tabIndex={-1}>{response?.display_name ?? "Topics to discuss"}</h1>
        <p>Open questions for our next conversation. Follow a discussion link for more context.</p>
      </div>
      <div className="topics-toolbar">
        {response ? <p role="status">{response.topics.length} open {response.topics.length === 1 ? "topic" : "topics"}</p> : null}
        <button className="button" type="button" disabled={loading} onClick={() => setRefresh(value => value + 1)}>Refresh topics</button>
        {admin ? <AppLink to={productPath(product)}>Product overview</AppLink> : null}
      </div>
      {loading ? <p className="owner-review-state" role="status">Loading topics…</p> : error ? (
        <p className="owner-review-alert" role="alert">{error}</p>
      ) : response?.topics.length === 0 ? (
        <p className="owner-review-state">No open topics to discuss.</p>
      ) : response ? (
        <ul className="topics-list" aria-label="Open topics">
          {response.topics.map(topic => {
            const issueUrl = safeExternalUrl(topic.issue_url);
            return (
              <li className="owner-review-card" key={topic.topic_id}>
                <h2>{topic.title}</h2>
                <p className="topics-added">Added <time dateTime={topic.added_at}>{formatTime(topic.added_at)}</time></p>
                {issueUrl ? <a href={issueUrl.href} aria-label={`View discussion: ${topic.title} (opens in a new tab)`} target="_blank" rel="noopener noreferrer">View discussion <span aria-hidden="true">↗</span></a> : null}
              </li>
            );
          })}
        </ul>
      ) : null}
    </section>
  );
}
