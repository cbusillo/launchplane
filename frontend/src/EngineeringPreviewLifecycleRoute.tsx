import { Wrench } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { LaunchplaneApiError, sweepPreviewLifecycle } from "./api";
import { recoverBrowserOperationState, stableRequestFingerprint } from "./browser-operation";
import type { DevFixtureMode } from "./dev-fixture-loader";
import { EngineeringRouteFrame } from "./EngineeringRouteUi";
import type { AcceptedEvidenceResponse, ApplyPreviewLifecycleSweepData } from "./generated/openapi.ts";
import { InlineFormError, OperationNotice } from "./ProductConfigForms";
import { productConfigOperationFailure } from "./product-config-operation";
import { useBrowserOperationController } from "./use-browser-operation";
import "./preview-lifecycle.css";

type SweepRequest = ApplyPreviewLifecycleSweepData["body"];
class SweepRecoveryStorageError extends Error {}

function sweepRequest(apply: boolean): SweepRequest {
  return { apply, source: "launchplane-preview-lifecycle" };
}

export function EngineeringPreviewLifecycleRoute({ actorId, admin, fixtureMode }: {
  actorId: number; admin: boolean; fixtureMode: DevFixtureMode;
}) {
  return <EngineeringRouteFrame view="preview-lifecycle" title="Preview cleanup" icon={Wrench}
    description="Report orphaned previews or explicitly clean them up across all products with previews enabled.">
    {admin && !fixtureMode ? <SweepControls actorId={actorId} /> :
      <p role="status">Preview cleanup requires a signed-in administrator outside demonstration mode.</p>}
  </EngineeringRouteFrame>;
}

function SweepControls({ actorId }: { actorId: number }) {
  const scope = `github:${actorId}:preview-lifecycle-sweep`;
  const submitting = useRef(false);
  const [running, setRunning] = useState(false);
  const [recovery, setRecovery] = useState<{ fingerprint: string; payload: SweepRequest } | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState("");
  const [response, setResponse] = useState<AcceptedEvidenceResponse | null>(null);
  const operation = useBrowserOperationController<SweepRequest, AcceptedEvidenceResponse>({
    scope, failureFor: productConfigOperationFailure,
    execute: async (payload, options) => {
      const fingerprint = await stableRequestFingerprint({ request: payload, scope });
      return sweepPreviewLifecycle(payload, { ...options, onDispatch: () => {
        // The shared controller persists a submitting identity at this boundary.
        // Verify it before requestJson can issue the sweep POST.
        options.onDispatch?.();
        const retained = recoverBrowserOperationState(scope);
        if (retained.identity?.idempotencyKey !== options.idempotencyKey ||
            retained.identity.requestFingerprint !== fingerprint) {
          throw new SweepRecoveryStorageError("The operation key could not be retained for recovery. Nothing was sent; allow session storage and retry.");
        }
      } });
    },
    failureCertainty: (failure, dispatched) => failure instanceof SweepRecoveryStorageError || !dispatched ||
      (failure instanceof LaunchplaneApiError && failure.statusCode < 500) ? "definitive" : "uncertain",
  });
  const busy = running || ["queued", "submitting"].includes(operation.state.phase);
  const uncertain = operation.state.requiresIdempotencyContinuity && !busy;
  const fingerprint = operation.state.identity?.requestFingerprint;
  const saved = recovery?.fingerprint === fingerprint ? recovery?.payload : null;

  useEffect(() => {
    if (!uncertain || !fingerprint) return;
    let active = true;
    setRecovery(null);
    setError("");
    void (async () => {
      // These are the only two requests this page can send. Match the retained
      // fingerprint instead of relying on a separately persisted form draft.
      for (const apply of [false, true]) {
        const payload = sweepRequest(apply);
        if (await stableRequestFingerprint({ request: payload, scope }) === fingerprint) {
          if (active) setRecovery({ fingerprint, payload });
          return;
        }
      }
      throw new Error("The retained fingerprint does not match either supported sweep request.");
    })().catch(() => {
      if (active) setError("The original request could not be recovered. Keep the operation key and inspect its service receipt before another sweep.");
    });
    return () => { active = false; };
  }, [uncertain, fingerprint, scope]);

  async function run(apply: boolean) {
    if (submitting.current || busy || uncertain && !saved || !uncertain && apply && !confirmed) return;
    submitting.current = true;
    setRunning(true);
    setConfirmed(false);
    try {
      const payload = uncertain ? saved! : sweepRequest(apply);
      setError("");
      setResponse(null);
      const result = await operation.run(payload);
      if (!result) return;
      setResponse(result);
    } finally {
      submitting.current = false;
      setRunning(false);
    }
  }

  return <section className="preview-lifecycle-panel" aria-label="Preview cleanup controls">
    <h2>All preview-enabled products</h2>
    <p>Report records what needs cleanup. Apply cleanup rechecks current state and deletes orphaned previews where permitted. Held previews require their original recovery process.</p>
    <OperationNotice label="Preview sweep" state={operation.state} />
    {error ? <InlineFormError message={error} /> : null}
    {uncertain ? <>
      <p role="status">The {saved?.apply ? "cleanup" : saved ? "report" : "original"} result is uncertain. Retry the retained request with its original operation key before starting another sweep.</p>
      <button className="button" type="button" disabled={busy || !saved} onClick={() => void run(false)}>
        {saved ? `Retry original ${saved.apply ? "cleanup" : "report"}` : "Retry original sweep"}
      </button>
    </> : <>
      <label className="preview-lifecycle-confirm"><input type="checkbox" checked={confirmed} disabled={busy}
        onChange={event => setConfirmed(event.target.checked)} />Delete orphaned previews across all preview-enabled products.</label>
      <div className="product-config-actions">
        <button className="button" type="button" disabled={busy} onClick={() => void run(false)}>Report</button>
        <button className="button button-primary" type="button" disabled={busy || !confirmed} onClick={() => void run(true)}>Apply cleanup</button>
        {busy ? <span role="status">Running preview sweep…</span> : null}
      </div>
    </>}
    {operation.state.identity ? <p className="preview-lifecycle-key">Operation key: <code>{operation.state.identity.idempotencyKey}</code></p> : null}
    {response ? <SweepResult response={response} /> : null}
  </section>;
}

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function text(value: unknown, fallback = "Unknown"): string {
  return typeof value === "string" && value ? value : fallback;
}

function count(value: unknown): string {
  return Array.isArray(value) ? String(value.length) : "Unknown";
}

function SweepResult({ response }: { response: AcceptedEvidenceResponse }) {
  const result = record(response.result);
  const profiles = Array.isArray(result.profiles) ? result.profiles.map(record) : null;
  return <section className="preview-lifecycle-result" aria-label="Preview sweep result" aria-live="polite">
    <h2>{result.apply === true ? "Cleanup" : result.apply === false ? "Report" : "Sweep"} result: {text(result.status)}</h2>
    <p>Trace: <code>{response.original_trace_id || response.trace_id}</code>{response.replayed ? " · Replayed original result" : ""}</p>
    {!profiles ? <p role="alert">Per-product outcomes are unavailable. Inspect this trace before another cleanup.</p> :
      !profiles.length ? <p>No preview-enabled products were returned.</p> :
        <ul className="preview-lifecycle-products">{profiles.map((profile, index) => <li key={`${text(profile.product)}:${index}`}>
          <h3>{text(profile.product)}: {text(profile.status)}</h3>
          {profile.error_message ? <p>{text(profile.error_message)}</p> : null}
          <dl>{[["Orphaned", profile.orphaned_slugs], ["Destroyed", profile.destroyed_slugs],
            ["Failed", profile.failed_slugs], ["Blocked", profile.blocked_slugs], ["Held", profile.held_reservations]].map(([label, value]) =>
              <div key={String(label)}><dt>{String(label)}</dt><dd>{count(value)}{label === "Held" && profile.held_reservations_truncated ? "+" : ""}</dd></div>)}</dl>
          <details><summary>Evidence</summary><p>Context: <code>{text(profile.context)}</code></p>
            <p>Plan: <code>{text(profile.preview_lifecycle_plan_id, "Not returned")}</code></p>
            <p>Cleanup record: <code>{text(profile.preview_lifecycle_cleanup_id, "Not returned")}</code></p></details>
        </li>)}</ul>}
  </section>;
}
