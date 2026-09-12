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

    @model_validator(mode="after")
    def _validate(self) -> OrdinaryAgentDeliveryAuthorizationCandidateInputsResponse:
        self.trace_id = required_token(self.trace_id, "trace_id")
        self.observed_at = normalize_utc_timestamp(self.observed_at, "observed_at")
        if (self.merge_policy_state == "available") != (self.merge_policy is not None):
            raise ValueError("Available merge policy state must match merge policy provenance.")
        return self
