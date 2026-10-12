import { useEffect, useId, useRef, useState } from "react";
import { ShieldCheck } from "lucide-react";
import { applyProductionBackupAuthority, LaunchplaneApiError, readAuthSession, readProductEnvironmentConfigStatus, readProductionBackupAuthority } from "./api";
import type { ProductionBackupAuthorityReadModel, ProductionBackupAuthorityWriteEnvelope, ProductionBackupAuthorityWriteResponse, ProductEnvironmentDetail } from "./generated/openapi.ts";
import { recoverBrowserOperationState } from "./browser-operation";
import { productConfigFailureCertainty, productConfigOperationFailure } from "./product-config-operation";
import { InlineFormError, OperationNotice } from "./ProductConfigForms";
import { backupProposalStorageKey, readBackupProposal } from "./production-backup-proposal";
import { useBrowserOperationController } from "./use-browser-operation";

type Review = { request: ProductionBackupAuthorityWriteEnvelope; digest: string };
function approvalFailureCertainty(error: unknown, dispatched: boolean) {
  // This conflict follows idempotency replay preflight and the atomic locked write.
  // Auth/validation refusals precede replay, so they cannot settle a lost response.
  if (error instanceof LaunchplaneApiError && error.statusCode === 409 &&
      error.code === "production_backup_authority_conflict") return "settled" as const;
  return productConfigFailureCertainty(error, dispatched);
}
export function ProductionBackupAuthorityPanel({ detail, fixtureMode, onRefresh }: {
  detail: ProductEnvironmentDetail; fixtureMode: boolean; onRefresh: () => void;
}) {
  const id = useId();
  const scope = `${detail.product}:${detail.context}:${detail.environment}:backup-authority:apply`;
  const storageKey = backupProposalStorageKey(scope);
  const [proposal, setProposal] = useState<ProductionBackupAuthorityWriteEnvelope | null>(() => {
    try { return readBackupProposal(sessionStorage.getItem(storageKey) ?? "null", detail); } catch { return null; }
  });
  const [pending, setPending] = useState<ProductionBackupAuthorityWriteEnvelope | null>(() => {
    try {
      const raw = sessionStorage.getItem(`${storageKey}:pending`);
      if (!raw || !recoverBrowserOperationState(scope).requiresIdempotencyContinuity) return null;
      const value = JSON.parse(raw) as ProductionBackupAuthorityWriteEnvelope;
      readBackupProposal(raw, detail);
      return value.mode === "apply" && /^[a-f0-9]{64}$/.test(value.reviewed_authority_digest ?? "") ? value : null;
    } catch { return null; }
  });
  const [admin, setAdmin] = useState(false);
  const [requiredActions, setRequiredActions] = useState<string[]>([]);
  const [authority, setAuthority] = useState<ProductionBackupAuthorityReadModel | null>(null);
  const [readingAuthority, setReadingAuthority] = useState(false);
  const [review, setReview] = useState<Review | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [checking, setChecking] = useState(false);
  const generation = useRef(0);
  const plan = useBrowserOperationController<ProductionBackupAuthorityWriteEnvelope, ProductionBackupAuthorityWriteResponse>({
    scope: `${scope}:plan`, readOnly: true, execute: applyProductionBackupAuthority,
    failureCertainty: productConfigFailureCertainty, failureFor: productConfigOperationFailure,
  });
  const apply = useBrowserOperationController<ProductionBackupAuthorityWriteEnvelope, ProductionBackupAuthorityWriteResponse>({
    scope, execute: applyProductionBackupAuthority, failureCertainty: approvalFailureCertainty, failureFor: productConfigOperationFailure,
  });
  const busy = checking || [plan.state.phase, apply.state.phase].some(phase => ["queued", "submitting"].includes(phase));
  const uncertain = apply.state.requiresIdempotencyContinuity;
  const supportedAction = Boolean(proposal && requiredActions.includes(proposal.policy.promotion_action));
  const proposalLoad = useRef(0);
  useEffect(() => {
    if (apply.state.phase === "failed" && !apply.state.requiresIdempotencyContinuity) {
      setPending(null); setReview(null);
      try { sessionStorage.removeItem(`${storageKey}:pending`); } catch { /* Settled refusal. */ }
    }
  }, [apply.state.phase, apply.state.requiresIdempotencyContinuity, storageKey]);
  useEffect(() => {
    if (fixtureMode) return;
    const controller = new AbortController();
    void Promise.all([readAuthSession(controller.signal), readProductEnvironmentConfigStatus(detail.product, detail.environment, controller.signal)])
      .then(([session, response]) => {
        if (controller.signal.aborted) return;
        const config = response.config_status;
        if (config.product !== detail.product || config.context !== detail.context || config.environment !== detail.environment) throw new Error("Backup requirements belong to another lane.");
        setAdmin(session.identity.role === "admin");
        setRequiredActions(config.production_backup_authorities.map(item => item.promotion_action));
      }).catch(failure => { if (!controller.signal.aborted) { setAdmin(false); setError(failure instanceof Error ? failure.message : "Current backup requirements could not be read."); } });
    return () => controller.abort();
  }, [fixtureMode, detail.product, detail.context, detail.environment]);
  useEffect(() => {
    generation.current += 1; setReview(null);
    if (!proposal || fixtureMode) return;
    setAuthority(null);
    const controller = new AbortController();
    setReadingAuthority(true);
    void readProductionBackupAuthority(proposal.policy, controller.signal).then(response => {
      if (!controller.signal.aborted) setAuthority(response.authority);
    }).catch(failure => { if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "Backup authority could not be read."); })
      .finally(() => { if (!controller.signal.aborted) setReadingAuthority(false); });
    return () => controller.abort();
  }, [proposal, fixtureMode]);

  async function load(file: File | undefined) {
    if (!file || !admin || busy || uncertain || fixtureMode) return;
    setReview(null); setError(""); setMessage("");
    const loadVersion = ++proposalLoad.current;
    generation.current += 1;
    try {
      if (file.size > 100_000) throw new Error("Use a single product's backup proposal (under 100 KB).");
      const next = readBackupProposal(await file.text(), detail);
      if (loadVersion !== proposalLoad.current) return;
      if (!requiredActions.includes(next.policy.promotion_action)) throw new Error("This proposal does not protect the lane's current production promotion.");
      const raw = JSON.stringify(next);
      sessionStorage.setItem(storageKey, raw);
      if (sessionStorage.getItem(storageKey) !== raw) throw new Error("The proposal could not be retained in this tab.");
      setProposal(next); plan.reset(); apply.reset();
    } catch (failure) {
      if (loadVersion === proposalLoad.current) setError(failure instanceof Error ? failure.message : "Proposal could not be loaded.");
    }
  }
  async function dryRun() {
    if (!proposal || !supportedAction || !admin || busy || uncertain || fixtureMode) return;
    setReview(null); setError(""); setMessage("");
    const version = generation.current;
    const response = await plan.run(proposal);
    if (!response || version !== generation.current) return;
    if (response.result.mode !== "dry_run" || response.result.status !== "would_apply" ||
        response.result.policy.promotion_action !== proposal.policy.promotion_action ||
        !/^[a-f0-9]{64}$/.test(response.result.authority_digest)) {
      setError("The dry run did not return a reviewable authority digest."); return;
    }
    setReview({ request: proposal, digest: response.result.authority_digest });
  }
  async function approve() {
    if (!admin || busy || fixtureMode || (uncertain ? !pending : !review || !supportedAction)) return;
    const request: ProductionBackupAuthorityWriteEnvelope = uncertain ? pending! : {
      ...review!.request, mode: "apply", reviewed_authority_digest: review!.digest,
    };
    try {
      const raw = JSON.stringify(request); sessionStorage.setItem(`${storageKey}:pending`, raw);
      if (sessionStorage.getItem(`${storageKey}:pending`) !== raw) throw new Error("Storage unavailable");
    } catch { setError("The reviewed request could not be retained. Nothing was sent."); return; }
    setPending(request); setError(""); setMessage("");
    const response = await apply.run(request);
    if (!response) {
      if (!apply.state.requiresIdempotencyContinuity) setReview(null);
      return;
    }
    setPending(null); setReview(null);
    try { sessionStorage.removeItem(`${storageKey}:pending`); } catch { /* The response settled approval. */ }
    setChecking(true);
    try {
      const current = await readProductionBackupAuthority(request.policy);
      setAuthority(current.authority);
      if (current.authority.policy?.record_id !== response.result.policy.record_id || !current.authority.ready) {
        setMessage("Approval completed, but current backup authority is not ready or differs. Refresh and inspect before another change.");
      } else {
        setMessage("Backup policy approved and read back. No capture or release was started.");
        try { sessionStorage.removeItem(storageKey); } catch { /* A reload still needs a fresh dry run. */ }
        setProposal(null); onRefresh();
      }
    } catch { setMessage("Approval completed; its authority read-back could not be confirmed. Refresh before another change."); }
    finally { setChecking(false); }
  }
  const snapshot = proposal?.policy.fast_snapshot;
  const backup = proposal?.policy.independent_backup;
  return <section className="product-config-panel" aria-labelledby={`${id}-title`} aria-busy={busy}>
    <header className="product-config-panel-header"><ShieldCheck aria-hidden="true" /><div>
      <p className="eyebrow">Production protection</p><h2 id={`${id}-title`}>Production backup authority</h2>
      <p>Review and approve this product's snapshot and independent backup policy. Approval changes backup records; it does not take a backup or release the product.</p>
    </div></header>
    <p>Current authority: <strong>{authority?.state ?? (proposal ? readingAuthority ? "Reading…" : "Unknown" : "Load a prepared proposal to inspect")}</strong></p>
    {!admin ? <p>A signed-in administrator with access to this product must review the proposal.</p> : null}
    <div className="product-config-field"><label htmlFor={`${id}-file`}>Prepared backup proposal</label>
      <input id={`${id}-file`} type="file" accept="application/json,.json" disabled={!admin || !requiredActions.length || busy || uncertain || fixtureMode} onChange={event => {
        const file = event.target.files?.[0]; event.target.value = ""; void load(file);
      }} />
      <small>Preparation uses a private proposal file. It stays in this tab until approval; credentials are never part of it.</small>
    </div>
    {proposal && snapshot && backup ? <div className="product-owner-plan">
      <h3>Proposed protection for {detail.display_name} · production</h3>
      <dl className="environment-action-facts">
        <div><dt>Production promotion scope</dt><dd>{proposal.policy.promotion_action}</dd></div>
        <div><dt>Snapshot retention</dt><dd>{snapshot.retention_count} snapshots · prefix {snapshot.snapshot_prefix}</dd></div>
        <div><dt>Snapshot freshness</dt><dd>{snapshot.max_evidence_age_seconds} seconds maximum age</dd></div>
        <div><dt>Independent backup freshness</dt><dd>{backup.max_evidence_age_seconds} seconds maximum age</dd></div>
        <div><dt>Policy revision</dt><dd>{proposal.policy.policy_revision}</dd></div>
        <div><dt>Effective from</dt><dd>{proposal.policy.effective_at}</dd></div>
        <div><dt>Review by</dt><dd>{proposal.policy.review_after}</dd></div>
      </dl>
      <p>{proposal.policy.reason}</p>
      <p>Guest snapshots and backups include the whole guest and any other workloads on it.</p>
      <ul>{proposal.targets?.map(target => <li key={target.target_id}>
        {target.target_id} · revision {target.target_revision}: {target.destination.host} · {target.destination.username} · {target.destination.destination_kind === "proxmox_guest" ? `${target.destination.guest_kind} ${target.destination.guest_id}` : target.destination.storage_id}
      </li>)}</ul>
      {!proposal.targets?.length ? <>
        <p>Existing targets: {snapshot.source_target_id} → {backup.destination_target_id}. The dry-run digest binds their current revisions; provider coordinates are withheld from this read.</p>
        <ul>{authority?.targets.map(target => <li key={target.record_id}>{target.target_id} · {target.destination_kind} · revision {target.target_revision} · {target.record_id}</li>)}</ul>
      </> : null}
    </div> : null}
    <OperationNotice state={plan.state} label="Dry run" /><OperationNotice state={apply.state} label="Approval" />
    {error ? <InlineFormError message={error} /> : null}
    {message ? <p role="status" aria-label="Backup authority result">{message}</p> : null}
    {review || (uncertain && pending) ? <p className="backup-authority-digest">Reviewed digest: <code>{review?.digest ?? pending?.reviewed_authority_digest}</code></p> : null}
    {uncertain ? <p role="status">The approval outcome is uncertain. Keep the original proposal and retry only the same request and key.</p> : null}
    <div className="product-config-actions">
      <button type="button" className="button" disabled={!admin || !proposal || !supportedAction || busy || uncertain || fixtureMode} onClick={() => void dryRun()}>Run backup authority dry run</button>
      <button type="button" className="button button-primary" disabled={!admin || busy || fixtureMode || (uncertain ? !pending : !review || !supportedAction)} onClick={() => void approve()}>{uncertain ? "Retry original backup approval" : "Approve this product's backup policy"}</button>
    </div>
  </section>;
}
