import { useEffect, useState } from "react";
import { readReleaseReview, writeReleaseReviewDecision } from "./api";
import { loadDevFixtures, type DevFixtureMode } from "./dev-fixture-loader";
import type { ReleaseReviewDecisionEnvelope, ReleaseReviewResponse } from "./generated/openapi.ts";
import { groupReleaseItems, untestedReason } from "./release-review-model";
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
  const grouped = checklist ? groupReleaseItems(checklist.items) : null;
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
      <p className="owner-review-state">{response.viewer_is_owner ? "Reviewing as the Client" : response.can_override ? "Reviewing as an admin" : "Viewing this release · read only"}</p>
      {!response.owner_github_login ? <p role="alert">No Client set for this product. Ask an admin to name one.</p> : null}
      {testingUrl ? <a className="button button-primary owner-review-preview" href={testingUrl.toString()} target="_blank" rel="noreferrer">Open the testing site</a> : null}
      {latestDecision ? <section className="owner-review-latest" aria-label="Latest release decision">
        <h3>{latestDecision.decision === "overridden" ? "Admin approval override recorded" : latestDecision.decision === "accepted" ? "Client approval recorded" : "Changes requested"}</h3>
        <p>Recorded by {latestDecision.actor_github_login} for this proposed version.</p>
        {latestDecision.reason ? <blockquote>{latestDecision.reason}</blockquote> : null}
        {!latestDecision.release_issue_url ? <p role="alert" className="owner-review-alert">Decision saved, but its release record has not been published. Approval cannot be used for deployment yet. Retry the same decision to publish its record.</p> : null}
      </section> : null}
      {checklist && grouped ? <>
        <h3>What to test</h3>
        {grouped.checks.length ? <ol className="release-review-checklist">{grouped.checks.map(check => <li key={check.items[0].pull_request_number}>
          <p className="release-review-notes">{check.notes || "Test notes are missing for this change."}</p>
          <ul className="release-review-changes">{check.items.map(item => <li key={item.pull_request_number}>
            {item.title}
            {item.already_reviewed ? <span>{`${response.viewer_is_owner ? "You" : "The Client"} accepted this change in its preview. Check it again as part of this release.`}</span> : null}
          </li>)}</ul>
        </li>)}</ol> : <p>{checklist.items.length ? "Nothing in this release needs you to test it." : "No merged pull request changes between these versions."}</p>}
        {grouped.nothingToTest.length ? <details className="release-review-untested">
          <summary>{grouped.nothingToTest.length === 1 ? "1 change needs nothing from you" : `${grouped.nothingToTest.length} changes need nothing from you`}</summary>
          <ul>{grouped.nothingToTest.map(item => <li key={item.pull_request_number}>
            {item.title}
            {untestedReason(item.owner_test_notes) ? <span>{untestedReason(item.owner_test_notes)}</span> : null}
          </li>)}</ul>
        </details> : null}
      </> : null}
      {response.review.blockers.length ? <ul>{response.review.blockers.map(blocker => <li key={blocker}>{blocker}</li>)}</ul> : null}
      {response.review.unavailable_reason ? <p className="owner-review-state">Reason code <code>{response.review.unavailable_reason}</code> · Trace ID <code>{response.trace_id}</code></p> : null}
      {checklist && response.viewer_is_owner ? <section className="owner-review-action" aria-label="Client release decision">
        <h3>Your release decision</h3>
        <p><strong>Accept release</strong> records your approval for the proposed version to become production. Deployment happens later.</p>
        <p><strong>Request changes</strong> records your feedback and replaces any earlier approval or admin override for this release.</p>
        <label><span>What should change? (needed only when you request changes)</span><textarea value={reason} maxLength={4000} disabled={busy} onChange={event => setReason(event.target.value)} /></label>
        <div className="owner-review-action-buttons">
          <button className="button button-primary" type="button" disabled={busy || incomplete} onClick={() => void decide("accepted", reason)}>Accept release</button>
          <button className="button" type="button" disabled={busy || !reason.trim()} onClick={() => void decide("changes_requested", reason)}>Request changes</button>
        </div>
      </section> : null}
      {checklist && response.can_override ? <section className="owner-review-action release-review-override" aria-label="Admin Approval Override">
        <h3>Admin Approval Override</h3>
        <p>This records approval under your admin identity for a later production deployment without Client acceptance. It replaces any earlier request for changes. Explain why you are overriding the Client review.</p>
        <label><span>Justification for approving this release by override</span><textarea value={overrideReason} maxLength={4000} disabled={busy} onChange={event => setOverrideReason(event.target.value)} /></label>
        <div className="owner-review-action-buttons">
          <button className="button" type="button" disabled={busy || !overrideReason.trim()} onClick={() => void decide("overridden", overrideReason)}>Record admin approval override</button>
        </div>
      </section> : null}
      {checklist && (response.viewer_is_owner || response.can_override) ? <p className="owner-review-state">Each new decision is saved and published as a separate release record. Recording a decision does not deploy anything. Production deployment is a separate action and still requires the release and backup checks.</p> : null}
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
