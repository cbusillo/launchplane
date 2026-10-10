import type { DataProvenance, ProductEnvironmentDetail, ProductEnvironmentSummary, ProductSiteOverview } from "./generated/openapi.ts";

function expireProvenance(provenance: DataProvenance, now: number): DataProvenance {
  return ["verified", "recorded"].includes(provenance.freshness_status) && Date.parse(provenance.stale_after) < now
    ? { ...provenance, freshness_status: "stale" } : provenance;
}

type Topology = ProductEnvironmentSummary["topology"];
type Evidence = { trust_state: DataProvenance["freshness_status"]; provenance: DataProvenance };
type EnvironmentEvidence = Pick<ProductEnvironmentSummary, "provenance" | "health_monitoring" | "topology">;

function expireEvidence<T extends Evidence>(evidence: T, now: number): T {
  const provenance = expireProvenance(evidence.provenance, now);
  return provenance === evidence.provenance ? evidence : { ...evidence, provenance, trust_state: "stale" };
}

function publicTopologyEvidence(topology: Topology): Evidence[] {
  const recorded = topology.provider_recorded;
  return [recorded, recorded.ingress, recorded.tls,
    topology.observed.ingress, ...topology.observed.tls_domains];
}

function publicTopologyApplicable(topology: Topology): boolean {
  // #3189 / PR #3215 owns this forthcoming server field. Older replies omit it.
  // Consume it when present without inferring intent from private monitoring.
  return !("public_website" in topology.desired && topology.desired.public_website === "none");
}

export function environmentEvidenceDeadlines(environment: EnvironmentEvidence): string[] {
  return [environment.provenance, ...environment.health_monitoring.checks.map(check => check.provenance),
    environment.topology.provider_recorded.placement.provenance,
    environment.topology.observed.placement.provenance,
    ...publicTopologyEvidence(environment.topology).map(evidence => evidence.provenance),
  ].map(provenance => provenance.stale_after);
}

function expiredAggregate(state: DataProvenance["freshness_status"], expired: boolean): DataProvenance["freshness_status"] {
  return expired && ["verified", "recorded"].includes(state) ? "stale" : state;
}

export function expireEnvironmentEvidence<T extends ProductEnvironmentSummary | ProductEnvironmentDetail>(environment: T, now = Date.now()): T {
  const provenance = expireProvenance(environment.provenance, now);
  const sourceTopology = environment.topology;
  const recorded = sourceTopology.provider_recorded;
  const placement = expireEvidence(sourceTopology.observed.placement, now);
  const checks = environment.health_monitoring.checks.map(check => expireEvidence(check, now));
  const topology: Topology = {
    ...sourceTopology,
    provider_recorded: {
      ...expireEvidence(recorded, now),
      placement: expireEvidence(recorded.placement, now),
      ingress: expireEvidence(recorded.ingress, now),
      tls: expireEvidence(recorded.tls, now),
    },
    observed: {
      ...sourceTopology.observed,
      placement,
      ingress: expireEvidence(sourceTopology.observed.ingress, now),
      tls_domains: sourceTopology.observed.tls_domains.map(domain => expireEvidence(domain, now)),
    },
  };
  const publicExpired = publicTopologyApplicable(topology) && publicTopologyEvidence(topology).some(evidence => evidence.trust_state === "stale");
  const placementExpired = placement.trust_state === "stale";
  topology.trust_state = expiredAggregate(topology.trust_state, publicExpired || placementExpired);
  topology.observed.trust_state = expiredAggregate(topology.observed.trust_state,
    placementExpired || publicTopologyApplicable(topology) &&
    [topology.observed.ingress, ...topology.observed.tls_domains].some(evidence => evidence.trust_state === "stale"));
  return {
    ...environment, provenance,
    trust_state: provenance.freshness_status === "stale" ? "stale" : environment.trust_state,
    health_monitoring: {
      ...environment.health_monitoring,
      checks,
      trust_state: checks.some(check => check.probe_effective && check.trust_state === "stale")
        ? "stale" : environment.health_monitoring.trust_state,
    },
    topology,
  };
}

export function expireProductEvidence(product: ProductSiteOverview, now = Date.now()): ProductSiteOverview {
  const environments = product.environments.map(environment => expireEnvironmentEvidence(environment, now));
  return { ...product, environments,
    trust_state: environments.some(environment => environment.trust_state === "stale") ? "stale" : product.trust_state,
  };
}

export type SignalTone = ProductEnvironmentSummary["trust_state"] | "warning" | "danger";

export function environmentOperationalTone(environment: ProductEnvironmentSummary | null, now = Date.now()): SignalTone {
  if (!environment) return "missing";
  environment = expireEnvironmentEvidence(environment, now);
  const checks = environment.health_monitoring.checks.filter(check => check.probe_effective);
  const negativeTlsStates = new Set([
    "expired", "hostname_mismatch", "untrusted", "self_signed", "unreachable",
  ]);
  if (
    environment.health_monitoring.open_incidents.length > 0 ||
    checks.some(check => check.status === "fail" || check.incident_status === "open") ||
    ["mismatch", "malformed"].includes(
      environment.topology.observed.placement?.runtime_identity_status ?? "unchecked",
    ) ||
    environment.topology.warnings.some(warning => warning.severity === "error") ||
    environment.topology.observed.tls_domains.some(domain => negativeTlsStates.has(domain.status))
  ) return "danger";
  if (
    environment.warnings.length || environment.topology.warnings.length ||
    environment.topology.observed.placement.trust_state === "stale" ||
    publicTopologyApplicable(environment.topology) && publicTopologyEvidence(environment.topology).some(evidence => evidence.trust_state === "stale") ||
    checks.some(check => check.status !== "pass" || check.trust_state !== "verified") ||
    checks.length > 0 && environment.provenance.freshness_status !== "verified" ||
    Date.parse(environment.provenance.stale_after) < now ||
    checks.some(check => Date.parse(check.provenance?.stale_after ?? "") < now) ||
    environment.provenance.freshness_status === "stale" || environment.trust_state === "stale"
  ) return "warning";
  // Lane provenance is verified only after health and current deployment identity both pass.
  if (checks.length && environment.provenance.freshness_status === "verified" &&
      !["missing", "unsupported"].includes(environment.trust_state)) return "verified";
  if (environment.provenance.freshness_status === "missing") return "missing";
  return environment.trust_state === "verified" ? "recorded" : environment.trust_state;
}
