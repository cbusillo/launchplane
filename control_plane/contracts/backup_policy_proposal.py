"""Private proposal input and allowlisted browser evidence for backup policies."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from control_plane.production_backup_authority import ProductionBackupAuthorityWriteEnvelope

BACKUP_POLICY_DESCRIPTOR = "production-backup-policy"


class BackupPolicyProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    authority: ProductionBackupAuthorityWriteEnvelope
    reason: str = Field(min_length=1, max_length=4000)
    related_issue: str = ""

    @model_validator(mode="after")
    def validate_inert_input(self) -> "BackupPolicyProposalInput":
        if self.authority.mode != "dry_run" or self.authority.reviewed_authority_digest:
            raise ValueError("Backup proposals accept only inert, unreviewed dry-run input.")
        if self.authority.policy.status != "active" or any(
            target.status != "active" for target in self.authority.targets
        ):
            raise ValueError("Backup protection proposals require active policy and targets.")
        if not {target.target_id for target in self.authority.targets} <= set(
            self.authority.policy.target_ids
        ):
            raise ValueError("Every proposed target must be used by this backup policy.")
        return self


class BackupPolicyTargetChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    destination_kind: Literal["proxmox_guest", "proxmox_storage"]
    target_revision: int
    change: Literal["new", "revised", "unchanged"]


class BackupPolicyHumanEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    plan_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    product: str
    context: str
    instance: str
    promotion_action: str
    policy_revision: int
    retention_count: int
    targets: tuple[BackupPolicyTargetChange, ...]
    snapshot_max_age_seconds: int
    backup_max_age_seconds: int
    effective_at: str
    review_after: str
    result_status: Literal["ok"] = "ok"


class BackupPolicyExecutionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    result_status: Literal["ok", "error"]
    result_digest: str
    reconciliation_required: bool = False
    failure_code: str = ""


class BackupPolicyProposalSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: str
    descriptor_id: Literal["production-backup-policy"] = "production-backup-policy"
    descriptor_version: int = 1
    result_status: Literal["ok"] = "ok"
    status: str
    created_at: str
    expires_at: str
    review_path: str
