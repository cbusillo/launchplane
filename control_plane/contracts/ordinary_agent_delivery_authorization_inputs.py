from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.merge_train_policy import normalize_merge_train_policy_timestamp
from control_plane.contracts.repository_inventory import (
    normalize_sha256,
    normalize_utc_timestamp,
    required_decimal_id,
    required_token,
)


RepositoryInventoryProjectionState = Literal["complete", "unavailable", "truncated", "ambiguous"]
MergePolicyProjectionState = Literal[
    "available", "missing", "ambiguous", "unavailable", "truncated"
]
InspectionSetupState = Literal["not_evaluated", "metadata_recorded", "incomplete", "unavailable"]
InspectionSetupRuntimeState = Literal[
    "not_evaluated",
    "metadata_recorded",
    "record_missing",
    "record_unreadable",
    "record_ambiguous",
    "app_id_missing",
    "app_id_invalid",
    "unavailable",
]
InspectionSetupManagedSecretState = Literal[
    "not_evaluated",
    "metadata_recorded",
    "secret_missing",
    "secret_unreadable",
    "secret_ambiguous",
    "binding_missing",
    "binding_unreadable",
    "binding_ambiguous",
    "binding_mismatch",
    "version_pointer_missing",
    "unavailable",
]


class AuthorizationCandidatePolicyProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    revision: int = Field(ge=1)
    schema_version: int = Field(ge=1)
    policy_sha256: str

    @model_validator(mode="after")
    def _validate(self) -> AuthorizationCandidatePolicyProvenance:
        self.record_id = required_token(self.record_id, "authorization policy record_id")
        self.policy_sha256 = normalize_sha256(
            self.policy_sha256, "authorization policy policy_sha256"
        )
        return self


class AuthorizationCandidateMergePolicyProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    policy_sha256: str
    updated_at: str

    @model_validator(mode="after")
    def _validate(self) -> AuthorizationCandidateMergePolicyProvenance:
        self.record_id = required_token(self.record_id, "merge policy record_id")
        self.policy_sha256 = normalize_sha256(self.policy_sha256, "merge policy policy_sha256")
        self.updated_at = normalize_merge_train_policy_timestamp(self.updated_at)
        return self


class OrdinaryAgentDeliveryAuthorizationCandidateRepository(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    repository_id: str
    repository: str
    inventory_revision: int = Field(ge=1)
    inventory_sha256: str
    recorded_at: str
    configured_branches: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate(self) -> OrdinaryAgentDeliveryAuthorizationCandidateRepository:
        self.record_id = required_token(self.record_id, "repository record_id")
        self.repository_id = required_decimal_id(self.repository_id, "repository_id")
        self.repository = required_token(self.repository, "repository")
        self.inventory_sha256 = normalize_sha256(
            self.inventory_sha256, "repository inventory_sha256"
        )
        self.recorded_at = normalize_utc_timestamp(self.recorded_at, "repository recorded_at")
        self.configured_branches = tuple(
            required_token(branch, "configured branch") for branch in self.configured_branches
        )
        if tuple(sorted(set(self.configured_branches))) != self.configured_branches:
            raise ValueError("Configured branches must be sorted and unique.")
        return self


class AuthorizationCandidateInputDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str

    @model_validator(mode="after")
    def _validate(self) -> AuthorizationCandidateInputDiagnostic:
        self.code = required_token(self.code, "diagnostic code")
        self.message = required_token(self.message, "diagnostic message")
        return self


class InspectionSetupRuntimeMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: InspectionSetupRuntimeState = "not_evaluated"
    app_id: str | None = None
    recorded_at: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> InspectionSetupRuntimeMetadata:
        if self.state == "metadata_recorded":
            if self.app_id is None or self.recorded_at is None:
                raise ValueError(
                    "Recorded inspection runtime metadata requires an ID and timestamp."
                )
            self.app_id = required_decimal_id(self.app_id, "inspection app_id")
            if int(self.app_id) > 2**63 - 1:
                raise ValueError("Inspection app_id exceeds the supported positive ID range.")
            self.recorded_at = normalize_utc_timestamp(
                self.recorded_at, "inspection runtime recorded_at"
            )
        elif self.app_id is not None or self.recorded_at is not None:
            raise ValueError(
                "Incomplete inspection runtime metadata cannot expose recorded values."
            )
        return self


class InspectionSetupManagedSecretMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: InspectionSetupManagedSecretState = "not_evaluated"
    secret_id: str | None = None
    binding_id: str | None = None
    current_version_id: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> InspectionSetupManagedSecretMetadata:
        identifiers = (self.secret_id, self.binding_id, self.current_version_id)
        if self.state == "metadata_recorded":
            if any(identifier is None for identifier in identifiers):
                raise ValueError("Recorded inspection secret metadata requires all identifiers.")
            self.secret_id = required_token(self.secret_id or "", "inspection secret_id")
            self.binding_id = required_token(self.binding_id or "", "inspection binding_id")
            self.current_version_id = required_token(
                self.current_version_id or "", "inspection current_version_id"
            )
        elif any(identifier is not None for identifier in identifiers):
            raise ValueError("Incomplete inspection secret metadata cannot expose identifiers.")
        return self


class InspectionSetupMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: InspectionSetupState = "not_evaluated"
    runtime: InspectionSetupRuntimeMetadata = Field(default_factory=InspectionSetupRuntimeMetadata)
    managed_secret: InspectionSetupManagedSecretMetadata = Field(
        default_factory=InspectionSetupManagedSecretMetadata
    )

    @model_validator(mode="after")
    def _validate(self) -> InspectionSetupMetadata:
        component_states = (self.runtime.state, self.managed_secret.state)
        if self.state == "not_evaluated" and component_states != (
            "not_evaluated",
            "not_evaluated",
        ):
            raise ValueError("Unevaluated inspection setup requires unevaluated components.")
        if self.state == "metadata_recorded" and component_states != (
            "metadata_recorded",
            "metadata_recorded",
        ):
            raise ValueError("Recorded inspection setup requires recorded component metadata.")
        if self.state == "incomplete" and (
            "not_evaluated" in component_states
            or "unavailable" in component_states
            or "record_unreadable" in component_states
            or "secret_unreadable" in component_states
            or "binding_unreadable" in component_states
            or component_states == ("metadata_recorded", "metadata_recorded")
        ):
            raise ValueError("Incomplete inspection setup requires readable incomplete metadata.")
        if self.state == "unavailable" and not any(
            component_state
            in {"unavailable", "record_unreadable", "secret_unreadable", "binding_unreadable"}
            for component_state in component_states
        ):
            raise ValueError("Unavailable inspection setup requires an unavailable component.")
        return self


class OrdinaryAgentDeliveryAuthorizationCandidateInputsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    schema_version: Literal[1] = 1
    trace_id: str
    observed_at: str
    authorization_policy: AuthorizationCandidatePolicyProvenance
    inventory_state: RepositoryInventoryProjectionState
    merge_policy_state: MergePolicyProjectionState
    merge_policy: AuthorizationCandidateMergePolicyProvenance | None
    repositories: tuple[OrdinaryAgentDeliveryAuthorizationCandidateRepository, ...]
    diagnostics: tuple[AuthorizationCandidateInputDiagnostic, ...]
    inspection_setup: InspectionSetupMetadata = Field(default_factory=InspectionSetupMetadata)

    @model_validator(mode="after")
    def _validate(self) -> OrdinaryAgentDeliveryAuthorizationCandidateInputsResponse:
        self.trace_id = required_token(self.trace_id, "trace_id")
        self.observed_at = normalize_utc_timestamp(self.observed_at, "observed_at")
        if (self.merge_policy_state == "available") != (self.merge_policy is not None):
            raise ValueError("Available merge policy state must match merge policy provenance.")
        return self
