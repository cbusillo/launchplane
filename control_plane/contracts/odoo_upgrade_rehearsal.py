"""An isolated upgrade rehearsal owns copies, never a product's lane binding."""

from datetime import datetime
from enum import StrEnum
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget


ODOO_UPGRADE_REHEARSAL_ROUTE = "/v1/products/{product}/odoo-upgrade-rehearsal"
ODOO_UPGRADE_REHEARSAL_PREFLIGHT_ROUTE = ODOO_UPGRADE_REHEARSAL_ROUTE + "/preflight"
REHEARSAL_PUBLIC_FAILURE_MESSAGES = {
    "rehearsal_storage_capacity_insufficient": "No rehearsal action started. Requalify storage capacity and review a new operation key.",
    "rehearsal_precheck_refused": "No rehearsal action started. Requalify the prerequisite and review a new operation key.",
}
RehearsalAction = Literal["prepare", "execute", "simulate-final", "verify", "retire"]
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
    elapsed_seconds: dict[str, float] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    @property
    def binding_sha256(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json", exclude={"elapsed_seconds"}))


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
