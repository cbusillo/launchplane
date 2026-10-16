import { useEffect, useId, useState } from "react";
import { ShieldCheck } from "lucide-react";
import { decideBackupPolicyProposal, LaunchplaneApiError, readAuthSession, readBackupPolicyProposals } from "./api";
import type { BackupPolicyProposalReview, ProductEnvironmentDetail } from "./generated/openapi.ts";
import { productConfigFailureCertainty, productConfigOperationFailure } from "./product-config-operation";
import { InlineFormError, OperationNotice } from "./ProductConfigForms";
import { useBrowserOperationController } from "./use-browser-operation";

export function ProductionBackupAuthorityPanel({ detail, fixtureMode, onRefresh }: {
  detail: ProductEnvironmentDetail; fixtureMode: boolean; onRefresh: () => void;
}) {
  const id = useId();
  const [proposals, setProposals] = useState<BackupPolicyProposalReview[]>([]);
  const [admin, setAdmin] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [lastDecided, setLastDecided] = useState("");
  const selected = new URLSearchParams(window.location.search).get("backup_proposal");
  const waiting = proposals.some(item => ["approved", "executing"].includes(item.status));
  useEffect(() => {
    if (fixtureMode) return;
    const controller = new AbortController();
    void Promise.all([readAuthSession(controller.signal), readBackupPolicyProposals(detail, controller.signal)])
      .then(([session, response]) => {
        if (controller.signal.aborted) return;
        if (response.proposals.some(item => item.protection.product !== detail.product || item.protection.context !== detail.context || item.protection.instance !== detail.environment)) {
          throw new Error("The backup proposals belong to another environment.");
        }
        setAdmin(session.identity.role === "admin"); setProposals(response.proposals); setError("");
      }).catch(failure => { if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "Backup proposals could not be read."); });
    return () => controller.abort();
  }, [fixtureMode, detail.product, detail.context, detail.environment, revision]);
  useEffect(() => {
    if (!waiting) return;
    const timer = window.setInterval(() => setRevision(value => value + 1), 3000);
    return () => window.clearInterval(timer);
  }, [waiting]);
  const visible = proposals.filter(item => item.operation_id === selected || item.operation_id === lastDecided || ["planned", "approved", "executing"].includes(item.status));
  return <section id="production-backup-authority" className="product-config-panel" aria-labelledby={`${id}-title`}>
    <header className="product-config-panel-header"><ShieldCheck aria-hidden="true" /><div>
      <p className="eyebrow">Production protection</p><h2 id={`${id}-title`}>Production backup authority</h2>
      <p>Review proposed protection here. Approval records the backup policy; it does not take a backup or release the product.</p>
    </div></header>
    {error ? <InlineFormError message={error} /> : null}
    {!admin ? <p>A signed-in Director administrator must approve or decline proposals.</p> : null}
    {!visible.length && !error ? <p>No pending backup policy proposals.</p> : null}
    {selected && !proposals.some(item => item.operation_id === selected) && !error ? <p>The linked proposal is unavailable for this product. Refresh or ask for its current link.</p> : null}
    {visible.map(proposal => <BackupProposal key={proposal.operation_id} proposal={proposal} admin={admin} fixtureMode={fixtureMode} displayName={detail.display_name}
      onRecheck={() => setRevision(value => value + 1)} onChanged={next => {
        setLastDecided(next.operation_id);
        setProposals(items => items.map(item => item.operation_id === next.operation_id ? next : item));
        setRevision(value => value + 1); onRefresh();
      }} />)}
    <button className="button" type="button" disabled={fixtureMode} onClick={() => setRevision(value => value + 1)}>Refresh proposals</button>
  </section>;
}

function age(seconds: number): string {
  return seconds % 3600 === 0 ? `${seconds / 3600} hours` : `${seconds / 60} minutes`;
}

function BackupProposal({ proposal, admin, fixtureMode, displayName, onChanged, onRecheck }: {
  proposal: BackupPolicyProposalReview; admin: boolean; fixtureMode: boolean; displayName: string;
  onChanged: (next: BackupPolicyProposalReview) => void; onRecheck: () => void;
}) {
  const decisionKey = `backup-policy:${proposal.operation_id}:decision-kind`;
  const [decision, setDecision] = useState<"approve" | "decline" | null>(() => {
    try { const value = sessionStorage.getItem(decisionKey); return value === "approve" || value === "decline" ? value : null; } catch { return null; }
  });
  const operation = useBrowserOperationController({
    scope: `backup-policy:${proposal.operation_id}:decision`, execute: decideBackupPolicyProposal,
    failureCertainty: (failure, dispatched) => failure instanceof LaunchplaneApiError &&
      ((failure.statusCode === 409 && failure.code === "backup_proposal_conflict") ||
       (failure.statusCode === 404 && failure.code === "backup_proposal_missing")) ? "settled" : productConfigFailureCertainty(failure, dispatched), failureFor: productConfigOperationFailure,
  });
  const busy = ["queued", "submitting"].includes(operation.state.phase);
  const uncertain = operation.state.requiresIdempotencyContinuity;
  const protection = proposal.protection;
  async function decide(next: "approve" | "decline") {
    if (!admin || fixtureMode || busy) return;
    setDecision(next);
    try { sessionStorage.setItem(decisionKey, next); } catch { /* The server also pins the decision to this proposal. */ }
    const response = await operation.run({ operationId: proposal.operation_id, decision: next });
    if (response) onChanged(response.proposal); else onRecheck();
  }
  return <div className="product-owner-plan">
    <h3>Proposed protection for {displayName} · production</h3>
    <p>Take a snapshot of the whole production guest and an independent PBS backup before release. Both include any other workloads on that guest.</p>
    <dl className="environment-action-facts">
      <div><dt>Snapshot retention</dt><dd>{protection.retention_count} snapshots</dd></div>
      <div><dt>Snapshot freshness</dt><dd>At most {age(protection.snapshot_max_age_seconds)} old</dd></div>
      <div><dt>Independent backup freshness</dt><dd>At most {age(protection.backup_max_age_seconds)} old</dd></div>
      <div><dt>Policy revision</dt><dd>{protection.policy_revision}</dd></div>
      <div><dt>Effective from</dt><dd>{new Date(protection.effective_at).toLocaleString()}</dd></div>
      <div><dt>Review by</dt><dd>{new Date(protection.review_after).toLocaleString()}</dd></div>
    </dl>
    <ul>{protection.targets.map(target => <li key={target.destination_kind}>
      {target.destination_kind === "proxmox_guest" ? "Snapshot source" : "Independent backup destination"}: {target.change} · revision {target.target_revision}.
      {target.change === "revised" ? " This changes the backup location or its settings." : target.change === "new" ? " This adds a backup target." : " Existing target retained."}
    </li>)}</ul>
    <p>Launchplane checked this proposal and will check it again before saving.</p>
    <p role="status" aria-label="Backup authority result">{proposal.status === "executed" ? "Backup policy approved. No capture or release was started." : proposal.status === "cancelled" ? "Backup policy proposal declined." : ["approved", "executing"].includes(proposal.status) ? "Approved. Launchplane is recording the backup policy." : proposal.status === "execution_failed" ? "The policy could not be recorded. Ask for a fresh proposal after checking the recorded outcome." : "Pending your decision."}</p>
    <OperationNotice state={operation.state} label="Backup policy decision" />
    {proposal.status === "planned" ? <div className="product-config-actions">
      <button className="button button-primary" disabled={!admin || !proposal.can_approve || busy || uncertain || fixtureMode} onClick={() => void decide("approve")}>Approve</button>
      <button className="button" disabled={!admin || !proposal.can_decline || busy || uncertain || fixtureMode} onClick={() => void decide("decline")}>Decline</button>
      {uncertain ? <button className="button" disabled={!admin || busy || fixtureMode || !decision} onClick={() => decision && void decide(decision)}>Retry original decision</button> : null}
    </div> : null}
  </div>;
}
