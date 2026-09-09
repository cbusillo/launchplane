from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget, PrincipalProfile


EnrollmentAction = Literal["enroll", "rotate_credential", "revoke_principal"]
EnrollmentReasonCode = Literal["ordinary_agent_enrollment_not_activated"]
EnrollmentDiagnosticCode = Literal["credential_custody_unavailable"]


class DormantEnrollmentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    authority_state: Literal["dormant"] = "dormant"
    authorizes_execution: Literal[False] = False

    @field_validator("authorizes_execution", mode="before")
    @classmethod
    def validate_literal_false(cls, value: object) -> object:
        if value is not False:
            raise ValueError("authorizes_execution must be the JSON boolean false")
        return value


class OrdinaryAgentPolicyBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    record_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1, le=2**63 - 1)
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    managed_set_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
    managed_rule_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
    target: OrdinaryAgentTarget


class OrdinaryAgentPrincipalPreState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    record_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1, le=2**63 - 1)
    pre_state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class OrdinaryAgentEnrollRequest(DormantEnrollmentModel):
    action: Literal["enroll"]
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    policy: OrdinaryAgentPolicyBinding
    expected_principal_absent: Literal[True]


class OrdinaryAgentRotateCredentialRequest(DormantEnrollmentModel):
    action: Literal["rotate_credential"]
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    policy: OrdinaryAgentPolicyBinding
    principal: OrdinaryAgentPrincipalPreState
    credential_id: str = Field(min_length=1, max_length=256)
    credential_version: int = Field(ge=1, le=2**63 - 1)


class OrdinaryAgentRevokePrincipalRequest(DormantEnrollmentModel):
    action: Literal["revoke_principal"]
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    principal: OrdinaryAgentPrincipalPreState


OrdinaryAgentEnrollmentRequest: TypeAlias = Annotated[
    OrdinaryAgentEnrollRequest
    | OrdinaryAgentRotateCredentialRequest
    | OrdinaryAgentRevokePrincipalRequest,
    Field(discriminator="action"),
]


class OrdinaryAgentEnrollmentReview(DormantEnrollmentModel):
    action: EnrollmentAction
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    policy: OrdinaryAgentPolicyBinding | None = None
    principal: OrdinaryAgentPrincipalPreState | None = None
    expected_principal_absent: bool = False
    credential_id: str = ""
    credential_version: int | None = Field(default=None, ge=1, le=2**63 - 1)
    derived_execution_profile: PrincipalProfile | None = None
    credential_custody: Literal["unavailable", "not_applicable"]
    review_sha256: str = ""

    @model_validator(mode="after")
    def validate_review(self) -> OrdinaryAgentEnrollmentReview:
        if self.action == "enroll":
            if (
                self.policy is None
                or self.principal is not None
                or not self.expected_principal_absent
                or self.credential_id
                or self.credential_version is not None
                or self.derived_execution_profile is None
                or self.credential_custody != "unavailable"
            ):
                raise ValueError("enroll review does not match its dormant contract")
        elif self.action == "rotate_credential":
            if (
                self.policy is None
                or self.principal is None
                or self.expected_principal_absent
                or not self.credential_id
                or self.credential_version is None
                or self.derived_execution_profile is None
                or self.credential_custody != "unavailable"
            ):
                raise ValueError("credential-rotation review does not match its dormant contract")
        elif (
            self.policy is not None
            or self.principal is None
            or self.expected_principal_absent
            or self.credential_id
            or self.credential_version is not None
            or self.derived_execution_profile is not None
            or self.credential_custody != "not_applicable"
        ):
            raise ValueError("principal-revocation review does not match its dormant contract")
        expected = canonical_json_sha256(self.model_dump(mode="json", exclude={"review_sha256"}))
        if self.review_sha256 and self.review_sha256 != expected:
            raise ValueError("ordinary-agent enrollment review digest does not match payload")
        object.__setattr__(self, "review_sha256", expected)
        return self


class DormantOrdinaryAgentEnrollmentResult(DormantEnrollmentModel):
    action: EnrollmentAction
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    status: Literal["unavailable"] = "unavailable"
    reason_code: EnrollmentReasonCode = "ordinary_agent_enrollment_not_activated"
    diagnostic_codes: tuple[EnrollmentDiagnosticCode, ...] = ()
    effect_count: Literal[0] = 0
