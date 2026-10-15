"""Closed internal effect/worker contracts. None are HTTP execution capabilities."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self, TypeAlias, cast
from dataclasses import fields
import json

from pydantic import Field, TypeAdapter, field_validator, model_validator

from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget, StrictFrozenModel
from control_plane.contracts.merge_train_effect import (
    MergeTrainEffectLineage,
    CandidateRefPrepareEffect,
    CandidateHeadMergeEffect,
    PullRequestHeadRefreshEffect,
    StackChildMergeEffect,
    PullRequestLandingEffect,
    StackChildCommentEffect,
    StackChildLabelEffect,
    StackChildCloseEffect,
    CandidateRefDeleteEffect,
)


from control_plane.contracts.ordinary_agent_snapshot import (
    OrdinaryAgentMergeTrainSnapshotResult,
    OrdinaryAgentCandidateCheckResult,
    OrdinaryAgentProviderRequestCounts,
    OrdinaryAgentReadmissionObservation,
)
from control_plane.contracts.ordinary_agent_qualification import (
    OrdinaryAgentQualificationAttestation,
    OrdinaryAgentQualificationSetup,
    OrdinaryRepositoryAdminObservation,
)


Identifier = Annotated[str, Field(min_length=1, max_length=256)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Epoch = Annotated[int, Field(ge=0, le=2**63 - 1)]


class _SemanticCommand(StrictFrozenModel):
    @field_validator("effect", mode="before", check_fields=False)
    @classmethod
    def decode_persisted_effect(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        effect_type = cls.model_fields["effect"].annotation
        if effect_type is None:
            raise ValueError("semantic command has no effect type")
        if set(value) - {field.name for field in fields(cast(type[Any], effect_type))}:
            raise ValueError("unknown semantic effect field")
        lineage = value.get("lineage")
        if isinstance(lineage, dict) and set(lineage) - {
            field.name for field in fields(MergeTrainEffectLineage)
        }:
            raise ValueError("unknown semantic lineage field")
        # Strict JSON decoding reconstructs existing frozen dataclasses without
        # permitting scalar coercion or silently accepting future command fields.
        return TypeAdapter(effect_type).validate_json(json.dumps(value), strict=True)


class CandidateRefPrepareCommand(_SemanticCommand):
    kind: Literal["candidate_ref_prepare"] = "candidate_ref_prepare"
    effect: CandidateRefPrepareEffect


class CandidateHeadMergeCommand(_SemanticCommand):
    kind: Literal["candidate_head_merge"] = "candidate_head_merge"
    effect: CandidateHeadMergeEffect


class PullRequestHeadRefreshCommand(_SemanticCommand):
    kind: Literal["pull_request_head_refresh"] = "pull_request_head_refresh"
    effect: PullRequestHeadRefreshEffect


class StackChildMergeCommand(_SemanticCommand):
    kind: Literal["stack_child_merge"] = "stack_child_merge"
    effect: StackChildMergeEffect


class PullRequestLandingCommand(_SemanticCommand):
    kind: Literal["pull_request_landing"] = "pull_request_landing"
    effect: PullRequestLandingEffect


class StackChildCommentCommand(_SemanticCommand):
    kind: Literal["stack_child_comment"] = "stack_child_comment"
    effect: StackChildCommentEffect


class StackChildLabelCommand(_SemanticCommand):
    kind: Literal["stack_child_label"] = "stack_child_label"
    effect: StackChildLabelEffect


class StackChildCloseCommand(_SemanticCommand):
    kind: Literal["stack_child_close"] = "stack_child_close"
    effect: StackChildCloseEffect


class CandidateRefDeleteCommand(_SemanticCommand):
    kind: Literal["candidate_ref_delete"] = "candidate_ref_delete"
    effect: CandidateRefDeleteEffect


OrdinaryAgentSemanticCommand: TypeAlias = Annotated[
    CandidateRefPrepareCommand
    | CandidateHeadMergeCommand
    | PullRequestHeadRefreshCommand
    | StackChildMergeCommand
    | PullRequestLandingCommand
    | StackChildCommentCommand
    | StackChildLabelCommand
    | StackChildCloseCommand
    | CandidateRefDeleteCommand,
    Field(discriminator="kind"),
]


class OrdinaryAgentControllerFence(StrictFrozenModel):
    controller_key: Identifier
    lease_owner: Identifier
    lease_acquired_at: Identifier


EffectState = Literal[
    "reserved",
    "dispatching",
    "waiting_provider",
    "not_dispatched",
    "completed",
    "completed_observed",
    "rebind_pending",
    "terminal_conflict",
    "exhausted",
    "reconciliation_required",
    "retained_no_conditional_delete",
]


class OrdinaryAgentEffectRecord(StrictFrozenModel):
    schema_version: Literal[1] = 1
    effect_id: Identifier
    request_id: Identifier
    session_id: Identifier
    lease_id: Identifier
    principal_id: Identifier
    scope_sha256: Digest
    binding_revision: int = Field(ge=1)
    semantic_ordinal: int = Field(ge=1)
    action_ordinal: int = Field(ge=1)
    command_sha256: Digest
    command: OrdinaryAgentSemanticCommand
    target: OrdinaryAgentTarget
    controller_fence: OrdinaryAgentControllerFence
    policy_record_id: Identifier
    policy_revision: int = Field(ge=1)
    policy_sha256: Digest
    credential_id: Identifier
    credential_version: int = Field(ge=1)
    credential_digest: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    state: EffectState = "reserved"
    revision: int = Field(default=1, ge=1)
    reserved_at: Epoch
    updated_at: Epoch
    dispatch_count: int = Field(default=0, ge=0)
    dispatch_custody_count: int = Field(default=0, ge=0)
    reconciliation_count: int = Field(default=0, ge=0)
    reconciliation_custody_count: int = Field(default=0, ge=0)
    next_observation_at: Epoch | None = None
    reason_code: str | None = Field(default=None, max_length=128)
    rebound_revision: int | None = Field(default=None, ge=1)


class OrdinaryAgentSnapshotAttemptRecord(StrictFrozenModel):
    schema_version: Literal[1] = 1
    attempt_id: Identifier
    request_id: Identifier
    binding_revision: int = Field(ge=1)
    scope_sha256: Digest
    principal_id: Identifier
    credential_id: Identifier
    credential_version: int = Field(ge=1)
    purpose: Literal["snapshot", "candidate_check"]
    attempt_ordinal: int = Field(ge=1)
    candidate_sha: str = Field(default="", max_length=64)
    controller_fence: OrdinaryAgentControllerFence
    state: Literal[
        "reserved", "reading", "completed", "consumed", "incomplete", "fenced", "exhausted"
    ] = "reserved"
    revision: int = Field(default=1, ge=1)
    created_at: Epoch
    updated_at: Epoch
    custody_attempt_ids: tuple[str, ...] = ()
    result: (
        OrdinaryAgentMergeTrainSnapshotResult
        | OrdinaryAgentCandidateCheckResult
        | OrdinaryAgentReadmissionObservation
        | None
    ) = None
    failure_counts: OrdinaryAgentProviderRequestCounts | None = None
    reason_code: str | None = Field(default=None, max_length=128)
    next_due_at: Epoch | None = None
    rebound_revision: int | None = Field(default=None, ge=1)

    @field_validator("custody_attempt_ids", mode="before")
    @classmethod
    def read_ids(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_readmission_consumption(self) -> Self:
        if (self.state == "consumed") != (self.rebound_revision is not None):
            raise ValueError("only consumed readmission evidence may bind a rebound revision")
        if self.state == "consumed" and not isinstance(
            self.result, OrdinaryAgentReadmissionObservation
        ):
            raise ValueError("consumed read evidence must contain a readmission observation")
        return self


class OrdinaryAgentQualificationAttemptRecord(StrictFrozenModel):
    """Schema-v2 controller-free qualification attempt.

    It deliberately does not share the guarded v1 controller field: parsing a
    qualification payload must never induce a merge-controller lookup.
    """

    schema_version: Literal[2] = 2
    attempt_id: Identifier
    request_id: Identifier
    binding_revision: int = Field(ge=1)
    scope_sha256: Digest
    principal_id: Identifier
    credential_id: Identifier
    credential_version: int = Field(ge=1)
    purpose: Literal["qualification"] = "qualification"
    attempt_ordinal: int = Field(ge=1)
    candidate_sha: Literal[""] = ""
    setup: OrdinaryAgentQualificationSetup
    custody_record_id: Identifier
    custody_sha256: Digest
    repository_inventory_record_id: Identifier
    repository_inventory_revision: int = Field(ge=1)
    repository_inventory_digest: Digest
    github_app_id: int = Field(gt=0, le=2**63 - 1)
    github_installation_id: int | None = Field(default=None, gt=0, le=2**63 - 1)
    managed_secret_binding_id: Identifier
    managed_secret_id: Identifier
    managed_secret_version_id: Identifier
    provider_inspection_sha256: Digest
    installed_permission_ceiling_sha256: Digest
    read_profile_sha256: Digest
    state: Literal["reserved", "reading", "completed", "incomplete", "fenced", "exhausted"] = (
        "reserved"
    )
    revision: int = Field(default=1, ge=1)
    created_at: Epoch
    updated_at: Epoch
    custody_attempt_ids: tuple[str, ...] = ()
    result: OrdinaryRepositoryAdminObservation | None = None
    attestation: OrdinaryAgentQualificationAttestation | None = None
    failure_counts: OrdinaryAgentProviderRequestCounts | None = None
    reason_code: str | None = Field(default=None, max_length=128)
    next_due_at: Epoch | None = None

    @field_validator("custody_attempt_ids", mode="before")
    @classmethod
    def read_ids(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_success_pair(self) -> Self:
        if self.attestation is not None and (
            self.result is None or self.result.status != "qualified"
        ):
            raise ValueError("only a positive qualification result may have an attestation")
        if (
            self.result is not None
            and self.result.status == "qualified"
            and self.attestation is None
        ):
            raise ValueError("positive qualification result requires an attestation")
        if self.attestation is not None and self.result != self.attestation.observation:
            raise ValueError("qualification attestation must bind its exact observation")
        return self


OrdinaryAgentReadAttemptRecord: TypeAlias = (
    OrdinaryAgentSnapshotAttemptRecord | OrdinaryAgentQualificationAttemptRecord
)
_READ_ATTEMPT_ADAPTER: TypeAdapter[OrdinaryAgentReadAttemptRecord] = TypeAdapter(
    OrdinaryAgentReadAttemptRecord
)


def parse_ordinary_agent_read_attempt(value: object) -> OrdinaryAgentReadAttemptRecord:
    """Read v1 guarded and v2 qualification payloads without adapting old bytes."""
    return _READ_ATTEMPT_ADAPTER.validate_python(value)
