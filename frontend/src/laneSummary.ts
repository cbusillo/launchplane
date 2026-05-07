import type { LaneSummary, Status } from "./types";

export function artifactFromLane(lane: LaneSummary | null): string {
  return (
    lane?.inventory?.artifact_identity?.artifact_id ??
    lane?.latest_deployment?.artifact_identity?.artifact_id ??
    lane?.latest_promotion?.artifact_identity?.artifact_id ??
    ""
  );
}

export function sourceRefFromLane(lane: LaneSummary | null): string {
  return (
    lane?.inventory?.source_git_ref ??
    lane?.latest_deployment?.source_git_ref ??
    ""
  );
}

export function releaseIdentityFromLane(lane: LaneSummary | null): string {
  if (lane?.release_tuple?.tuple_id) {
    return lane.release_tuple.tuple_id;
  }
  const artifact = artifactFromLane(lane);
  if (!artifact) {
    return "";
  }
  return artifact.includes("@")
    ? (artifact.split("@").at(-1) ?? artifact)
    : artifact;
}

export function worstStatus(statuses: Array<Status | string>): Status | string {
  if (statuses.includes("fail")) {
    return "fail";
  }
  if (statuses.includes("pending")) {
    return "pending";
  }
  if (statuses.every((status) => status === "pass")) {
    return "pass";
  }
  return "unknown";
}

export function shorten(value: string): string {
  if (value.length <= 14) {
    return value;
  }
  return `${value.slice(0, 7)}...${value.slice(-4)}`;
}
