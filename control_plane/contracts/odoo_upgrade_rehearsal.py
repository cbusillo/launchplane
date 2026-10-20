"""An isolated upgrade rehearsal owns copies, never a product's lane binding."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane._odoo_rehearsal_attachment_validation import ATTACHMENT_METADATA_LIMITS
from control_plane._odoo_rehearsal_diagnostics import (
    REHEARSAL_DIAGNOSTIC_TEXT_LIMIT,
    REHEARSAL_DIAGNOSTIC_FILE_LIMIT,
)
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget


ODOO_UPGRADE_REHEARSAL_ROUTE = "/v1/products/{product}/odoo-upgrade-rehearsal"
ODOO_UPGRADE_REHEARSAL_PREFLIGHT_ROUTE = ODOO_UPGRADE_REHEARSAL_ROUTE + "/preflight"
REHEARSAL_PUBLIC_FAILURE_MESSAGES = {
    "rehearsal_storage_capacity_insufficient": "No rehearsal action started. Requalify storage capacity and review a new operation key.",
    "rehearsal_precheck_refused": "No rehearsal action started. Requalify the prerequisite and review a new operation key.",
}
RehearsalAction = Literal[
    "prepare", "execute", "simulate-final", "verify", "retire", "retire-incomplete"
]
RehearsalState = Literal["planned", "prepared", "upgraded", "switched", "verified", "retired"]
RehearsalBackingRole = Literal[
    "database", "filestore", "runner_scratch", "database_scratch", "host_custody"
]


class RehearsalBackingGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    available_bytes: int = Field(ge=0, strict=True)
    roles: tuple[RehearsalBackingRole, ...] = Field(min_length=1)


class RehearsalPreflightObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observed_at: datetime
    reader_host_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    odoo_version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)*$")
    database_bytes: int = Field(gt=0, strict=True)
    filestore_apparent_bytes: int = Field(ge=0, strict=True)
    filestore_allocated_bytes: int = Field(ge=0, strict=True)
    backing_basis: Literal["allocation-parent"]
    backing_groups: tuple[RehearsalBackingGroup, ...] = Field(min_length=1)
    read_only: Literal[True]

    @model_validator(mode="after")
    def complete(self) -> "RehearsalPreflightObservation":
        roles = [role for group in self.backing_groups for role in group.roles]
        groups = [group.group_id for group in self.backing_groups]
        if (
            self.observed_at.utcoffset() is None
            or set(roles) != set(get_args(RehearsalBackingRole))
            or len(roles) != len(set(roles))
            or len(groups) != len(set(groups))
        ):
            raise ValueError("Preflight requires current, complete backing observations.")
        return self


class RehearsalPreflightResult(RehearsalPreflightObservation):
    product: str = Field(min_length=1)
    source_context: str = Field(min_length=1)
    reader_server_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    source_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RehearsalPrerequisite(StrEnum):
    SOURCE_UNAVAILABLE = "rehearsal_source_unavailable"
    ARTIFACT_UNAVAILABLE = "rehearsal_artifact_unavailable"
    CREDENTIALS_UNAVAILABLE = "rehearsal_credentials_unavailable"
    PLACEMENT_UNAVAILABLE = "rehearsal_placement_unavailable"
    STATE_UNVERIFIED = "rehearsal_state_unverified"
    RECOVERY_REQUIRED = "rehearsal_recovery_required"
    PRECHECK_REFUSED = "rehearsal_precheck_refused"
    OPERATION_UNAVAILABLE = "rehearsal_operation_unavailable"


class OdooUpgradeRehearsalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["dry-run", "apply", "readback"] = "dry-run"
    action: RehearsalAction = "prepare"
    rehearsal_id: str = Field(default="", pattern=r"^(?:lp-rehearsal-[a-f0-9]{32})?$")
    source_context: str = Field(min_length=1)
    source_odoo_version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)*$")
    target_artifact_id: str = Field(min_length=1)
    storage: OdooTestingDrillStorageBudget
    reason: str = Field(min_length=1)
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")

    def intent(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"mode", "action", "reviewed_plan_sha256"})


class RehearsalCopyReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    database: str = Field(
        pattern=r"^lp_rehearsal_[a-f0-9]{32}_(?:source|upgrade|final_source|final_upgrade)$"
    )
    witness_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    database_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    filestore_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_capture_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sanitized: Literal[True]
    odoo_version: str = Field(min_length=1)
    captured_at: datetime

    @model_validator(mode="after")
    def aware(self) -> "RehearsalCopyReceipt":
        if self.captured_at.utcoffset() is None:
            raise ValueError("Copy custody requires an aware capture time.")
        return self


class IncompleteRehearsalHostResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    qualified: Literal[True]
    disposed: bool
    binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    intent_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    attempt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    refused_reviews: tuple[Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")], ...] = ()
    disposal_started: bool = False
    containers_absent: bool
    volumes_absent: bool
    network_absent: bool
    raw_source_absent: bool
    public_route: Literal[False]
    business_host_frozen: Literal[False]

    @model_validator(mode="after")
    def disposal_matches_absence(self) -> "IncompleteRehearsalHostResult":
        if self.disposed != all(
            (
                self.containers_absent,
                self.volumes_absent,
                self.network_absent,
                self.raw_source_absent,
            )
        ):
            raise ValueError("Disposal requires every native absence observation.")
        return self


class IncompleteRehearsalRetirementCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    original_key_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewed_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    host_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    host_disposal: IncompleteRehearsalHostResult | None = None

    @model_validator(mode="after")
    def bound_disposal(self) -> "IncompleteRehearsalRetirementCheckpoint":
        if self.host_disposal is not None and (
            not self.host_disposal.disposed
            or self.host_disposal.binding_sha256 != self.host_binding_sha256
            or self.host_disposal.attempt_sha256 != self.original_key_sha256
        ):
            raise ValueError("Retirement checkpoint requires bound native disposal.")
        return self


class IncompleteRehearsalRetirementReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rehearsal_id: str = Field(pattern=r"^lp-rehearsal-[a-f0-9]{32}$")
    product: str
    state: Literal["retired"]
    source_odoo_version: str
    target_artifact_id: str
    source_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    shape_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    copies: tuple[RehearsalCopyReceipt, ...] = Field(max_length=0)
    elapsed_seconds: dict[str, float]
    resources_preserved: Literal[False]
    public_route: Literal[False]
    business_host_frozen: Literal[False]
    action: Literal["retire-incomplete"]
    completed: Literal[True]
    disposed: Literal[True]
    raw_source_retained: Literal[False]
    original_operation_released: Literal[True]
    recovery_tuples_produced: Literal[False]
    host_disposal: IncompleteRehearsalHostResult

    @model_validator(mode="after")
    def native_disposal(self) -> "IncompleteRehearsalRetirementReceipt":
        if not self.host_disposal.disposed:
            raise ValueError("Retirement requires completed native disposal.")
        return self


class OdooUpgradeRehearsalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rehearsal_id: str = Field(pattern=r"^lp-rehearsal-[a-f0-9]{32}$")
    product: str = Field(min_length=1)
    principal: str = Field(min_length=1)
    intent_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    shape_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    credential_bindings_sha256: str = ""
    shape_environment: dict[str, str] = Field(default_factory=dict)
    upgrade_environment: dict[str, str] = Field(default_factory=dict)
    source_odoo_version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)*$")
    target_artifact: ArtifactIdentityManifest
    server_id: str = Field(min_length=1)
    storage: OdooTestingDrillStorageBudget
    project_id: str = ""
    environment_id: str = ""
    compose_id: str = ""
    state: RehearsalState = "planned"
    copies: tuple[RehearsalCopyReceipt, ...] = ()
    incomplete_retirement: IncompleteRehearsalRetirementCheckpoint | None = None
    elapsed_seconds: dict[str, float] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    @property
    def binding_sha256(self) -> str:
        return canonical_json_sha256(
            self.model_dump(mode="json", exclude={"elapsed_seconds", "incomplete_retirement"})
        )


class RehearsalCustodyObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    effect_started: bool
    raw_database_bytes: int | None = Field(default=None, ge=0)
    raw_filestore_bytes: int | None = Field(default=None, ge=0)
    raw_filestore_extracted: bool
    capture_receipt_present: bool
    capture_pending_present: bool
    source_snapshot_directory_present: bool


class RehearsalHostBusy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    host_busy: Literal[True]


class RehearsalAttachmentFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reason: Literal["attachment_size_mismatch", "attachment_hash_mismatch"]
    store_fname: str = Field(pattern=r"^[a-f0-9]{2}/[a-f0-9]{40}$")
    name: str | None = Field(max_length=ATTACHMENT_METADATA_LIMITS["name"])
    res_model: str | None = Field(max_length=ATTACHMENT_METADATA_LIMITS["res_model"])
    res_id: int | None
    mimetype: str | None = Field(max_length=ATTACHMENT_METADATA_LIMITS["mimetype"])
    database_size: int = Field(ge=0)
    database_checksum: str = Field(pattern=r"^[a-f0-9]{40}$")
    file_size: int = Field(ge=0)
    file_checksum: str | None = Field(pattern=r"^[a-f0-9]{40}$")
    file_status: Literal["measured", "unreadable", "changed"]
    truncated_fields: list[Literal["name", "res_model", "mimetype"]]

    @model_validator(mode="after")
    def compared_facts(self) -> "RehearsalAttachmentFacts":
        if (
            self.store_fname != self.database_checksum[:2] + "/" + self.database_checksum
            or len(self.truncated_fields) != len(set(self.truncated_fields))
            or (self.file_status == "measured") != (self.file_checksum is not None)
            or (self.file_status != "measured" and self.reason != "attachment_size_mismatch")
            or (self.reason == "attachment_size_mismatch" and self.database_size == self.file_size)
            or (
                self.reason == "attachment_hash_mismatch"
                and (
                    self.database_size != self.file_size
                    or self.database_checksum == self.file_checksum
                )
            )
        ):
            raise ValueError("Attachment evidence must describe its observed mismatch.")
        return self


class RehearsalAttachmentMismatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    attempt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    record_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    attachment: RehearsalAttachmentFacts


class RehearsalChildFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    attempt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    record_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    step: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    exit_status: int = Field(strict=True)
    error_text: str = Field(max_length=REHEARSAL_DIAGNOSTIC_TEXT_LIMIT)

    @model_validator(mode="after")
    def failed(self) -> "RehearsalChildFailure":
        if self.exit_status == 0:
            raise ValueError("Child failure requires a nonzero exit status.")
        return self


class RehearsalRetainedLog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: str = Field(pattern=r"^(?:[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+\.log(?:\.[0-9]+)?$")
    bytes: int = Field(ge=0, strict=True)
    modified_at: datetime
    tail: str = Field(max_length=REHEARSAL_DIAGNOSTIC_TEXT_LIMIT)


class RehearsalRuntimeLog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: Literal["script-runner", "database"]
    available: bool
    tail: str = Field(max_length=REHEARSAL_DIAGNOSTIC_TEXT_LIMIT)


class RehearsalRetainedLogs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: list[RehearsalRetainedLog] = Field(max_length=REHEARSAL_DIAGNOSTIC_FILE_LIMIT)
    runtime: list[RehearsalRuntimeLog] = Field(max_length=2)
    since: datetime
    truncated: bool


class RehearsalHostResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    state: RehearsalState
    copies: tuple[RehearsalCopyReceipt, ...]
    elapsed_seconds: dict[str, float]
    resources_preserved: Literal[True]
    public_route: Literal[False]
    business_host_frozen: Literal[False]
    completed: bool = True
    raw_source_retained: bool = False
    prepare_not_started: bool = False
    custody: RehearsalCustodyObservation | None = None
    attachment_mismatch: RehearsalAttachmentMismatch | None = None
    child_failure: RehearsalChildFailure | None = None
    retained_upgrade_logs: RehearsalRetainedLogs | None = None
    capture_attachment_validation: (
        Literal[
            "dependency_unavailable",
            "environment_unavailable",
            "database_unavailable",
            "attachment_query_unavailable",
            "attachment_metadata_invalid",
            "attachment_path_unsafe",
            "attachment_missing",
            "attachment_unreadable",
            "attachment_size_mismatch",
            "attachment_hash_mismatch",
            "attachment_size_and_hash_mismatch",
            "attachment_content_matches_checksum_size_mismatch",
            "capture_attachment_matches_reference_copy_differs",
            "capture_attachment_checksum_matches_size_metadata_differs",
            "capture_attachment_matches_copy_reference_differs",
            "capture_attachment_differs_from_copy_and_reference",
            "capture_attachment_not_present",
            "capture_comparison_unavailable",
            "capture_comparison_incomplete",
            "verified",
            "incomplete",
            "writer_active",
            "runtime_unavailable",
        ]
        | None
    ) = None
    capture_database_reference_validation: (
        Literal["reference_present", "reference_not_found", "unavailable", "incomplete"] | None
    ) = None
    refusal_code: Literal[
        "", "rehearsal_storage_capacity_insufficient", "rehearsal_precheck_refused"
    ] = ""
    attempt_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")


def rehearsal_identity(product: str, principal: str, key: str) -> str:
    if not key.strip():
        raise ValueError("Planning requires the original stable key.")
    return (
        "lp-rehearsal-"
        + canonical_json_sha256({"product": product, "principal": principal, "key": key.strip()})[
            :32
        ]
    )
