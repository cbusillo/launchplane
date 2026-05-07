import { formatTime, labelForStatus } from "./format";
import {
  artifactFromLane,
  releaseIdentityFromLane,
  shorten,
  worstStatus,
} from "./laneSummary";
import { KeyValue, MetricTile, PanelHead } from "./panel-ui";
import { SkeletonRows, StatusPill } from "./status-ui";
import { TrustBadge } from "./TrustBadge";

import type { LaneSummary } from "./types";

type LaneKind = "prod" | "testing";

export function LanePanel({
  title,
  laneKind,
  lane,
  loading,
}: {
  title: string;
  laneKind: LaneKind;
  lane: LaneSummary | null;
  loading: boolean;
}) {
  const artifact = artifactFromLane(lane);
  const deployStatus =
    lane?.latest_deployment?.deploy.status ??
    lane?.inventory?.deploy.status ??
    "unknown";
  const healthStatus =
    lane?.inventory?.destination_health.status ??
    lane?.latest_deployment?.destination_health.status ??
    "unknown";
  const backupStatus = lane?.latest_backup_gate?.status ?? "unknown";
  const settingsStatus = lane?.odoo_instance_override ? "pass" : "unknown";
  const updatedAt =
    lane?.inventory?.updated_at ??
    lane?.latest_deployment?.deploy.finished_at ??
    "";
  const targetName =
    lane?.latest_deployment?.deploy.target_name ??
    lane?.inventory?.deploy.target_name ??
    "";
  const releaseIdentity = releaseIdentityFromLane(lane);

  return (
    <section className={`panel lane-panel lane-${laneKind}`}>
      <PanelHead
        eyebrow="environment lane"
        title={title}
        right={
          <div className="panel-badges">
            <TrustBadge provenance={lane?.provenance ?? null} />
            <StatusPill status={worstStatus([deployStatus, healthStatus])} />
          </div>
        }
      />
      {loading ? (
        <SkeletonRows />
      ) : (
        <div className="lane-body">
          <div className="lane-release">
            <span className={`lane-chip lane-chip-${laneKind}`}>
              {laneKind}
            </span>
            <code>{releaseIdentity || "release unknown"}</code>
          </div>
          <div className="lane-metrics">
            <MetricTile
              label="Deploy"
              status={deployStatus}
              value={labelForStatus(deployStatus)}
            />
            <MetricTile
              label="Health"
              status={healthStatus}
              value={labelForStatus(healthStatus)}
            />
            <MetricTile
              label="Backup"
              status={backupStatus}
              value={labelForStatus(backupStatus)}
            />
            <MetricTile
              label="Settings"
              status={settingsStatus}
              value={labelForStatus(settingsStatus)}
            />
          </div>
          <KeyValue label="Artifact" value={artifact} mono muted={!artifact} />
          <KeyValue
            label="Target"
            value={targetName}
            mono
            muted={!targetName}
          />
          <KeyValue
            label="Source"
            value={shorten(
              lane?.inventory?.source_git_ref ??
                lane?.latest_deployment?.source_git_ref ??
                "",
            )}
            mono
          />
          <KeyValue
            label="Deployment"
            value={
              lane?.latest_deployment?.record_id ??
              lane?.inventory?.deployment_record_id ??
              ""
            }
            mono
          />
          <KeyValue
            label="Promotion"
            value={
              lane?.latest_promotion?.record_id ??
              lane?.inventory?.promotion_record_id ??
              ""
            }
            mono
            muted={
              !lane?.latest_promotion && !lane?.inventory?.promotion_record_id
            }
          />
          <KeyValue label="Updated" value={formatTime(updatedAt)} mono />
          <EvidenceStrip lane={lane} laneKind={laneKind} />
        </div>
      )}
    </section>
  );
}

function EvidenceStrip({
  lane,
  laneKind,
}: {
  lane: LaneSummary | null;
  laneKind: LaneKind;
}) {
  const backup = lane?.latest_backup_gate?.status ?? "unknown";
  const promotion = lane?.latest_promotion?.deploy.status ?? "unknown";
  return (
    <div className="evidence-strip">
      <span className={`lane-chip lane-chip-${laneKind}`}>{laneKind}</span>
      <StatusPill status={backup} />
      <StatusPill status={promotion} />
    </div>
  );
}
