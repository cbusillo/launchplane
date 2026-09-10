"""Inert typed administration records for ordinary-agent delivery activation."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget


OrdinaryAgentDeliveryActivationDesiredState = Literal["guarded", "revoked"]
OrdinaryAgentDeliveryActivationEffectiveState = Literal[
    "qualification_only",
    "guarded",
    "revoked",
]
OrdinaryAgentDeliveryActivationEventAction = Literal[
    "installed",
    "guarded_derived",
    "readiness_lost",
    "revoked",
    "superseded",
]
OrdinaryAgentDeliveryActivationPlanBlocker = Literal[
    "database_revision_incompatible",
    "activation_schema_incompatible",
    "activation_storage_unavailable",
    "activation_cas_unavailable",
    "activation_recovery_unavailable",
    "activation_rollback_reader_unavailable",
]

_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_MANAGED_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ACTIVATION_ID_PATTERN = re.compile(r"^ordinary-agent-delivery-activation-[0-9a-f]{32}$")
_EVENT_ID_PATTERN = re.compile(r"^ordinary-agent-delivery-activation-event-[0-9a-f]{32}$")


class StrictFrozenActivationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _required_token(value: str, field_name: str) -> str:
    normalized = value.strip()
    if _TOKEN_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a bounded token")
    return normalized


def _managed_id(value: str, field_name: str) -> str:
    normalized = value.strip()
    if _MANAGED_ID_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a canonical managed identifier")
    return normalized


def _sha256(value: str, field_name: str) -> str:
    normalized = value.strip().lower()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


def _timestamp(value: str, field_name: str) -> str:
    normalized = value.strip()
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _positive_versions(value: tuple[int, ...], field_name: str) -> tuple[int, ...]:
    if any(version < 1 for version in value):
        raise ValueError(f"{field_name} may contain only positive versions")
    if tuple(sorted(set(value))) != value:
        raise ValueError(f"{field_name} must be sorted and unique")
    return value


class OrdinaryAgentDeliveryActivationScope(StrictFrozenActivationModel):
    target: OrdinaryAgentTarget
    managed_set_id: str
    managed_rule_id: str

    @model_validator(mode="after")
    def _validate_scope(self) -> "OrdinaryAgentDeliveryActivationScope":
        object.__setattr__(
            self, "managed_set_id", _managed_id(self.managed_set_id, "managed_set_id")
        )
        object.__setattr__(
            self,
            "managed_rule_id",
            _managed_id(self.managed_rule_id, "managed_rule_id"),
        )
        return self


class OrdinaryAgentDeliveryPolicyPackageReference(StrictFrozenActivationModel):
    policy_operation_id: str
    request_sha256: str
    evidence_sha256: str
    plan_sha256: str
    desired_set_sha256: str
    candidate_policy_sha256: str

    @model_validator(mode="after")
    def _validate_reference(self) -> "OrdinaryAgentDeliveryPolicyPackageReference":
        object.__setattr__(
            self,
            "policy_operation_id",
            _required_token(self.policy_operation_id, "policy_operation_id"),
        )
        for field_name in (
            "request_sha256",
            "evidence_sha256",
            "plan_sha256",
            "desired_set_sha256",
            "candidate_policy_sha256",
        ):
            object.__setattr__(
                self, field_name, _sha256(str(getattr(self, field_name)), field_name)
            )
        return self


class OrdinaryAgentDeliveryInventoryReference(StrictFrozenActivationModel):
    record_id: str
    revision: int = Field(ge=1, le=2**63 - 1)
    inventory_sha256: str
    state: Literal["tracked"] = "tracked"

    @model_validator(mode="after")
    def _validate_reference(self) -> "OrdinaryAgentDeliveryInventoryReference":
        object.__setattr__(self, "record_id", _required_token(self.record_id, "record_id"))
        object.__setattr__(
            self,
            "inventory_sha256",
            _sha256(self.inventory_sha256, "inventory_sha256"),
        )
        return self


class OrdinaryAgentDeliveryActivationReference(StrictFrozenActivationModel):
    activation_id: str
    revision: int = Field(ge=1, le=2**63 - 1)
    activation_sha256: str

    @model_validator(mode="after")
    def _validate_reference(self) -> "OrdinaryAgentDeliveryActivationReference":
        activation_id = self.activation_id.strip()
        if _ACTIVATION_ID_PATTERN.fullmatch(activation_id) is None:
            raise ValueError("activation_id is not canonical")
        object.__setattr__(self, "activation_id", activation_id)
        object.__setattr__(
            self,
            "activation_sha256",
            _sha256(self.activation_sha256, "activation_sha256"),
        )
        return self


class OrdinaryAgentDeliveryActivationSetupOption(StrictFrozenActivationModel):
    policy_operation_id: str
    repository_inventory_record_id: str
    scope: OrdinaryAgentDeliveryActivationScope
    predecessor: OrdinaryAgentDeliveryActivationReference | None = None
    label: str = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def _validate_option(self) -> "OrdinaryAgentDeliveryActivationSetupOption":
        object.__setattr__(
            self,
            "policy_operation_id",
            _required_token(self.policy_operation_id, "policy_operation_id"),
        )
        object.__setattr__(
            self,
            "repository_inventory_record_id",
            _required_token(
                self.repository_inventory_record_id,
                "repository_inventory_record_id",
            ),
        )
        object.__setattr__(self, "label", self.label.strip())
        return self


class OrdinaryAgentDeliveryActivationRevokeOption(StrictFrozenActivationModel):
    activation: OrdinaryAgentDeliveryActivationReference
    scope: OrdinaryAgentDeliveryActivationScope
    label: str = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def _validate_option(self) -> "OrdinaryAgentDeliveryActivationRevokeOption":
        object.__setattr__(self, "label", self.label.strip())
        return self


class OrdinaryAgentDeliveryRuntimeCapabilityEvidence(StrictFrozenActivationModel):
    schema_version: Literal[1] = 1
    observed_database_revision: str
    database_revision_compatible: bool
    activation_schema_invariants_sha256: str
    activation_schema_invariants_valid: bool
    finite_request_versions: tuple[int, ...]
    read_attempt_versions: tuple[int, ...]
    custody_reservation_versions: tuple[int, ...]
    qualification_attestation_versions: tuple[int, ...]
    activation_record_versions: tuple[int, ...]
    activation_event_versions: tuple[int, ...]
    recovery_versions: tuple[int, ...]
    authz_policy_read_versions: tuple[int, ...]
    variant_parsers_registered: bool
    activation_storage_registered: bool
    activation_cas_registered: bool
    activation_recovery_registered: bool
    bounded_cleanup_registered: bool
    rollback_reader_registered: bool
    qualification_advancer_registered: bool
    guarded_worker_registered: bool
    policy_v3_write_supported: bool
    service_image_reference: str = Field(default="", max_length=512)
    worker_image_reference: str = Field(default="", max_length=512)
    observed_at: str
    authorizes_execution: Literal[False] = False

    @field_validator(
        "finite_request_versions",
        "read_attempt_versions",
        "custody_reservation_versions",
        "qualification_attestation_versions",
        "activation_record_versions",
        "activation_event_versions",
        "recovery_versions",
        "authz_policy_read_versions",
        mode="before",
    )
    @classmethod
    def _read_version_tuple(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _validate_evidence(self) -> "OrdinaryAgentDeliveryRuntimeCapabilityEvidence":
        object.__setattr__(
            self,
            "observed_database_revision",
            _required_token(self.observed_database_revision, "observed_database_revision"),
        )
        object.__setattr__(
            self,
            "activation_schema_invariants_sha256",
            _sha256(
                self.activation_schema_invariants_sha256,
                "activation_schema_invariants_sha256",
            ),
        )
        for field_name in (
            "finite_request_versions",
            "read_attempt_versions",
            "custody_reservation_versions",
            "qualification_attestation_versions",
            "activation_record_versions",
            "activation_event_versions",
            "recovery_versions",
            "authz_policy_read_versions",
        ):
            object.__setattr__(
                self,
                field_name,
                _positive_versions(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "service_image_reference", self.service_image_reference.strip())
        object.__setattr__(self, "worker_image_reference", self.worker_image_reference.strip())
        object.__setattr__(self, "observed_at", _timestamp(self.observed_at, "observed_at"))
        return self

    @property
    def setup_blockers(self) -> tuple[OrdinaryAgentDeliveryActivationPlanBlocker, ...]:
        blockers: list[OrdinaryAgentDeliveryActivationPlanBlocker] = []
        if not self.database_revision_compatible:
            blockers.append("database_revision_incompatible")
        if not self.activation_schema_invariants_valid:
            blockers.append("activation_schema_incompatible")
        if not self.activation_storage_registered:
            blockers.append("activation_storage_unavailable")
        if not self.activation_cas_registered:
            blockers.append("activation_cas_unavailable")
        if not self.activation_recovery_registered:
            blockers.append("activation_recovery_unavailable")
        if not self.rollback_reader_registered:
            blockers.append("activation_rollback_reader_unavailable")
        return tuple(blockers)


class OrdinaryAgentDeliveryActivationSetupRequest(StrictFrozenActivationModel):
    schema_version: Literal[1] = 1
    action: Literal["setup"] = "setup"
    policy_operation_id: str
    repository_inventory_record_id: str
    predecessor: OrdinaryAgentDeliveryActivationReference | None = None
    activation_expires_at: str
    reason: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def _validate_request(self) -> "OrdinaryAgentDeliveryActivationSetupRequest":
        object.__setattr__(
            self,
            "policy_operation_id",
            _required_token(self.policy_operation_id, "policy_operation_id"),
        )
        object.__setattr__(
            self,
            "repository_inventory_record_id",
            _required_token(
                self.repository_inventory_record_id,
                "repository_inventory_record_id",
            ),
        )
        object.__setattr__(
            self,
            "activation_expires_at",
            _timestamp(self.activation_expires_at, "activation_expires_at"),
        )
        object.__setattr__(self, "reason", self.reason.strip())
        return self


class OrdinaryAgentDeliveryActivationRevokeRequest(StrictFrozenActivationModel):
    schema_version: Literal[1] = 1
    action: Literal["revoke_activation"] = "revoke_activation"
    activation_id: str
    expected_revision: int = Field(ge=1, le=2**63 - 1)
    expected_activation_sha256: str
    reason: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def _validate_request(self) -> "OrdinaryAgentDeliveryActivationRevokeRequest":
        activation_id = self.activation_id.strip()
        if _ACTIVATION_ID_PATTERN.fullmatch(activation_id) is None:
            raise ValueError("activation_id is not canonical")
        object.__setattr__(self, "activation_id", activation_id)
        object.__setattr__(
            self,
            "expected_activation_sha256",
            _sha256(self.expected_activation_sha256, "expected_activation_sha256"),
        )
        object.__setattr__(self, "reason", self.reason.strip())
        return self


OrdinaryAgentDeliveryActivationRequest: TypeAlias = Annotated[
    OrdinaryAgentDeliveryActivationSetupRequest | OrdinaryAgentDeliveryActivationRevokeRequest,
    Field(discriminator="action"),
]


class OrdinaryAgentDeliveryActivationSetupHumanEvidence(StrictFrozenActivationModel):
    schema_version: Literal[1] = 1
    action: Literal["setup"] = "setup"
    result_status: Literal["ok", "blocked"]
    scope: OrdinaryAgentDeliveryActivationScope
    policy_package: OrdinaryAgentDeliveryPolicyPackageReference
    inventory: OrdinaryAgentDeliveryInventoryReference
    predecessor: OrdinaryAgentDeliveryActivationReference | None = None
    activation_expires_at: str
    runtime_capability: OrdinaryAgentDeliveryRuntimeCapabilityEvidence
    blocker_codes: tuple[OrdinaryAgentDeliveryActivationPlanBlocker, ...] = ()
    plan_digest: str
    initial_behavior: Literal[
        "Guarded delivery remains qualification-only until current readiness passes."
    ] = "Guarded delivery remains qualification-only until current readiness passes."
    stop_behavior: Literal[
        "Revocation or readiness loss blocks fresh work while preserving bounded custody cleanup."
    ] = "Revocation or readiness loss blocks fresh work while preserving bounded custody cleanup."
    authorizes_execution: Literal[False] = False
    persists_state: Literal[False] = False

    @field_validator("blocker_codes", mode="before")
    @classmethod
    def _read_blockers(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _validate_evidence(self) -> "OrdinaryAgentDeliveryActivationSetupHumanEvidence":
        object.__setattr__(
            self,
            "activation_expires_at",
            _timestamp(self.activation_expires_at, "activation_expires_at"),
        )
        blockers = tuple(sorted(set(self.blocker_codes)))
        if blockers != self.blocker_codes:
            raise ValueError("activation blocker_codes must be sorted and unique")
        if blockers != tuple(sorted(self.runtime_capability.setup_blockers)):
            raise ValueError("activation blocker_codes must match runtime capability")
        if (self.result_status == "blocked") != bool(blockers):
            raise ValueError("activation result_status must match setup blockers")
        object.__setattr__(self, "plan_digest", _sha256(self.plan_digest, "plan_digest"))
        return self


class OrdinaryAgentDeliveryActivationRevokeHumanEvidence(StrictFrozenActivationModel):
    schema_version: Literal[1] = 1
    action: Literal["revoke_activation"] = "revoke_activation"
    result_status: Literal["ok"] = "ok"
    scope: OrdinaryAgentDeliveryActivationScope
    activation: OrdinaryAgentDeliveryActivationReference
    source_setup_operation_id: str
    plan_digest: str
    stop_behavior: Literal[
        "Revocation permanently blocks fresh work for this activation intent."
    ] = "Revocation permanently blocks fresh work for this activation intent."
    authorizes_execution: Literal[False] = False
    persists_state: Literal[False] = False

    @model_validator(mode="after")
    def _validate_evidence(self) -> "OrdinaryAgentDeliveryActivationRevokeHumanEvidence":
        object.__setattr__(
            self,
            "source_setup_operation_id",
            _required_token(self.source_setup_operation_id, "source_setup_operation_id"),
        )
        object.__setattr__(self, "plan_digest", _sha256(self.plan_digest, "plan_digest"))
        return self


OrdinaryAgentDeliveryActivationHumanEvidence: TypeAlias = Annotated[
    OrdinaryAgentDeliveryActivationSetupHumanEvidence
    | OrdinaryAgentDeliveryActivationRevokeHumanEvidence,
    Field(discriminator="action"),
]


class OrdinaryAgentDeliveryActivationRecord(StrictFrozenActivationModel):
    schema_version: Literal[1] = 1
    activation_id: str
    scope: OrdinaryAgentDeliveryActivationScope
    source_setup_operation_id: str
    source_setup_approval_sha256: str
    policy_package: OrdinaryAgentDeliveryPolicyPackageReference
    inventory: OrdinaryAgentDeliveryInventoryReference
    desired_state: OrdinaryAgentDeliveryActivationDesiredState
    effective_state: OrdinaryAgentDeliveryActivationEffectiveState
    activation_expires_at: str
    runtime_capability_at_setup: OrdinaryAgentDeliveryRuntimeCapabilityEvidence
    revision: int = Field(ge=1, le=2**63 - 1)
    predecessor: OrdinaryAgentDeliveryActivationReference | None = None
    installed_at: str
    updated_at: str
    revoked_at: str = ""
    superseded_by_activation_id: str = ""
    superseded_at: str = ""
    activation_sha256: str = ""
    authorizes_execution: Literal[False] = False

    @model_validator(mode="after")
    def _validate_record(self) -> "OrdinaryAgentDeliveryActivationRecord":
        activation_id = self.activation_id.strip()
        if _ACTIVATION_ID_PATTERN.fullmatch(activation_id) is None:
            raise ValueError("activation_id is not canonical")
        object.__setattr__(self, "activation_id", activation_id)
        object.__setattr__(
            self,
            "source_setup_operation_id",
            _required_token(self.source_setup_operation_id, "source_setup_operation_id"),
        )
        expected_activation_id = build_ordinary_agent_delivery_activation_id(
            scope=self.scope,
            source_setup_operation_id=self.source_setup_operation_id,
        )
        if activation_id != expected_activation_id:
            raise ValueError("activation_id does not match scope and setup operation")
        object.__setattr__(
            self,
            "source_setup_approval_sha256",
            _sha256(self.source_setup_approval_sha256, "source_setup_approval_sha256"),
        )
        for field_name in ("activation_expires_at", "installed_at", "updated_at"):
            object.__setattr__(
                self,
                field_name,
                _timestamp(str(getattr(self, field_name)), field_name),
            )
        installed_at = datetime.fromisoformat(self.installed_at)
        updated_at = datetime.fromisoformat(self.updated_at)
        expires_at = datetime.fromisoformat(self.activation_expires_at)
        if updated_at < installed_at or expires_at <= installed_at:
            raise ValueError("activation timestamps are not monotonic")
        revoked_at = self.revoked_at.strip()
        if self.desired_state == "revoked":
            if self.effective_state != "revoked" or not revoked_at:
                raise ValueError("revoked activation requires terminal effective state and time")
            revoked_at = _timestamp(revoked_at, "revoked_at")
            if datetime.fromisoformat(revoked_at) < updated_at:
                raise ValueError("revoked_at cannot precede updated_at")
        elif self.effective_state == "revoked" or revoked_at:
            raise ValueError("guarded activation cannot contain revocation state")
        object.__setattr__(self, "revoked_at", revoked_at)
        superseded_by_activation_id = self.superseded_by_activation_id.strip()
        superseded_at = self.superseded_at.strip()
        if bool(superseded_by_activation_id) != bool(superseded_at):
            raise ValueError("activation supersession identity and timestamp must be complete")
        if superseded_by_activation_id:
            if revoked_at:
                raise ValueError("revoked activation cannot also be superseded")
            if _ACTIVATION_ID_PATTERN.fullmatch(superseded_by_activation_id) is None:
                raise ValueError("superseded_by_activation_id is not canonical")
            if superseded_by_activation_id == activation_id:
                raise ValueError("activation cannot supersede itself")
            superseded_at = _timestamp(superseded_at, "superseded_at")
            if datetime.fromisoformat(superseded_at) < updated_at:
                raise ValueError("superseded_at cannot precede updated_at")
            if datetime.fromisoformat(superseded_at) < expires_at:
                raise ValueError("only an expired guarded activation can be superseded")
        object.__setattr__(self, "superseded_by_activation_id", superseded_by_activation_id)
        object.__setattr__(self, "superseded_at", superseded_at)
        expected_sha256 = ordinary_agent_delivery_activation_record_sha256(self)
        if self.activation_sha256:
            if _sha256(self.activation_sha256, "activation_sha256") != expected_sha256:
                raise ValueError("activation_sha256 does not match activation record")
        else:
            object.__setattr__(self, "activation_sha256", expected_sha256)
        return self


class OrdinaryAgentDeliveryActivationEvent(StrictFrozenActivationModel):
    schema_version: Literal[1] = 1
    event_id: str
    activation_id: str
    sequence: int = Field(ge=1, le=2**63 - 1)
    action: OrdinaryAgentDeliveryActivationEventAction
    previous_revision: int = Field(ge=0, le=2**63 - 1)
    previous_activation_sha256: str = ""
    resulting_revision: int = Field(ge=1, le=2**63 - 1)
    resulting_activation_sha256: str
    resulting_desired_state: OrdinaryAgentDeliveryActivationDesiredState
    resulting_effective_state: OrdinaryAgentDeliveryActivationEffectiveState
    occurred_at: str
    source_operation_id: str = ""
    evidence_ids: tuple[str, ...] = ()
    invalidation_reason: str = Field(default="", max_length=240)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _read_evidence_ids(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _validate_event(self) -> "OrdinaryAgentDeliveryActivationEvent":
        event_id = self.event_id.strip()
        if _EVENT_ID_PATTERN.fullmatch(event_id) is None:
            raise ValueError("activation event_id is not canonical")
        object.__setattr__(self, "event_id", event_id)
        if _ACTIVATION_ID_PATTERN.fullmatch(self.activation_id.strip()) is None:
            raise ValueError("activation event activation_id is not canonical")
        object.__setattr__(self, "activation_id", self.activation_id.strip())
        previous_sha256 = self.previous_activation_sha256.strip().lower()
        if self.previous_revision == 0:
            if previous_sha256 or self.action != "installed" or self.resulting_revision != 1:
                raise ValueError("initial activation event must install from exact absence")
        else:
            previous_sha256 = _sha256(previous_sha256, "previous_activation_sha256")
            if self.resulting_revision != self.previous_revision + 1:
                raise ValueError("activation event revision must advance exactly once")
        if self.action == "installed" and self.previous_revision != 0:
            raise ValueError("installed activation event requires exact absence")
        expected_states = {
            "installed": ("guarded", "qualification_only"),
            "guarded_derived": ("guarded", "guarded"),
            "readiness_lost": ("guarded", "qualification_only"),
            "revoked": ("revoked", "revoked"),
        }
        if (
            self.action in expected_states
            and (
                self.resulting_desired_state,
                self.resulting_effective_state,
            )
            != expected_states[self.action]
        ):
            raise ValueError("activation event action does not match resulting state")
        if self.action == "superseded" and self.resulting_desired_state != "guarded":
            raise ValueError("only a guarded activation can be superseded")
        object.__setattr__(self, "previous_activation_sha256", previous_sha256)
        object.__setattr__(
            self,
            "resulting_activation_sha256",
            _sha256(self.resulting_activation_sha256, "resulting_activation_sha256"),
        )
        object.__setattr__(self, "occurred_at", _timestamp(self.occurred_at, "occurred_at"))
        source_operation_id = self.source_operation_id.strip()
        if self.action in {"installed", "revoked", "superseded"}:
            source_operation_id = _required_token(source_operation_id, "source_operation_id")
        elif source_operation_id:
            raise ValueError("derived activation events cannot claim a source operation")
        object.__setattr__(self, "source_operation_id", source_operation_id)
        expected_event_id = build_ordinary_agent_delivery_activation_event_id(
            activation_id=self.activation_id,
            sequence=self.sequence,
            action=self.action,
            source_operation_id=source_operation_id,
        )
        if event_id != expected_event_id:
            raise ValueError("activation event_id does not match event identity")
        evidence_ids = tuple(_required_token(value, "evidence_ids") for value in self.evidence_ids)
        if tuple(sorted(set(evidence_ids))) != evidence_ids:
            raise ValueError("activation event evidence_ids must be sorted and unique")
        object.__setattr__(self, "evidence_ids", evidence_ids)
        invalidation_reason = self.invalidation_reason.strip()
        if self.action == "readiness_lost" and not invalidation_reason:
            raise ValueError("readiness loss requires a bounded invalidation reason")
        if self.action != "readiness_lost" and invalidation_reason:
            raise ValueError("only readiness loss records an invalidation reason")
        object.__setattr__(self, "invalidation_reason", invalidation_reason)
        return self


class OrdinaryAgentDeliveryActivationExecutionEvidence(StrictFrozenActivationModel):
    schema_version: Literal[1] = 1
    action: Literal["setup", "revoke_activation"]
    result_status: Literal["ok", "error"]
    result_digest: str
    changed: bool
    activation_id: str = ""
    activation_revision: int = Field(default=0, ge=0, le=2**63 - 1)
    activation_sha256: str = ""
    desired_state: OrdinaryAgentDeliveryActivationDesiredState | None = None
    effective_state: OrdinaryAgentDeliveryActivationEffectiveState | None = None
    source_operation_id: str = ""
    reconciliation_required: bool
    failure_code: str = Field(default="", max_length=160)

    @model_validator(mode="after")
    def _validate_execution(self) -> "OrdinaryAgentDeliveryActivationExecutionEvidence":
        object.__setattr__(self, "result_digest", _sha256(self.result_digest, "result_digest"))
        failure_code = self.failure_code.strip()
        object.__setattr__(self, "failure_code", failure_code)
        if self.result_status == "ok":
            if failure_code or self.reconciliation_required:
                raise ValueError("successful activation execution cannot require reconciliation")
            if self.activation_revision < 1:
                raise ValueError("successful activation execution requires a revision")
            if self.desired_state is None or self.effective_state is None:
                raise ValueError("successful activation execution requires resulting states")
            if self.desired_state == "revoked" and self.effective_state != "revoked":
                raise ValueError("revoked activation execution must be terminal")
            if self.desired_state == "guarded" and self.effective_state == "revoked":
                raise ValueError("guarded activation execution cannot be revoked")
            activation_id = self.activation_id.strip()
            if _ACTIVATION_ID_PATTERN.fullmatch(activation_id) is None:
                raise ValueError("successful activation execution requires canonical activation_id")
            object.__setattr__(self, "activation_id", activation_id)
            object.__setattr__(
                self,
                "activation_sha256",
                _sha256(self.activation_sha256, "activation_sha256"),
            )
            object.__setattr__(
                self,
                "source_operation_id",
                _required_token(self.source_operation_id, "source_operation_id"),
            )
        elif not failure_code:
            raise ValueError("failed activation execution requires a bounded failure code")
        return self


def build_ordinary_agent_delivery_activation_id(
    *,
    scope: OrdinaryAgentDeliveryActivationScope,
    source_setup_operation_id: str,
) -> str:
    digest = canonical_json_sha256(
        {
            "domain": "ordinary-agent-delivery-activation-id-v1",
            "scope": scope.model_dump(mode="json"),
            "source_setup_operation_id": _required_token(
                source_setup_operation_id,
                "source_setup_operation_id",
            ),
        }
    )
    return f"ordinary-agent-delivery-activation-{digest[:32]}"


def build_ordinary_agent_delivery_activation_event_id(
    *,
    activation_id: str,
    sequence: int,
    action: OrdinaryAgentDeliveryActivationEventAction,
    source_operation_id: str = "",
) -> str:
    digest = canonical_json_sha256(
        {
            "domain": "ordinary-agent-delivery-activation-event-id-v1",
            "activation_id": activation_id,
            "sequence": sequence,
            "action": action,
            "source_operation_id": source_operation_id,
        }
    )
    return f"ordinary-agent-delivery-activation-event-{digest[:32]}"


def ordinary_agent_delivery_activation_record_sha256(
    record: OrdinaryAgentDeliveryActivationRecord,
) -> str:
    payload = record.model_dump(mode="json")
    payload["activation_sha256"] = ""
    return canonical_json_sha256(payload)
