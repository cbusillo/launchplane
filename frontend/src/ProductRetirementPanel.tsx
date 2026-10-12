import { useEffect, useState } from "react";
import { Archive } from "lucide-react";
import { LaunchplaneApiError, executeProductRetirement, readAuthSession, readProductProfile } from "./api";
import { recoverBrowserOperationState } from "./browser-operation";
import { type DevFixtureMode } from "./dev-fixture-loader";
import { productConfigOperationFailure } from "./product-config-operation";
import { profileApplyFailureCertainty } from "./ProductOwnerPanel";
import { isOperationBusy, InlineFormError, ReasonField } from "./ProductConfigForms";
import { useBrowserOperationController } from "./use-browser-operation";
import type { AcceptedEvidenceResponse, ExecuteProductRetirementData } from "./generated/openapi.ts";

type RetirementRequest = ExecuteProductRetirementData["body"];
interface Effect { target_sha256?: string; preview_id?: string; application_name?: string; provider_state?: string; routes: string[]; affected_records: string[]; ownership: string }
interface Review { request: RetirementRequest; previews: Effect[]; testing: Effect }
function effect(value: unknown): value is Effect {
  if (!value || typeof value !== "object") return false;
  const item = value as Effect;
  return Array.isArray(item.routes) && item.routes.every(v => typeof v === "string") &&
    Array.isArray(item.affected_records) && item.affected_records.every(v => typeof v === "string") && typeof item.ownership === "string";
}
function reviewPlan(response: AcceptedEvidenceResponse, intent: RetirementRequest): Review | null {
  const result = response.result;
  const recordId = response.records?.product_retirement_plan_id;
  if (typeof recordId !== "string" || !recordId || result?.product !== intent.product || result.instance !== intent.instance ||
      result.mode !== "plan" || result.outcome !== "planned" || typeof result.plan_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(result.plan_sha256) ||
      typeof result.confirmation !== "string" || !result.confirmation.trim() ||
      !Array.isArray(result.preview_plan) || !result.preview_plan.every(p => effect(p) && typeof p.preview_id === "string" && typeof p.application_name === "string" && ["present", "absent"].includes(p.provider_state ?? "")) || !effect(result.testing_plan)) return null;
  return { request: { ...intent, mode: "apply", reviewed_plan_record_id: recordId, reviewed_plan_sha256: result.plan_sha256, confirmation: result.confirmation }, previews: result.preview_plan as Effect[], testing: result.testing_plan };
}

export function ProductRetirementPanel({ product, fixtureMode, onSaved }: { product: string; fixtureMode: DevFixtureMode; onSaved: () => void }) {
  const scope = `${product}:retirement:apply`;
  const storageKey = `launchplane:product-retirement:${product}`;
  const [review, setReview] = useState<Review | null>(() => {
    try {
      if (!recoverBrowserOperationState(scope).requiresIdempotencyContinuity) { sessionStorage.removeItem(storageKey); return null; }
      const saved = JSON.parse(sessionStorage.getItem(storageKey) ?? "null") as Review | null;
      return saved?.request.product === product && saved.request.include_previews === true && saved.request.mode === "apply" &&
        typeof saved.request.reviewed_plan_sha256 === "string" && /^[a-f0-9]{64}$/.test(saved.request.reviewed_plan_sha256) &&
        typeof saved.request.confirmation === "string" && Boolean(saved.request.confirmation.trim()) && effect(saved.testing) && saved.previews.every(effect) ? saved : null;
    } catch { return null; }
  });
  const [reason, setReason] = useState(review?.request.reason ?? "");
  const [relatedIssue, setRelatedIssue] = useState(review?.request.related_issue ?? "");
  const [admin, setAdmin] = useState(false);
  const [eligible, setEligible] = useState(false);
  const [confirmation, setConfirmation] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const plan = useBrowserOperationController<RetirementRequest, AcceptedEvidenceResponse>({
    scope: `${product}:retirement:plan`, execute: executeProductRetirement,
    failureCertainty: profileApplyFailureCertainty, failureFor: productConfigOperationFailure,
  });
  const apply = useBrowserOperationController<RetirementRequest, AcceptedEvidenceResponse>({
    scope, execute: async (payload, options) => {
      const response = await executeProductRetirement(payload, options);
      if (response.result?.product !== product || response.result.mode !== "apply" || response.result.lifecycle_after !== "retired" || response.result.provider_absence_verified !== true || response.records?.product_retirement_plan_id !== payload.reviewed_plan_record_id || response.result.plan_sha256 !== payload.reviewed_plan_sha256) throw new Error("The response does not verify the original retirement. Preserve its request and key.");
      return response;
    }, failureCertainty: (failure, dispatched) => failure instanceof LaunchplaneApiError && ["mutation_reconciliation_required", "mutation_in_progress"].includes(failure.code) ? "uncertain" : profileApplyFailureCertainty(failure, dispatched), failureFor: productConfigOperationFailure,
  });
  const busy = isOperationBusy(plan.state) || isOperationBusy(apply.state);
  const recovering = apply.state.requiresIdempotencyContinuity;
  const locked = busy || recovering || plan.state.requiresIdempotencyContinuity;
  const matches = review?.request.reason === reason.trim() && review?.request.related_issue === relatedIssue.trim();
  useEffect(() => {
    if (fixtureMode) return;
    const controller = new AbortController();
    void Promise.all([readAuthSession(controller.signal), readProductProfile(product, controller.signal)]).then(([session, profile]) => {
      if (controller.signal.aborted) return;
      setAdmin(session.is_administrator === true);
      setEligible(profile.profile.driver_id === "generic-web" && profile.profile.production_use !== "live" && profile.profile.lifecycle_state === "active" &&
        profile.profile.lanes?.length === 1 && profile.profile.lanes[0].instance === "testing");
    }).catch(failure => { if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "Retirement eligibility could not be read."); });
    return () => controller.abort();
  }, [product, fixtureMode]);
  useEffect(() => {
    if (apply.state.failure?.code === "product_retirement_blocked" && !apply.state.requiresIdempotencyContinuity) {
      setReview(null); setConfirmation(false);
      try { sessionStorage.removeItem(storageKey); } catch { /* The refusal settled before effects. */ }
    }
  }, [apply.state.failure, apply.state.requiresIdempotencyContinuity, storageKey]);
  async function preview() {
    if (!admin || busy || recovering) return;
    setError(""); setNotice(""); setReview(null); setConfirmation(false); apply.reset();
    const response = await plan.run({ mode: "plan", product, instance: "testing", include_previews: true, reason: reason.trim(), related_issue: relatedIssue.trim() });
    if (!response) return;
    const next = reviewPlan(response, { mode: "plan", product, instance: "testing", include_previews: true, reason: reason.trim(), related_issue: relatedIssue.trim() });
    if (!next) { setError("The retirement plan could not be verified. Nothing will be deleted."); return; }
    setReview(next);
  }
  async function retire() {
    if (!review || !admin || busy || (!recovering && (!matches || !confirmation))) return;
    setError(""); setNotice("");
    try {
      const draft = JSON.stringify(review); sessionStorage.setItem(storageKey, draft);
      if (sessionStorage.getItem(storageKey) !== draft) throw new Error("Draft unavailable");
    } catch { setError("The original reviewed request could not be preserved. Nothing was sent."); return; }
    const response = await apply.run(review.request);
    if (!response) return;
    if (response.result?.product !== product || response.result.lifecycle_after !== "retired" || response.result.provider_absence_verified !== true) {
      setError("The response does not prove retirement. Read the product again before another change."); return;
    }
    setReview(null); setConfirmation(false);
    try { sessionStorage.removeItem(storageKey); } catch { /* The durable response is settled. */ }
    try {
      const profile = await readProductProfile(product);
      if (profile.profile.lifecycle_state !== "retired") throw new Error("The current product is not retired.");
      setEligible(false); setNotice("Product retired. Preview and testing deletion is verified; history and container images are retained."); onSaved();
    } catch (failure) { setError(`Retirement completed; current product read-back failed. ${failure instanceof Error ? failure.message : "Read failed."}`); }
  }
  if (!admin) return null;
  return <section className="product-config-panel" aria-labelledby="product-retirement-title">
    <header className="product-config-panel-header"><span aria-hidden="true"><Archive /></span><div>
      <p className="eyebrow">Product lifecycle</p><h2 id="product-retirement-title">Retire product</h2>
      <p>Review this product’s previews, testing application and routes before deleting them. History and container images stay retained.</p>
    </div></header>
    <fieldset disabled={locked}><ReasonField reason={reason} onChange={setReason} /></fieldset>
    <label className="product-config-field">Related issue<input type="text" aria-label="Retirement related issue" value={relatedIssue} onChange={event => setRelatedIssue(event.target.value)} disabled={locked} /></label>
    <button className="button button-secondary" type="button" disabled={!eligible || busy || recovering || !reason.trim() || !relatedIssue.trim()} onClick={() => void preview()}>{plan.state.requiresIdempotencyContinuity ? "Retry original plan" : "Review retirement plan"}</button>
    {review && <div className="product-owner-plan" aria-label="Reviewed retirement plan">
      <details><summary>Retirement evidence</summary><p>Plan: <code>{review.request.reviewed_plan_record_id}</code></p><p>Digest: <code>{review.request.reviewed_plan_sha256}</code></p></details>
      <h3>Testing application</h3><EffectList item={review.testing} />
      <h3>Previews ({review.previews.length})</h3>
      {review.previews.length ? review.previews.map(item => <div key={item.preview_id}><h4>{item.application_name} · {item.provider_state}</h4><EffectList item={item} /></div>) : <p>No active previews.</p>}
      <p>The product enters retiring, preventing new previews and deploys. Launchplane verifies each deletion before retiring the product. A partial or uncertain result keeps that fence for recovery.</p>
      <p>{review.request.confirmation}</p>
      <label className="confirmation-check"><input type="checkbox" checked={confirmation} disabled={busy || recovering} onChange={event => setConfirmation(event.target.checked)} />I reviewed this deletion plan and want to retire this product.</label>
      <button className="button button-primary" type="button" disabled={busy || (!recovering && (!confirmation || !matches))} onClick={() => void retire()}>{recovering ? "Resume original retirement" : "Confirm retirement"}</button>
    </div>}
    {!eligible && !recovering && !notice && <p>Retirement here requires one non-live generic-web testing lane. Launchplane checks provider ownership before producing a plan.</p>}
    {recovering && <p role="status">The result is uncertain. Keep the original reviewed request and key; resume it to let Launchplane verify checkpoints before retrying.</p>}
    {(error || plan.state.failure?.message || apply.state.failure?.message) && <InlineFormError message={error || plan.state.failure?.message || apply.state.failure?.message || "Retirement failed."} />}
    {notice && <p role="status">{notice}</p>}
  </section>;
}
function EffectList({ item }: { item: Effect }) {
  return <><p>{item.application_name}</p><p>{item.ownership}</p>{item.target_sha256 && <details><summary>Target evidence</summary><code>{item.target_sha256}</code></details>}<ul>{item.routes.map(route => <li key={route}>Remove route: {route}</li>)}{item.affected_records.map(record => <li key={record}>Delete or disable authority: {record}</li>)}</ul></>;
}
