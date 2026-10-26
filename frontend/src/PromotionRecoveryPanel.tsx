import { useEffect, useRef, useState } from "react";
import { ShieldCheck } from "lucide-react";
import { applyPromotionRecovery, LaunchplaneApiError, readAuthSession, readPromotionRecovery, reviewPromotionRecovery, type PromotionRecoveryTarget } from "./api";
import type { PromotionRecoveryPlan } from "./generated/openapi.ts";

type Review = { target: PromotionRecoveryTarget; reason: string; plan: PromotionRecoveryPlan };

function verifiedPlan(plan: PromotionRecoveryPlan, product: string, original?: PromotionRecoveryPlan): boolean {
  return plan.product === product && plan.instance === "prod" && typeof plan.recovery_reference === "string" && !!plan.recovery_reference &&
    /^[a-f0-9]{64}$/.test(plan.recovery_digest) && ["running", "reconcile_required", "completed"].includes(plan.reservation_state) &&
    Number.isInteger(plan.reservation_attempt) && plan.reservation_attempt > 0 && typeof plan.checkpoint === "string" &&
    ["replay_completed", "wait_for_active_lease", "adopt_promotion", "adopt_rollback", "hold_unknown"].includes(plan.proposed_action) &&
    (!original || plan.recovery_reference === original.recovery_reference);
}

export function PromotionRecoveryPanel({ product, fixtureMode }: { product: string; fixtureMode: boolean }) {
  const storageKey = `launchplane:promotion-recovery:${product}`;
  const [admin, setAdmin] = useState(false);
  const [decisionRecordId, setDecisionRecordId] = useState("");
  const [attempt, setAttempt] = useState<1 | 2>(1);
  const [step, setStep] = useState<"promote" | "rollback">("promote");
  const [reason, setReason] = useState("");
  const [review, setReview] = useState<Review | null>(null);
  const [attempted, setAttempted] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const inFlight = useRef(false);
  useEffect(() => {
    if (fixtureMode) return;
    const controller = new AbortController();
    void readAuthSession(controller.signal).then(session => {
      if (controller.signal.aborted) return;
      setAdmin(session.is_administrator === true);
      if (!session.is_administrator) return;
      try {
        const value = sessionStorage.getItem(storageKey);
        if (!value) return;
        const saved = JSON.parse(value) as Review;
        if (saved.target.product !== product || !saved.target.decisionRecordId || ![1, 2].includes(saved.target.attempt) ||
            !["promote", "rollback"].includes(saved.target.step) || typeof saved.reason !== "string" || !verifiedPlan(saved.plan, product)) throw new Error();
        setDecisionRecordId(saved.target.decisionRecordId); setAttempt(saved.target.attempt); setStep(saved.target.step);
        setReason(saved.reason); setReview(saved); setAttempted(true);
        setNotice("Apply was already attempted. Read back the original operation; do not retry.");
      } catch { setAttempted(true); setError("Recovery storage is unavailable or unverified. Stop and retain the original operation."); }
    }).catch(() => { if (!controller.signal.aborted) setAdmin(false); });
    return () => controller.abort();
  }, [fixtureMode, product, storageKey]);

  function edit(change: () => void) {
    change(); setReview(null); setConfirmed(false); setError(""); setNotice("");
  }
  function fail(failure: unknown, action: "Review" | "Apply" | "Readback") {
    setConfirmed(false);
    setError(failure instanceof LaunchplaneApiError
      ? `${action} stopped: HTTP ${failure.statusCode}, ${failure.code || "request_failed"}. Trace: ${failure.traceId || "unavailable"}. Stop and retain the original operation. Do not retry Apply.`
      : `${action} is unverified. Stop and retain the original operation. Stop and retain the original operation. Do not retry Apply.`);
  }
  function operationStorageKey(target: PromotionRecoveryTarget): string {
    return `${storageKey}:${encodeURIComponent(target.decisionRecordId)}:${target.step}:${target.attempt}`;
  }
  function chooseAnother() {
    try {
      sessionStorage.removeItem(storageKey);
      setAttempted(false); setReview(null); setConfirmed(false); setDecisionRecordId(""); setReason(""); setError(""); setNotice("");
    } catch { setError("Recovery storage is unavailable. Keep the original operation until storage is available."); }
  }
  async function inspect() {
    if (!admin || inFlight.current || attempted) return;
    inFlight.current = true; setBusy(true); setReview(null); setConfirmed(false); setError(""); setNotice("");
    const target: PromotionRecoveryTarget = { product, decisionRecordId: decisionRecordId.trim(), attempt, step };
    const reviewedReason = reason.trim();
    try {
      const previous = sessionStorage.getItem(operationStorageKey(target));
      if (previous) {
        const saved = JSON.parse(previous) as Review;
        if (!verifiedPlan(saved.plan, product) || JSON.stringify(saved.target) !== JSON.stringify(target)) throw new Error();
        setReview(saved); setReason(saved.reason); setAttempted(true);
        setNotice("Apply was already attempted. Read back the original operation; do not retry.");
        return;
      }
      const original = await readPromotionRecovery(target);
      if (!verifiedPlan(original, product)) throw new Error();
      const plan = await reviewPromotionRecovery(target, reviewedReason);
      if (!verifiedPlan(plan, product, original) || plan.reservation_state !== original.reservation_state ||
          plan.reservation_attempt !== original.reservation_attempt || plan.checkpoint !== original.checkpoint) throw new Error();
      setReview({ target, reason: reviewedReason, plan });
    } catch (failure) { fail(failure, "Review"); }
    finally { inFlight.current = false; setBusy(false); }
  }
  const actionable = !!review && review.plan.reservation_state !== "completed" &&
    ["adopt_promotion", "adopt_rollback"].includes(review.plan.proposed_action);
  async function apply() {
    if (!admin || inFlight.current || attempted || !review || !confirmed || !actionable) return;
    inFlight.current = true; setBusy(true); setConfirmed(false); setError(""); setNotice("");
    setAttempted(true);
    try {
      const saved = JSON.stringify(review);
      sessionStorage.setItem(operationStorageKey(review.target), saved);
      sessionStorage.setItem(storageKey, saved);
      if (sessionStorage.getItem(operationStorageKey(review.target)) !== saved || sessionStorage.getItem(storageKey) !== saved) throw new Error();
      const result = await applyPromotionRecovery(review.target, {
        reason: review.reason, recovery_reference: review.plan.recovery_reference, expected_recovery_digest: review.plan.recovery_digest,
      });
      if (!verifiedPlan(result, product, review.plan) || result.status !== "accepted" || result.reservation_state !== "completed") throw new Error();
      setNotice("Apply returned completed. Read back the original operation separately.");
    } catch (failure) { fail(failure, "Apply"); }
    finally { inFlight.current = false; setBusy(false); }
  }
  async function readback() {
    if (!admin || inFlight.current || !review) return;
    inFlight.current = true; setBusy(true); setError(""); setNotice("");
    try {
      const result = await readPromotionRecovery(review.target);
      if (!verifiedPlan(result, product, review.plan)) throw new Error();
      setNotice(result.reservation_state === "completed"
        ? "Original reservation is completed. This does not establish release success."
        : `Original reservation remains ${result.reservation_state}. Stop; the operation is not settled.`);
    } catch (failure) { fail(failure, "Readback"); }
    finally { inFlight.current = false; setBusy(false); }
  }
  if (!admin || fixtureMode) return null;
  return <section className="product-config-panel promotion-recovery-panel" aria-labelledby="promotion-recovery-title">
    <header className="product-config-panel-header"><span aria-hidden="true"><ShieldCheck /></span><div>
      <p className="eyebrow">Production · existing operation</p><h2 id="promotion-recovery-title">Recover promotion</h2>
      <p>Review an interrupted accepted release for {product}. Apply reconciles its original operation using your existing administrator authority.</p>
    </div></header>
    <fieldset disabled={busy || attempted}>
      <label className="product-config-field">Release decision<input type="text" value={decisionRecordId} onChange={event => edit(() => setDecisionRecordId(event.target.value))} /></label>
      <label className="product-config-field">Release step<select value={step} onChange={event => edit(() => setStep(event.target.value as typeof step))}><option value="promote">Promotion</option><option value="rollback">Rollback drill</option></select></label>
      <label className="product-config-field">Promotion attempt<select value={attempt} onChange={event => edit(() => setAttempt(Number(event.target.value) as 1 | 2))}><option value={1}>First</option><option value={2}>Second</option></select></label>
      <label className="product-config-field">Recovery reason<textarea value={reason} maxLength={1000} onChange={event => edit(() => setReason(event.target.value))} /></label>
    </fieldset>
    <button className="button button-secondary" type="button" disabled={busy || attempted || !decisionRecordId.trim() || !reason.trim()} onClick={() => void inspect()}>Review recovery</button>
    {review && <div className="product-owner-plan" aria-label="Reviewed promotion recovery">
      <dl className="environment-action-facts">
        <div><dt>Original operation</dt><dd><code>{review.plan.recovery_reference}</code></dd></div>
        <div><dt>Original state</dt><dd>{review.plan.reservation_state}</dd></div>
        <div><dt>Checkpoint</dt><dd>{review.plan.checkpoint}</dd></div>
        <div><dt>Proposed action</dt><dd>{review.plan.proposed_action}</dd></div>
      </dl>
      <details><summary>Reviewed evidence</summary><p>Decision: <code>{review.target.decisionRecordId}</code></p><p>Step: {review.target.step}, attempt {review.target.attempt}</p><p>Reservation attempt: {review.plan.reservation_attempt}</p><p>Digest: <code>{review.plan.recovery_digest}</code></p></details>
      {!actionable && <p>The original operation is held or already completed. Apply is unavailable.</p>}
      <label className="confirmation-check"><input type="checkbox" checked={confirmed} disabled={busy || attempted || !actionable} onChange={event => setConfirmed(event.target.checked)} />I reviewed this original operation and authorize this recovery once.</label>
    </div>}
    <div className="product-config-actions">
      <button className="button button-primary" type="button" disabled={busy || attempted || !actionable || !confirmed} onClick={() => void apply()}>Apply reviewed recovery</button>
      <button className="button button-secondary" type="button" disabled={busy || !review} onClick={() => void readback()}>Read back original operation</button>
      {attempted && <button className="button button-secondary" type="button" disabled={busy} onClick={chooseAnother}>Choose another operation</button>}
    </div>
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
  </section>;
}
