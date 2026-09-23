import { useEffect, useState } from "react";
import { readReleaseReview, writeReleaseReviewDecision } from "./api";
import { loadDevFixtures, type DevFixtureMode } from "./dev-fixture-loader";
import type { ReleaseReviewDecisionEnvelope, ReleaseReviewResponse } from "./generated/openapi.ts";
import { safeExternalUrl } from "./url";

export function OwnerReleaseReviewRoute({ product, fixtureMode }: { product: string; fixtureMode: DevFixtureMode }) {
  const [response, setResponse] = useState<ReleaseReviewResponse | null>(null);
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [recorded, setRecorded] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setResponse(null);
    setError("");
    setReason("");
    setRecorded(false);
    const request = fixtureMode
      ? loadDevFixtures().then((fixtures) => fixtures.releaseReviewForFixture())
      : readReleaseReview(product, controller.signal);
    void request.then((value) => {
      if (!controller.signal.aborted) setResponse(value);
    }).catch(() => {
      if (!controller.signal.aborted) setError("This release review could not be loaded. Check the link and try again.");
    });
    return () => controller.abort();
  }, [product, fixtureMode]);

  async function decide(decision: ReleaseReviewDecisionEnvelope["decision"]) {
    if (!response?.review.checklist || busy) return;
    setBusy(true);
    setError("");
    setRecorded(false);
    try {
      if (fixtureMode) {
        setResponse({ ...response, review: { ...response.review, approved: decision !== "changes_requested", blockers: decision === "changes_requested" ? [reason] : [] } });
      } else {
        setResponse(await writeReleaseReviewDecision({ product, checklist_digest: response.review.checklist_digest, decision, reason }));
      }
      setRecorded(true);
    } catch {
      setError("The decision was not recorded. The release may have changed; reload this page before deciding again.");
    } finally {
      setBusy(false);
    }
  }

  const checklist = response?.review.checklist;
  const testingUrl = checklist ? safeExternalUrl(checklist.testing_url) : null;
  const incomplete = !checklist || !checklist.owner_github_id || !testingUrl || checklist.untracked_commits.length > 0 || checklist.items.some((item) => !item.owner_test_notes.trim());
  return <section className="owner-review-page">
    <div className="owner-review-intro">
      <p className="eyebrow">Release decision</p>
      <h1 data-route-heading tabIndex={-1}>Review this release</h1>
      <p>Open the testing site and work through the checklist. Your decision is saved for this version; it does not publish the site.</p>
    </div>
    {error ? <p role="alert" className="owner-review-alert">{error}</p> : null}
    {!response && !error ? <p role="status">Loading the release checklist…</p> : null}
    {response ? <article className="owner-review-card">
      <h2>{response.display_name}</h2>
      {!response.owner_github_login ? <p role="alert">No Owner set for this product. Ask the operator to name one.</p> : null}
      {testingUrl ? <a className="button button-primary" href={testingUrl.toString()} target="_blank" rel="noreferrer">Open the testing site</a> : null}
      {checklist ? <>
        <p className="owner-review-state">Production {checklist.production.source_commit.slice(0, 7)} → testing {checklist.candidate.source_commit.slice(0, 7)}</p>
        {checklist.items.length ? <ol className="release-review-checklist">{checklist.items.map((item) => <li key={item.pull_request_number}>
          <h3>{item.title}</h3>
          <p>{item.already_reviewed ? "You accepted this change in its preview. Check it again as part of this release." : "Not previously accepted in preview."}</p>
          <p className="release-review-notes">{item.owner_test_notes || "Owner test notes are missing for this change."}</p>
        </li>)}</ol> : <p>No merged pull request changes between these versions.</p>}
      </> : null}
      {response.review.blockers.length ? <ul>{response.review.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}</ul> : null}
      {response.review.latest_decision ? <p className="owner-review-state">Latest decision: {response.review.latest_decision.decision === "overridden" ? "Operator override" : response.review.latest_decision.decision === "accepted" ? "Accepted" : "Changes requested"}. {response.review.latest_decision.reason}</p> : null}
      {checklist && (response.viewer_is_owner || response.can_override) ? <section className="owner-review-action" aria-label="Release decision">
        <label><span>{response.can_override ? "Reason for changes or an operator override" : "What should change? (needed only when you request changes)"}</span><textarea value={reason} maxLength={4000} disabled={busy} onChange={(event) => setReason(event.target.value)} /></label>
        <div className="owner-review-action-buttons">
          {response.viewer_is_owner ? <><button className="button button-primary" type="button" disabled={busy || incomplete} onClick={() => void decide("accepted")}>Accept release</button><button className="button" type="button" disabled={busy || !reason.trim()} onClick={() => void decide("changes_requested")}>Request changes</button></> : null}
          {response.can_override ? <button className="button" type="button" disabled={busy || !reason.trim()} onClick={() => void decide("overridden")}>Record operator override</button> : null}
        </div>
      </section> : null}
      {recorded ? <p role="status" className="owner-review-success">Decision recorded. Nothing was published.</p> : null}
    </article> : null}
  </section>;
}
