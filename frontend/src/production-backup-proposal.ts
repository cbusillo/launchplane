import type { ProductionBackupAuthorityWriteEnvelope, ProductEnvironmentDetail } from "./generated/openapi.ts";

type ObjectValue = Record<string, unknown>;
function object(value: unknown, fields: string[]): ObjectValue {
  if (!value || typeof value !== "object" || Array.isArray(value) ||
      Object.keys(value).some(key => !fields.includes(key))) throw new Error("Use a typed backup-authority proposal without credentials or extra fields.");
  return value as ObjectValue;
}
const recordFields = ["schema_version", "record_id", "status", "effective_at", "review_after", "source", "reason", "supersedes_record_id"];

export function readBackupProposal(text: string, lane: Pick<ProductEnvironmentDetail, "product" | "context" | "environment">): ProductionBackupAuthorityWriteEnvelope {
  const value = object(JSON.parse(text), ["schema_version", "mode", "targets", "policy", "expected_current_target_record_ids", "expected_current_policy_record_id", "reviewed_authority_digest"]);
  const policy = object(value.policy, [...recordFields, "policy_id", "policy_digest", "product", "context", "instance", "promotion_action", "policy_revision", "fast_snapshot", "independent_backup"]);
  if (policy.product !== lane.product || policy.context !== lane.context || policy.instance !== lane.environment || lane.environment !== "prod") {
    throw new Error("This proposal belongs to a different product or production lane.");
  }
  if (policy.status && policy.status !== "active") throw new Error("This card approves active backup protection only.");
  if (typeof policy.promotion_action !== "string" || !policy.promotion_action.trim() ||
      typeof policy.review_after !== "string" || typeof policy.effective_at !== "string" ||
      typeof policy.reason !== "string" || !policy.reason.trim()) throw new Error("The proposal is missing policy scope, dates or a reason.");
  const snapshot = object(policy.fast_snapshot, ["operation", "source_target_id", "snapshot_prefix", "retention_count", "max_evidence_age_seconds"]);
  const backup = object(policy.independent_backup, ["operation", "source_target_id", "destination_target_id", "max_evidence_age_seconds"]);
  if (typeof snapshot.source_target_id !== "string" || typeof snapshot.snapshot_prefix !== "string" ||
      !Number.isInteger(snapshot.retention_count) || typeof snapshot.max_evidence_age_seconds !== "number" ||
      backup.source_target_id !== snapshot.source_target_id || typeof backup.destination_target_id !== "string" ||
      typeof backup.max_evidence_age_seconds !== "number") throw new Error("The proposal must include a snapshot and independent backup of the same source.");
  if (!Array.isArray(value.targets)) throw new Error("The proposal must include its target revisions (an empty list is allowed for a policy-only update).");
  for (const item of value.targets) {
    const target = object(item, [...recordFields, "target_id", "target_revision", "destination", "target_digest"]);
    if (typeof target.target_id !== "string" || (target.status && target.status !== "active")) throw new Error("The proposal needs active, named targets.");
    const destination = object(target.destination, ["provider_type", "destination_kind", "host", "username", "guest_kind", "guest_id", "storage_id"]);
    if (typeof destination.host !== "string" || typeof destination.username !== "string" ||
        !["proxmox_guest", "proxmox_storage"].includes(String(destination.destination_kind))) throw new Error("Each target needs an explicit backup destination.");
    if (destination.destination_kind === "proxmox_guest" &&
        (!["lxc", "qemu"].includes(String(destination.guest_kind)) || typeof destination.guest_id !== "string")) throw new Error("The source guest is missing.");
    if (destination.destination_kind === "proxmox_storage" && typeof destination.storage_id !== "string") throw new Error("The independent backup storage is missing.");
  }
  return { ...value, mode: "dry_run", reviewed_authority_digest: "" } as ProductionBackupAuthorityWriteEnvelope;
}

export function backupProposalStorageKey(scope: string): string { return `launchplane:backup-authority-proposal:${scope}`; }
