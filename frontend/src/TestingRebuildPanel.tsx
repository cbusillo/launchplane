import { useEffect, useRef, useState } from "react";
import { inspectTestingRebuild, readTestingRebuild, submitTestingRebuild } from "./api";
import { waitFor } from "./odoo-release-operation";
import { readTestingRebuildAttempt, runTestingRebuild, saveTestingRebuildAttempt, type TestingRebuildAttempt } from "./testing-rebuild-operation";
import type { ProductEnvironmentDetail, TestingRebuildPreflight, TestingRebuildResponse, TestingRebuildSubmit } from "./generated/openapi.ts";

export function TestingRebuildPanel({ detail, fixtureMode, onRefresh }: {
  detail: ProductEnvironmentDetail; fixtureMode: boolean; onRefresh: () => void;
}) {
  const [source, setSource] = useState<TestingRebuildSubmit["data_source_mode"]>("empty");
  const [reason, setReason] = useState("Rebuild testing for a fresh test session.");
  const [preflight, setPreflight] = useState<TestingRebuildPreflight | null>(null);
  const [inspectionError, setInspectionError] = useState("");
  const [attempt, setAttempt] = useState(() => readTestingRebuildAttempt(detail.product));
  const [result, setResult] = useState<TestingRebuildResponse | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const abort = useRef<AbortController | null>(null);

  useEffect(() => {
    if (fixtureMode || attempt) return;
    const controller = new AbortController();
    setPreflight(null); setInspectionError("");
    void inspectTestingRebuild(detail.product, source, controller.signal).then(response => {
      if (controller.signal.aborted) return;
      if (response.product !== detail.product || response.context !== detail.context || response.data_source_mode !== source || response.base_url.replace(/\/$/, "") !== detail.base_url.replace(/\/$/, "")) {
        throw new Error("The inspection does not identify this testing lane.");
      }
      setPreflight(response);
    }).catch(error => { if (!controller.signal.aborted) setInspectionError(error instanceof Error ? error.message : "Testing rebuild inspection is unavailable."); });
    return () => controller.abort();
  }, [detail.product, detail.context, detail.base_url, source, fixtureMode, refresh, !!attempt]);

  useEffect(() => {
    // Reloading may resume an existing operation read. It never resubmits a write.
    const saved = readTestingRebuildAttempt(detail.product);
    if (!fixtureMode && saved?.operationId && !saved.denied) void rebuild(saved);
    return () => abort.current?.abort();
  }, [detail.product, fixtureMode]);

  async function rebuild(existing?: TestingRebuildAttempt) {
    if (abort.current || fixtureMode) return;
    if (!existing && (!preflight || !reason.trim())) return;
    const selected = existing ?? {
      product: detail.product, key: `ui-testing-rebuild-${crypto.randomUUID()}`,
      payload: { data_source_mode: source, artifact_id: preflight!.artifact_id,
        source_git_ref: preflight!.source_git_ref, reason: reason.trim() },
    } satisfies TestingRebuildAttempt;
    const controller = new AbortController(); abort.current = controller;
    setBusy(true); setMessage("Preparing and checking the testing copy. The address stays the same.");
    try {
      const response = await runTestingRebuild(selected, {
        submit: submitTestingRebuild, read: readTestingRebuild, wait: waitFor,
        persist: value => { saveTestingRebuildAttempt(detail.product, value); setAttempt(value); if (!value) setPreflight(null); },
        progress: setResult,
      }, controller.signal);
      if (controller.signal.aborted) return;
      const status = response.operation.status;
      setMessage(status === "pass" ? "Testing rebuilt and verified healthy at the same address."
        : "The rebuild stopped. Check the lane health and operation evidence before continuing.");
      if (["pass", "fail", "cancelled", "superseded"].includes(status)) {
        saveTestingRebuildAttempt(detail.product, null); setAttempt(null); setPreflight(null); onRefresh();
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        const retained = readTestingRebuildAttempt(detail.product);
        setMessage(`${error instanceof Error ? error.message : "The rebuild outcome is unknown."} ${retained
          ? "Keep this request; do not start another rebuild." : "Review the fresh inspection before rebuilding."}`);
      }
    } finally {
      if (abort.current === controller) { abort.current = null; setBusy(false); }
    }
  }

  const locked = fixtureMode || busy || !!attempt;
  return <section className="promotion-control" aria-labelledby="testing-rebuild-title">
    <header className="promotion-control-header"><div>
      <p className="eyebrow">Testing</p><h2 id="testing-rebuild-title">Rebuild testing at the same address</h2>
      <p>{detail.base_url}</p>
      <p>Use the latest verified build. A working copy keeps serving until its replacement is healthy. Testing passwords and the old copy's data are retained.</p>
    </div></header>
    {fixtureMode ? <p>Rebuild controls are off in fixture mode.</p> : null}
    <label className="promotion-field"><span>Start with</span><select value={attempt?.payload.data_source_mode ?? source} disabled={locked} onChange={event => { setSource(event.target.value as TestingRebuildSubmit["data_source_mode"]); setPreflight(null); setResult(null); setMessage(""); }}>
      <option value="empty">Fresh empty database</option><option value="production_restore">Sanitized production copy</option><option value="upstream_restore">Configured upstream copy</option>
    </select></label>
    {(attempt?.payload.data_source_mode ?? source) !== "empty" ? <p>Copied data has live keys and outbound connections removed before it runs.</p> : null}
    {!attempt && !fixtureMode ? <p>{inspectionError || (preflight ? "Latest verified build selected." : "Checking the current build and testing lane…")}</p> : null}
    {!attempt ? <button type="button" className="primary-button" disabled={locked || !preflight || !reason.trim()} onClick={() => void rebuild()}>Rebuild testing</button> : null}
    <details><summary>Reason (optional edit)</summary><label className="promotion-field"><span>Reason</span><input value={attempt?.payload.reason ?? reason} disabled={locked} maxLength={1000} onChange={event => setReason(event.target.value)} /></label></details>
    {inspectionError && !attempt ? <button type="button" className="secondary-button" disabled={busy || fixtureMode} onClick={() => setRefresh(value => value + 1)}>Refresh inspection</button> : null}
    {attempt && !attempt.denied ? <button type="button" className="secondary-button" disabled={busy || fixtureMode} onClick={() => void rebuild(attempt)}>{attempt.operationId ? "Resume watching this rebuild" : "Recover this request"}</button> : null}
    {attempt?.denied ? <p>This request was denied and will not be submitted again. The denial needs to be resolved before rebuilding.</p> : null}
    <p role="status">{message}</p>
    {result ? <details><summary>Operation details</summary><p>{result.operation.status}: {result.operation.phase}</p><p>Operation: {result.operation.operation_id}</p><p>Trace: {result.trace_id}</p></details> : null}
  </section>;
}
