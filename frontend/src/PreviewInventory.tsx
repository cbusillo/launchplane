import { Archive, Eye } from "lucide-react";

import { formatTime } from "./format";
import { KeyValue, PanelHead } from "./panel-ui";
import { SkeletonRows, StateBlock, StatusPill } from "./status-ui";
import { TrustBadge } from "./TrustBadge";

import type { DataProvenance, DriverDescriptor, PreviewSummary } from "./types";

export function PreviewInventory({
  driver,
  previews,
  inventoryProvenance,
  loading,
}: {
  driver: DriverDescriptor | null;
  previews: PreviewSummary[];
  inventoryProvenance: DataProvenance | null;
  loading: boolean;
}) {
  const previewCapabilityId = previewInventoryCapabilityId(driver);
  const exposesPreviews = Boolean(previewCapabilityId);
  const latestPreview = previews
    .slice()
    .sort((left, right) => {
      const leftTime =
        left.latest_generation?.finished_at ?? left.preview.updated_at;
      const rightTime =
        right.latest_generation?.finished_at ?? right.preview.updated_at;
      return rightTime.localeCompare(leftTime);
    })
    .at(0);

  return (
    <section className="panel preview-panel">
      <PanelHead
        eyebrow="preview lane"
        title={exposesPreviews ? "Preview inventory" : "Previews not exposed"}
        right={
          <div className="panel-badges">
            <TrustBadge
              provenance={previewInventoryProvenance(
                exposesPreviews,
                latestPreview,
                inventoryProvenance,
              )}
            />
            {exposesPreviews ? (
              <span className="count-chip">{previews.length} active</span>
            ) : null}
          </div>
        }
      />
      {loading ? (
        <SkeletonRows />
      ) : !exposesPreviews ? (
        <StateBlock
          icon={<Eye size={18} />}
          title="Driver does not expose preview lifecycle"
        />
      ) : previews.length === 0 ? (
        <StateBlock icon={<Archive size={18} />} title="No active previews" />
      ) : (
        <div className="preview-list">
          {previews.map((summary) => {
            const health =
              summary.latest_generation?.overall_health_status ?? "unknown";
            return (
              <article className="preview-row" key={summary.preview.preview_id}>
                <div>
                  <strong>{summary.preview.preview_label}</strong>
                  <a href={summary.preview.anchor_pr_url}>
                    {summary.preview.anchor_repo}#
                    {summary.preview.anchor_pr_number}
                  </a>
                </div>
                <code>
                  {summary.latest_generation?.artifact_id ??
                    summary.preview.preview_id}
                </code>
                <div className="preview-status-stack">
                  <TrustBadge provenance={summary.provenance} compact />
                  <StatusPill status={health} />
                </div>
              </article>
            );
          })}
        </div>
      )}
      <div className="preview-footer">
        <KeyValue
          label="Capability"
          value={previewCapabilityId || "not exposed by driver"}
          mono
          status={exposesPreviews ? "pass" : "unknown"}
        />
        <KeyValue
          label="Latest"
          value={
            latestPreview
              ? formatTime(
                  latestPreview.latest_generation?.finished_at ??
                    latestPreview.preview.updated_at,
                )
              : "unknown"
          }
          mono
          muted={!latestPreview}
        />
      </div>
    </section>
  );
}

function previewInventoryCapabilityId(driver: DriverDescriptor | null): string {
  const previewCapabilityIds = new Set([
    "previewable",
    "preview_inventory_managed",
    "preview_lifecycle",
  ]);
  return (
    driver?.capabilities.find((capability) => {
      return (
        previewCapabilityIds.has(capability.capability_id) ||
        capability.panels.includes("preview_inventory")
      );
    })?.capability_id ?? ""
  );
}

function previewInventoryProvenance(
  exposesPreviews: boolean,
  latestPreview: PreviewSummary | undefined,
  inventoryProvenance: DataProvenance | null,
): DataProvenance {
  if (!exposesPreviews) {
    return {
      source_kind: "unsupported",
      source_record_id: "",
      recorded_at: "",
      refreshed_at: "",
      freshness_status: "unsupported",
      stale_after: "",
      detail: "Driver does not expose preview lifecycle.",
    };
  }
  if (inventoryProvenance) {
    return inventoryProvenance;
  }
  if (!latestPreview) {
    return {
      source_kind: "record",
      source_record_id: "",
      recorded_at: "",
      refreshed_at: "",
      freshness_status: "missing",
      stale_after: "",
      detail:
        "Launchplane has not recorded active preview inventory for this context.",
    };
  }
  return latestPreview.provenance;
}
