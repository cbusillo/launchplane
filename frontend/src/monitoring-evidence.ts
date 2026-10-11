import type { DataProvenance, ProductHealthMonitoringCheckSummary } from "./generated/openapi.ts";

// Each effective check owns its observation and deadline. Monitoring intent's
// provenance describes configuration, rather than whether probes returned.
export function monitoringEvidenceTrust(
  checks: readonly ProductHealthMonitoringCheckSummary[],
  now = Date.now(),
): DataProvenance["freshness_status"] {
  const states = checks.filter(check => check.probe_effective).map(check =>
    ["verified", "recorded"].includes(check.trust_state) && Date.parse(check.provenance.stale_after) < now
      ? "stale" : check.trust_state,
  );
  for (const state of ["missing", "stale", "unsupported", "recorded"] as const) {
    if (states.includes(state)) return state;
  }
  return states.length ? "verified" : "recorded";
}
