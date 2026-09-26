import { useEffect, useState } from "react";
import { readReleaseReview, writeReleaseReviewDecision } from "./api";
import { loadDevFixtures, type DevFixtureMode } from "./dev-fixture-loader";
import type { ReleaseReviewDecisionEnvelope, ReleaseReviewResponse } from "./generated/openapi.ts";
import { safeExternalUrl } from "./url";

export function OwnerReleaseReviewRoute({ product, fixtureMode }: { product: string; fixtureMode: DevFixtureMode }) {
  const [response, setResponse] = useState<ReleaseReviewResponse | null>(null);
  const [reason, setReason] = useState("");
  const [overrideReason, setOverrideReason] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [recorded, setRecorded] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setResponse(null);
    setError("");
    setReason("");
    setOverrideReason("");
    setRecorded(false);
    const request = fixtureMode
      ? loadDevFixtures().then(fixtures => fixtures.releaseReviewForFixture(fixtureMode))
      : readReleaseReview(product, controller.signal);
    void request.then(value => {
      if (!controller.signal.aborted) setResponse(value);
    }).catch(() => {
      if (!controller.signal.aborted) setError("This release review could not be loaded. Check the link and try again.");
    });
    return () => controller.abort();
  }, [product, fixtureMode]);

  async function decide(decision: ReleaseReviewDecisionEnvelope["decision"], decisionReason: string) {
    if (!response?.review.checklist || busy) return;
    setBusy(true);
    setError("");
    setRecorded(false);
    try {
      if (fixtureMode) {
        const fixtures = await loadDevFixtures();
        setResponse(fixtures.releaseDecisionForFixture(response, decision, decisionReason));
      } else {
        setResponse(await writeReleaseReviewDecision({ product, checklist_digest: response.review.checklist_digest, decision, reason: decisionReason }));
      }
      setRecorded(true);
    } catch {
      setError("The decision was not recorded. The release may have changed; reload this page before deciding again.");
    } finally {
      setBusy(false);
    }
  }

  const checklist = response?.review.checklist;
  const latestDecision = response?.review.latest_decision;
  const testingUrl = checklist ? safeExternalUrl(checklist.testing_url) : null;
  const incomplete = !checklist || !checklist.owner_github_id || !testingUrl || checklist.untracked_commits.length > 0 || checklist.additional_changes.length > 0 || checklist.items.some(item => !item.owner_test_notes.trim());
  return <section className="owner-review-page">
    <div className="owner-review-intro">
      <p className="eyebrow">Release decision</p>
      <h1 data-route-heading tabIndex={-1}>Review this release</h1>
      <p>Review the changes proposed for production. Open the testing site and check each item below before recording your decision.</p>
    </div>
    {error ? <p role="alert" className="owner-review-alert">{error}</p> : null}
    {!response && !error ? <p role="status">Loading the release checklist…</p> : null}
    {response ? <article className="owner-review-card">
      <h2>{response.display_name}</h2>
      <p className="owner-review-state">{response.viewer_is_owner ? "Reviewing as the site Owner" : response.can_override ? "Reviewing as an operator" : "Viewing this release · read only"}</p>
      {!response.owner_github_login ? <p role="alert">No Owner set for this product. Ask the operator to name one.</p> : null}
      {testingUrl ? <a className="button button-primary owner-review-preview" href={testingUrl.toString()} target="_blank" rel="noreferrer">Open the testing site</a> : null}
      {latestDecision ? <section className="owner-review-latest" aria-label="Latest release decision">
        <h3>{latestDecision.decision === "overridden" ? "Operator override recorded" : latestDecision.decision === "accepted" ? "Owner approval recorded" : "Changes requested"}</h3>
        <p>Recorded by {latestDecision.actor_github_login} for this proposed version.</p>
        {latestDecision.reason ? <blockquote>{latestDecision.reason}</blockquote> : null}
      </section> : null}
      {checklist ? <>
        <h3>Changes to review</h3>
        {checklist.items.length ? <ol className="release-review-checklist">{checklist.items.map(item => <li key={item.pull_request_number}>
          <h3>{item.title}</h3>
          <p>{item.already_reviewed ? `${response.viewer_is_owner ? "You" : "The Owner"} accepted this change in its preview. Check it again as part of this release.` : "Not previously accepted in preview."}</p>
          <p className="release-review-notes">{item.owner_test_notes || "Owner test notes are missing for this change."}</p>
        </li>)}</ol> : <p>No merged pull request changes between these versions.</p>}
      </> : null}
      {response.review.blockers.length ? <ul>{response.review.blockers.map(blocker => <li key={blocker}>{blocker}</li>)}</ul> : null}
      {checklist && response.viewer_is_owner ? <section className="owner-review-action" aria-label="Owner release decision">
        <h3>Your release decision</h3>
        <p><strong>Accept release</strong> records your approval for the proposed version to become production. Deployment happens later.</p>
        <p><strong>Request changes</strong> records your feedback and replaces any approval you already gave for this release.</p>
        <label><span>What should change? (needed only when you request changes)</span><textarea value={reason} maxLength={4000} disabled={busy} onChange={event => setReason(event.target.value)} /></label>
        <div className="owner-review-action-buttons">
          <button className="button button-primary" type="button" disabled={busy || incomplete} onClick={() => void decide("accepted", reason)}>Accept release</button>
          <button className="button" type="button" disabled={busy || !reason.trim()} onClick={() => void decide("changes_requested", reason)}>Request changes</button>
        </div>
      </section> : null}
      {checklist && response.can_override ? <section className="owner-review-action release-review-override" aria-label="Operator override">
        <h3>Operator override</h3>
        <p>This records approval under your operator identity, allowing a later production deployment without Owner acceptance. Explain why you are overriding the Owner review.</p>
        <label><span>Reason for operator override</span><textarea value={overrideReason} maxLength={4000} disabled={busy} onChange={event => setOverrideReason(event.target.value)} /></label>
        <div className="owner-review-action-buttons">
          <button className="button" type="button" disabled={busy || !overrideReason.trim()} onClick={() => void decide("overridden", overrideReason)}>Record operator override</button>
        </div>
      </section> : null}
      {checklist && (response.viewer_is_owner || response.can_override) ? <p className="owner-review-state">Recording a decision does not deploy anything. Production deployment is a separate action and still requires the release and backup checks.</p> : null}
      {recorded ? <p role="status" className="owner-review-success">Decision recorded. Nothing has been deployed.</p> : null}
      {checklist ? <details className="release-review-technical">
        <summary>Technical details</summary>
        <dl>
          <div><dt>Current production version:</dt><dd><code>{checklist.production.source_commit}</code></dd></div>
          <div><dt>Proposed production version:</dt><dd><code>{checklist.candidate.source_commit}</code><span>Currently in testing</span></dd></div>
        </dl>
      </details> : null}
    </article> : null}
  </section>;
}
