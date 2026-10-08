import { useEffect, useState } from "react";
import { readReleaseReview, writeReleaseReviewDecision } from "./api";
import { loadDevFixtures, type DevFixtureMode } from "./dev-fixture-loader";
import type { ClientReleaseRunView, ReleaseReviewDecisionEnvelope, ReleaseReviewResponse, ReleaseReviewItem } from "./generated/openapi.ts";
import { groupReleaseItems, untestedReason } from "./release-review-model";
import { safeExternalUrl } from "./url";

const RELEASE_STEP_LABELS: Record<ClientReleaseRunView["steps"][number]["kind"], string> = {
  backup: "Verified backup",
  promote: "Put this version live",
  rollback: "Rollback drill: return to the current version",
  recovery: "Automatic recovery: restore the previous passing version",
};
const RELEASE_STEP_STATUS: Record<ClientReleaseRunView["steps"][number]["status"], string> = {
  not_started: "Not started",
  pending: "Queued",
  running: "Running",
  pass: "Done",
  fail: "Failed",
  cancelled: "Cancelled",
  reconciliation_required: "Stopped for an admin",
};
const RELEASE_RUN_STATE: Record<ClientReleaseRunView["state"], string> = {
  waiting: "Starting",
  running: "In progress",
  passed: "Live",
  stopped: "Stopped",
};

function ReleaseRunProgress({ run }: { run: ClientReleaseRunView }) {
  return <section className="owner-review-latest" aria-label="Release progress">
    <h3>Release progress: {RELEASE_RUN_STATE[run.state]}</h3>
    {run.blocked_reason ? <p role="status">{run.blocked_reason}</p> : null}
    <ol>{run.steps.map(step => <li key={step.step}>
      {RELEASE_STEP_LABELS[step.kind]}: {RELEASE_STEP_STATUS[step.status]}
      {step.failure ? <div>
        <p>{step.failure.reason} (<code>{step.failure.code}</code>)</p>
        <details><summary>Failure details</summary>
          <p>Record: <code>{step.failure.record_id}</code></p>
          <p>Operation: <code>{step.operation_id}</code></p>
          <p>Trace: {step.failure.trace_id ? <code>{step.failure.trace_id}</code> : "Not recorded"}</p>
        </details>
      </div> : null}
    </li>)}</ol>
  </section>;
}

function ReleaseItems({ items, viewerIsOwner, emptyMessage = "No merged pull request changes between these versions." }: { items: readonly ReleaseReviewItem[]; viewerIsOwner: boolean; emptyMessage?: string }) {
  const grouped = groupReleaseItems(items);
  return <>
        {grouped.checks.length ? <ol className="release-review-checklist">{grouped.checks.map(check => <li key={check.items[0].url}>
          <p className="release-review-notes">{check.notes || "Test notes are missing for this change."}</p>
          <ul className="release-review-changes">{check.items.map(item => <li key={item.url}>
            {item.title}
            {item.already_reviewed ? <span>{`${viewerIsOwner ? "You" : "The Client"} accepted this change in its preview. Check it again as part of this release.`}</span> : null}
          </li>)}</ul>
        </li>)}</ol> : <p>{items.length ? "No changes from this repository need you to test them." : emptyMessage}</p>}
        {grouped.nothingToTest.length ? <details className="release-review-untested">
          <summary>{grouped.nothingToTest.length === 1 ? "1 change needs nothing from you" : `${grouped.nothingToTest.length} changes need nothing from you`}</summary>
          <ul>{grouped.nothingToTest.map(item => <li key={item.url}>
            {item.title}
            {untestedReason(item.owner_test_notes) ? <span>{untestedReason(item.owner_test_notes)}</span> : null}
          </li>)}</ul>
        </details> : null}
  </>;
}

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
  // Whether Accept starts the release, said before the button.
  const releaseStarts = !!response && response.release_on_acceptance !== "held";
  const liveSite = response ? safeExternalUrl(response.live_site_url) : null;
  const liveSiteName = response ? `${response.display_name}${liveSite ? ` (${liveSite.host})` : ""}` : "";
  const incomplete = !checklist || !testingUrl || response?.review.checklist_complete !== true;
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
        {latestDecision.decision === "accepted" && latestDecision.release_start ? <p>This acceptance started the release.</p> : null}
        {!latestDecision.release_issue_url ? <p role="alert" className="owner-review-alert">Decision saved, but its release record has not been published. Approval cannot be used for deployment yet. Retry the same decision to publish its record.</p> : null}
      </section> : null}
      {response.release_run ? <ReleaseRunProgress run={response.release_run} /> : null}
      {checklist ? <>
        <h3>What to test</h3>
        <ReleaseItems items={checklist.items} viewerIsOwner={response.viewer_is_owner} emptyMessage={(checklist.shared_sources ?? []).length ? "No website-repository changes. Review the shared components below." : undefined} />
        {(checklist.shared_sources ?? []).map(source => <section key={source.repository} aria-label={`Shared website components from ${source.repository}`}>
          <h4>Shared website components</h4>
          <p className="owner-review-state">{source.repository}</p>
          <ReleaseItems items={source.items} viewerIsOwner={response.viewer_is_owner} emptyMessage={source.untracked_commits.length ? "Shared changes have no merged pull request coverage. See the blockers below." : undefined} />
        </section>)}
      </> : null}
      {response.review.blockers.length ? <ul>{response.review.blockers.map(blocker => <li key={blocker}>{blocker}</li>)}</ul> : null}
      {response.review.unavailable_reason ? <p className="owner-review-state">Reason code <code>{response.review.unavailable_reason}</code> · Trace ID <code>{response.trace_id}</code></p> : null}
      {checklist && response.viewer_is_owner ? <section className="owner-review-action" aria-label="Client release decision">
        <h3>Your release decision</h3>
        {releaseStarts
          ? <p className="owner-review-golive"><strong>Accepting puts this version on the live site, {liveSiteName}.</strong> Launchplane takes a verified backup, puts it live, checks it, and rolls back by itself if the checks fail.{response.release_on_acceptance === "promote_with_rollback_drill" ? " This first time, it also rolls back once and puts the same version live again, to prove rollback works." : ""}</p>
          : <p><strong>Releases are on hold for {response.display_name}.</strong> Accepting records your approval; nothing goes live until an admin releases the hold.</p>}
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
      {checklist && (response.viewer_is_owner || response.can_override) ? <p className="owner-review-state">Each new decision is saved and published as a separate release record. {releaseStarts ? "Only the Client's acceptance starts the release; an admin override does not, and an admin can hold releases." : "Recording a decision does not deploy anything."} Production deployment still requires the release and backup checks.</p> : null}
      {recorded ? <p role="status" className="owner-review-success">{latestDecision?.release_start ? "Release accepted. Launchplane is starting it; this page shows its progress." : "Decision recorded. Nothing has been deployed."}</p> : null}
      {checklist ? <details className="release-review-technical">
        <summary>Technical details</summary>
        <dl>
          <div><dt>Current production version:</dt><dd><code>{checklist.production.source_commit}</code></dd></div>
          <div><dt>Proposed production version:</dt><dd><code>{checklist.candidate.source_commit}</code><span>Currently in testing</span></dd></div>
          {(checklist.shared_sources ?? []).map(source => <div key={source.repository}>
            <dt>Shared components from {source.repository}</dt>
            <dd><span>Production</span><code>{source.production_commit}</code><span>Currently in testing</span><code>{source.candidate_commit}</code></dd>
          </div>)}
        </dl>
      </details> : null}
    </article> : null}
  </section>;
}
