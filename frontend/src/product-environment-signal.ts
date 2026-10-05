import type { ProductEnvironmentSummary } from "./generated/openapi.ts";

export type SignalTone = ProductEnvironmentSummary["trust_state"] | "warning" | "danger";

export function environmentOperationalTone(environment: ProductEnvironmentSummary | null, now = Date.now()): SignalTone {
  if (!environment) return "missing";
  const checks = environment.health_monitoring.checks.filter(check => check.probe_effective);
  const negativeTlsStates = new Set([
    "expired", "hostname_mismatch", "untrusted", "self_signed", "unreachable",
  ]);
  if (
    checks.some(check => check.status === "fail" || check.incident_status === "open") ||
    ["mismatch", "missing", "malformed", "unverifiable"].includes(
      environment.topology.observed.placement?.runtime_identity_status ?? "unchecked",
    ) ||
    environment.topology.warnings.some(warning => warning.severity === "error") ||
    environment.topology.observed.tls_domains.some(domain => negativeTlsStates.has(domain.status))
  ) return "danger";
  if (
    environment.warnings.length || environment.topology.warnings.length ||
    checks.some(check => check.status !== "pass" || check.trust_state !== "verified") ||
    (checks.length > 0 && environment.provenance.freshness_status !== "verified") ||
    (Date.parse(environment.provenance.stale_after) < now) ||
    checks.some(check => Date.parse(check.provenance?.stale_after ?? "") < now) ||
    environment.provenance.freshness_status === "stale" || environment.trust_state === "stale"
  ) return "warning";
  // Lane provenance is verified only after health and current deployment identity both pass.
  if (checks.length && environment.provenance.freshness_status === "verified" &&
      !["missing", "unsupported"].includes(environment.trust_state)) return "verified";
  if (environment.provenance.freshness_status === "missing") return "missing";
  return environment.trust_state === "verified" ? "recorded" : environment.trust_state;
}
