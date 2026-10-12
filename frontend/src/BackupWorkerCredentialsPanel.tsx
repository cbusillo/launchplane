import { KeyRound } from "lucide-react";
import { useEffect, useId, useState } from "react";
import { readAuthSession, readProductEnvironmentConfigStatus, readBackupWorkerBindings, rebindBackupWorkerCredentials, qualifyProductionBackupPreflight, LaunchplaneApiError } from "./api";
import type { BackupCredentialBindingRequest, AcceptedEvidenceResponse, ProductEnvironmentDetail } from "./generated/openapi.ts";
import { recoverBrowserOperationState } from "./browser-operation";
import { productConfigFailureCertainty, productConfigOperationFailure } from "./product-config-operation";
import { OperationNotice } from "./ProductConfigForms";
import { useBrowserOperationController } from "./use-browser-operation";

const roles = [
  ["PRODUCTION_BACKUP_SSH_PRIVATE_KEY", "Capture SSH credential"],
  ["PRODUCTION_BACKUP_SSH_KNOWN_HOSTS", "SSH host verification"],
] as const;
type Credential = { binding_id: string; binding_key: string; version_id: string };

export function BackupWorkerCredentialsPanel({ detail, fixtureMode }: { detail: ProductEnvironmentDetail; fixtureMode: boolean }) {
  const id = useId();
  const scope = `${detail.product}:${detail.context}:prod:backup-worker-bindings`;
  const pendingKey = `${scope}:pending`;
  const [options, setOptions] = useState<Credential[]>([]);
  const [bindings, setBindings] = useState<Record<string, string>>({});
  const [promotionAction, setPromotionAction] = useState("");
  const [admin, setAdmin] = useState(false);
  const [review, setReview] = useState<BackupCredentialBindingRequest | null>(null);
  const [pending, setPending] = useState<BackupCredentialBindingRequest | null>(() => {
    try { return recoverBrowserOperationState(scope).requiresIdempotencyContinuity ? JSON.parse(sessionStorage.getItem(pendingKey) ?? "null") : null; } catch { return null; }
  });
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [reading, setReading] = useState(false);
  const apply = useBrowserOperationController<BackupCredentialBindingRequest, AcceptedEvidenceResponse>({
    scope, execute: (body, operation) => rebindBackupWorkerCredentials(detail.product, body, operation),
    failureCertainty: (failure, dispatched) => failure instanceof LaunchplaneApiError && failure.code === "backup_credential_conflict" ? "settled" : productConfigFailureCertainty(failure, dispatched),
    failureFor: productConfigOperationFailure,
  });
  const uncertain = apply.state.requiresIdempotencyContinuity;
  const busy = reading || ["queued", "submitting"].includes(apply.state.phase);
  async function refresh(signal?: AbortSignal) {
    const response = await readBackupWorkerBindings(detail.product, signal);
    const result = response.result;
    if (!result || result.product !== detail.product || result.context !== detail.context || result.instance !== "prod" || !Array.isArray(result.credentials)) throw new Error("Worker binding evidence does not match this production lane.");
    const next = result.credentials as Credential[];
    if (next.some(item => !item.binding_id || !item.binding_key || !item.version_id)) throw new Error("Worker binding evidence is incomplete.");
    setOptions(next);
    return next;
  }
  useEffect(() => {
    if (fixtureMode) return;
    const controller = new AbortController();
    void Promise.all([readAuthSession(controller.signal), refresh(controller.signal), readProductEnvironmentConfigStatus(detail.product, "prod", controller.signal)]).then(([session, credentials, config]) => {
      if (controller.signal.aborted) return;
      if (config.config_status.product !== detail.product || config.config_status.context !== detail.context || config.config_status.environment !== "prod") throw new Error("Backup requirements do not match this production lane.");
      const requirements = config.config_status.production_backup_authorities;
      setPromotionAction(requirements.length === 1 ? requirements[0].promotion_action : "");
      setAdmin(session.identity.role === "admin");
      setBindings(Object.fromEntries(roles.map(([key]) => [key, credentials.find(item => item.binding_key === key)?.binding_id ?? ""])));
    }).catch(failure => { if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "Worker binding metadata is unavailable."); });
    return () => controller.abort();
  }, [detail.product, detail.context, fixtureMode]);
  useEffect(() => {
    if (apply.state.phase === "failed" && !uncertain) { setPending(null); setReview(null); sessionStorage.removeItem(pendingKey); }
  }, [apply.state.phase, uncertain, pendingKey]);

  async function preview() {
    setReading(true); setError(""); setMessage(""); setReview(null);
    try {
      const body: BackupCredentialBindingRequest = { mode: "dry-run", bindings, reason: "Rebind this production lane's existing credentials for the shared backup worker" };
      const response = await rebindBackupWorkerCredentials(detail.product, body, { idempotencyKey: "" });
      const digest = response.result?.plan_sha256;
      if (typeof digest !== "string" || !/^[a-f0-9]{64}$/.test(digest)) throw new Error("The preview did not return a reviewed digest.");
      if (!response.result?.changed) { setMessage("The shared backup worker already uses these credentials."); return; }
      setReview({ ...body, mode: "apply", reviewed_plan_sha256: digest });
    } catch (failure) { setError(failure instanceof Error ? failure.message : "Binding preview failed."); }
    finally { setReading(false); }
  }
  async function rebind() {
    const body = uncertain ? pending : review;
    if (!body) return;
    setError(""); setMessage("");
    try { sessionStorage.setItem(pendingKey, JSON.stringify(body)); setPending(body); }
    catch { setError("This browser cannot retain the request for safe retry."); return; }
    const response = await apply.run(body);
    if (!response) return;
    setReading(true);
    setReview(null); setPending(null); sessionStorage.removeItem(pendingKey);
    try {
      const credentials = await refresh();
      if (response.result?.applied !== true || roles.some(([key]) => !credentials.some(item => item.binding_key === key && item.binding_id === body.bindings[key]))) throw new Error("The apply completed, but current binding read-back differs. Refresh this page before continuing.");
      setMessage("Existing credentials rebound and read back. Run the read-only check next.");
    } catch (failure) { setError(failure instanceof Error ? failure.message : "Binding read-back failed."); }
    finally { setReading(false); }
  }
  async function probe() {
    setReading(true); setError(""); setMessage("");
    try {
      const response = await qualifyProductionBackupPreflight({ product: detail.product, context: detail.context, instance: "prod", promotion_action: promotionAction });
      const result = response.result;
      if (result?.status !== "pass" || result.boundary_status !== "match_capture_only" || result.storage_status !== "active_pbs" || result.capture_started !== false) throw new Error(`Read-only backup check did not pass (${String(result?.error_code ?? "unverified")}). No capture was started.`);
      setMessage(`Capture-only boundary matches policy revision ${String(result.policy_revision)}; PBS is active and reachable. No capture was started.`);
    } catch (failure) { setError(failure instanceof Error ? failure.message : "Read-only backup check failed."); }
    finally { setReading(false); }
  }
  return <section className="product-config-panel" aria-labelledby={`${id}-title`} aria-busy={busy}>
    <header className="product-config-panel-header"><KeyRound aria-hidden="true" /><div><h2 id={`${id}-title`}>Production backup credentials</h2>
    <p>Reuse this lane's stored capture credential and host verification. The host and storage come from its approved backup policy.</p></div></header>
    {fixtureMode ? <p>Credential controls are unavailable in this preview.</p> : <>
      {roles.map(([key, label]) => <label className="promotion-field" key={key} htmlFor={`${id}-${key}`}><span>{label}</span><select id={`${id}-${key}`} value={bindings[key] ?? ""} disabled={!admin || busy || uncertain} onChange={event => { setBindings({ ...bindings, [key]: event.target.value }); setReview(null); setMessage(""); }}>
        <option value="">Select an existing worker binding</option>
        {options.map(item => <option key={item.binding_id} value={item.binding_id}>{item.binding_key}</option>)}
      </select></label>)}
      {review ? <p>Both selected references will become the shared worker's backup bindings; their encrypted secret versions stay the same.</p> : null}
      <div className="product-config-actions"><button type="button" className="button" disabled={!admin || busy || uncertain || roles.some(([key]) => !bindings[key])} onClick={() => void preview()}>Preview credential bindings</button>
      <button type="button" className="button" disabled={!admin || busy || (uncertain ? !pending : !review)} onClick={() => void rebind()}>{uncertain ? "Retry original credential rebinding" : "Use these existing credentials"}</button>
      <button type="button" className="button" disabled={busy || uncertain || !promotionAction} onClick={() => void probe()}>Check capture boundary and PBS</button></div>
      <OperationNotice label="Backup credential rebinding" state={apply.state} />
    </>}
    {message ? <p role="status" aria-label="Backup credential result">{message}</p> : null}
    {error ? <p role="alert">{error}</p> : null}
  </section>;
}
