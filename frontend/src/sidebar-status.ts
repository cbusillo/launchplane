import type { ProductEnvironmentSummary } from "./generated/openapi.ts";
import { environmentOperationalTone } from "./product-environment-signal";

// Labels describe returned diagnostic layers; they do not infer a failing proxy
// or app from a generic connection/HTTP failure.
const layerLabels: Record<string, string> = {
  dns: "DNS", tls: "Certificate", runtime_identity: "Runtime identity",
  network: "Network", redirect: "Redirect", http: "HTTP",
  configuration: "Configuration", provider: "Provider", unknown: "Unknown layer",
};

export function sidebarStatus(environment: ProductEnvironmentSummary | null, label: string) {
  const tone = environmentOperationalTone(environment);
  const state = !environment ? "Absent (not recorded)"
    : tone === "verified" ? "Healthy"
    : tone === "danger" ? "Failing"
    : tone === "warning" || tone === "stale" ? "Warning or stale evidence"
    : tone === "unsupported" ? "Unsupported"
    : "Unknown (unverified)";
  const layers = new Set(environment?.health_monitoring.open_incidents
    .map(incident => layerLabels[incident.failure_layer] ?? "Unknown layer"));
  if (environment) {
    for (const check of environment.health_monitoring.checks) {
      if (!check.probe_effective || check.status !== "fail") continue;
      if (check.failure_code === "dns_failure") layers.add("DNS");
      if (check.failure_code.startsWith("tls_")) layers.add("Certificate");
      if (check.failure_code === "wrong_runtime_identity") layers.add("Runtime identity");
    }
    if (["mismatch", "malformed"].includes(environment.topology.observed.placement.runtime_identity_status)) {
      layers.add("Runtime identity");
    }
    if (environment.topology.observed.tls_domains.some(domain =>
      ["expired", "hostname_mismatch", "untrusted", "self_signed", "unreachable", "expiring"].includes(domain.status))) {
      layers.add("Certificate");
    }
  }
  return { tone, description: `${label}: ${state}${layers.size ? `; ${[...layers].join(", ")}` : ""}` };
}
