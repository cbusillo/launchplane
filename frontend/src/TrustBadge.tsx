import { formatTime } from "./format";

import type { DataProvenance, FreshnessStatus } from "./types";

export function TrustBadge({
  provenance,
  compact = false,
}: {
  provenance: DataProvenance | null;
  compact?: boolean;
}) {
  const status = provenance?.freshness_status ?? "missing";
  return (
    <span
      className={`trust-badge trust-${status}${compact ? " trust-compact" : ""}`}
      title={provenanceDetail(provenance)}
      data-freshness={status}
    >
      <span>{freshnessLabel(status)}</span>
      {!compact ? <em>{provenanceSubLabel(provenance)}</em> : null}
    </span>
  );
}

export function freshnessLabel(status: FreshnessStatus): string {
  if (status === "verified") {
    return "verified";
  }
  if (status === "recorded") {
    return "recorded";
  }
  if (status === "stale") {
    return "stale";
  }
  if (status === "unsupported") {
    return "unsupported";
  }
  return "missing";
}

function provenanceSubLabel(provenance: DataProvenance | null): string {
  if (!provenance) {
    return "no evidence";
  }
  if (provenance.freshness_status === "unsupported") {
    return provenance.source_kind;
  }
  if (provenance.refreshed_at) {
    return formatTime(provenance.refreshed_at);
  }
  if (provenance.recorded_at) {
    return formatTime(provenance.recorded_at);
  }
  return provenance.source_record_id ? "recorded" : "no evidence";
}

function provenanceDetail(provenance: DataProvenance | null): string {
  if (!provenance) {
    return "Launchplane has not recorded provenance for this surface.";
  }
  const parts = [provenance.detail];
  if (provenance.source_record_id) {
    parts.push(`source: ${provenance.source_record_id}`);
  }
  if (provenance.stale_after) {
    parts.push(`stale after: ${formatTime(provenance.stale_after)}`);
  }
  return parts.filter(Boolean).join(" | ");
}
