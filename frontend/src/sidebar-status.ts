import type { ProductEnvironmentSummary } from "./generated/openapi.ts";
import { environmentOperationalTone, negativeTlsStates, failedRuntimeIdentityStates, expireEnvironmentEvidence, publicObservationGap } from "./product-environment-signal";

// Labels describe returned diagnostic layers; they do not infer a failing proxy
// or app from a generic connection/HTTP failure.
const layerLabels: Record<string, string> = {
  dns: "DNS", tls: "Certificate", runtime_identity: "Runtime identity",
  network: "Network", redirect: "Redirect", http: "HTTP",
  configuration: "Configuration", provider: "Provider", unknown: "Unknown layer",
};

export function sidebarLayerLabel(layer: string): string {
  return layerLabels[layer] ?? "Unknown layer";
}

export function sidebarStatus(environment: ProductEnvironmentSummary | null, label: string, staleDescription = "") {
  if (staleDescription) return { tone: "stale" as const, description: `${label}: ${staleDescription}` };
  environment = environment ? expireEnvironmentEvidence(environment) : null;
  const tone = environmentOperationalTone(environment);
  const evidenceStale = environment && (environment.provenance.freshness_status === "stale" ||
    environment.topology.observed.placement.trust_state === "stale" ||
    environment.health_monitoring.checks.some(check => check.probe_effective && check.trust_state === "stale") ||
    environment.topology.desired.public_website !== "none" && (
      environment.topology.observed.ingress.trust_state === "stale" ||
      environment.topology.observed.tls_domains.some(domain => domain.trust_state === "stale")));
  if (tone === "stale" || tone === "warning" && evidenceStale) {
    return { tone: "stale" as const, description: `${label}: Stale evidence; awaiting current observations` };
  }
  const state = !environment ? "Absent (not recorded)"
    : tone === "verified" ? "Healthy"
    : tone === "danger" ? "Alert"
    : tone === "warning" ? "Warning"
    : tone === "unsupported" ? "Unsupported"
    : tone === "recorded" ? "Unknown (recorded, not verified)"
    : "Unknown (unverified)";
  const layers = new Set(environment?.health_monitoring.open_incidents
    .map(incident => sidebarLayerLabel(incident.failure_layer)));
  if (environment) {
    for (const check of environment.health_monitoring.checks) {
      if (!check.probe_effective || check.status !== "fail") continue;
      if (check.failure_code === "dns_failure") layers.add(sidebarLayerLabel("dns"));
      if (check.failure_code.startsWith("tls_")) layers.add(sidebarLayerLabel("tls"));
      if (check.failure_code === "wrong_runtime_identity") layers.add(sidebarLayerLabel("runtime_identity"));
    }
    if (failedRuntimeIdentityStates.has(environment.topology.observed.placement.runtime_identity_status)) {
      layers.add(sidebarLayerLabel("runtime_identity"));
    }
    if (environment.topology.observed.tls_domains.some(domain =>
      negativeTlsStates.has(domain.status))) {
      layers.add(sidebarLayerLabel("tls"));
    }
  }
  const reasons = new Set<string>();
  if (tone === "warning" && environment) {
    const gap = publicObservationGap(environment.topology);
    if (gap) reasons.add(gap);
    if (environment.topology.observed.placement.runtime_identity_status !== "match") reasons.add("Runtime identity unverified");
    environment.topology.warnings.filter(warning => warning.severity === "warning")
      .forEach(warning => reasons.add(warning.detail));
    environment.warnings.forEach(warning => reasons.add(warning));
    if (!reasons.size) reasons.add("Health or runtime evidence not yet verified");
  }
  return { tone, description: `${label}: ${state}${layers.size ? `; ${[...layers].join(", ")}` : ""}${reasons.size ? `; ${[...reasons].join("; ")}` : ""}` };
}
