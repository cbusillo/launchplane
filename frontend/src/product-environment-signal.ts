import type { DataProvenance, ProductEnvironmentDetail, ProductEnvironmentSummary, ProductSiteOverview } from "./generated/openapi.ts";

function expireProvenance(provenance: DataProvenance, now: number): DataProvenance {
  return ["verified", "recorded"].includes(provenance.freshness_status) && Date.parse(provenance.stale_after) < now
    ? { ...provenance, freshness_status: "stale" } : provenance;
}

type Topology = ProductEnvironmentSummary["topology"];
type Evidence = { trust_state: DataProvenance["freshness_status"]; provenance: DataProvenance };
export type EnvironmentEvidence = Pick<ProductEnvironmentSummary, "provenance" | "health_monitoring" | "topology" | "lane_status">;

function expireEvidence<T extends Evidence>(evidence: T, now: number): T {
  const provenance = expireProvenance(evidence.provenance, now);
  return provenance === evidence.provenance ? evidence : { ...evidence, provenance, trust_state: "stale" };
}

function publicTopologyEvidence(topology: Topology): Evidence[] {
  const recorded = topology.provider_recorded;
  return [recorded, recorded.ingress, recorded.tls,
    topology.observed.ingress, ...topology.observed.tls_domains.filter(domain => !domain.advisory)];
}

function publicTopologyApplicable(topology: Topology): boolean {
  // #3189 / PR #3215 owns this forthcoming server field. Older replies omit it.
  // Consume it when present without inferring intent from private monitoring.
  return !("public_website" in topology.desired && topology.desired.public_website === "none");
}

export function environmentEvidenceDeadlines(environment: EnvironmentEvidence): string[] {
  const deadlines = [environment.provenance, ...environment.health_monitoring.checks.map(check => check.provenance),
    environment.topology.provider_recorded.placement.provenance,
    environment.topology.observed.placement.provenance,
    ...publicTopologyEvidence(environment.topology).map(evidence => evidence.provenance),
  ].map(provenance => provenance.stale_after);
  return [...deadlines, ...(environment.lane_status?.transitions.map(transition => transition.after) ?? [])];
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
    [topology.observed.ingress, ...topology.observed.tls_domains.filter(domain => !domain.advisory)].some(evidence => evidence.trust_state === "stale"));
  return {
    ...environment, provenance,
    trust_state: provenance.freshness_status === "stale" ? "stale" : expiredAggregate(environment.trust_state, topology.trust_state === "stale"),
    health_monitoring: {
      ...environment.health_monitoring,
      checks,
    },
    topology,
  };
}

export function expireProductEvidence(product: ProductSiteOverview, now = Date.now()): ProductSiteOverview {
  const environments = product.environments.map(environment => expireEnvironmentEvidence(environment, now));
  return { ...product, environments, preview: expireEvidence(product.preview, now),
    trust_state: environments.some(environment => environment.trust_state === "stale") ? "stale" : product.trust_state,
  };
}

export type SignalTone = ProductEnvironmentSummary["trust_state"] | "warning" | "danger";

export type LaneStatusEvidence = Partial<Pick<ProductEnvironmentSummary, "lane_status">>;

export function laneSignal(environment: LaneStatusEvidence | null, now = Date.now()) {
  const status = environment?.lane_status;
  if (!status) return null;
  let signal = status.current;
  for (const transition of status.transitions) {
    if (Date.parse(transition.after) < now) signal = transition.signal;
  }
  return signal;
}

export function publicObservationGap(environment: ProductEnvironmentSummary): string | null {
  return laneSignal(environment)?.public_observation_gap ?? null;
}

export function operationalWarningReasons(environment: LaneStatusEvidence | null, now = Date.now()): string[] {
  return laneSignal(environment, now)?.warning_reasons ?? [];
}

export function operationalAttentionReasons(environment: LaneStatusEvidence | null, now = Date.now()): string[] {
  return laneSignal(environment, now)?.attention_reasons ?? [];
}

export function environmentReleaseHealthy(environment: LaneStatusEvidence | null, now = Date.now()): boolean {
  return laneSignal(environment, now)?.release_healthy ?? false;
}

export const deployHoldNotices: Record<NonNullable<ProductEnvironmentSummary["deploy_hold"]>, string> = {
  isolation_staging: "Deploys held until testing is proven separate from production",
  staff_testing: "Deploys held while staff test",
};

export function deployHoldNotice(environment: ProductEnvironmentSummary | null): string | null {
  return environment?.deploy_hold ? deployHoldNotices[environment.deploy_hold] : null;
}

export function environmentOperationalTone(environment: LaneStatusEvidence | null, now = Date.now()): SignalTone {
  return laneSignal(environment, now)?.tone ?? "missing";
}
