"""Dormant, read-only ordinary-agent administrator qualification contracts."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget
from control_plane.contracts.ordinary_agent_lifecycle import StrictFrozenModel
from control_plane.contracts.ordinary_agent_snapshot import OrdinaryAgentProviderRequestCounts


class OrdinaryAgentQualificationSetup(StrictFrozenModel):
    """Service-resolved activation provenance; it is never request input."""

    source_activation_operation_id: str = Field(min_length=1, max_length=256)
    source_activation_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target: OrdinaryAgentTarget
    managed_set_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
    managed_rule_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
    administrator_github_id: int = Field(gt=0, le=2**63 - 1)
    administrator_login: str = Field(min_length=1, max_length=256)
    administrator_login_normalized: str = Field(min_length=1, max_length=256)
    attestation_expires_at: int = Field(ge=1, le=2**63 - 1)

    @model_validator(mode="after")
    def validate_login_normalization(self) -> Self:
        if (
            not self.administrator_login.strip()
            or self.administrator_login.casefold() != self.administrator_login_normalized
        ):
            raise ValueError("qualification setup login normalization must be casefolded")
        return self


class OrdinaryAgentQualificationIdentity(StrictFrozenModel):
    github_id: int = Field(gt=0, le=2**63 - 1)
    login: str = Field(min_length=1, max_length=256)
    login_normalized: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_login_normalization(self) -> Self:
        if not self.login.strip() or self.login.casefold() != self.login_normalized:
            raise ValueError("qualification identity login normalization must be casefolded")
        return self


class OrdinaryRepositoryAdminObservation(StrictFrozenModel):
    """One bounded, sanitized result of the collaborators admin page."""

    schema_version: Literal[1] = 1
    status: Literal[
        "qualified",
        "administrator_login_changed",
        "administrator_identity_mismatch",
        "administrator_not_admin",
        "inconclusive_truncated_page",
    ]
    expected: OrdinaryAgentQualificationIdentity
    observed: OrdinaryAgentQualificationIdentity | None = None
    entry_count: int = Field(ge=0, le=100)
    counts: OrdinaryAgentProviderRequestCounts
    observed_at: int = Field(ge=0, le=2**63 - 1)
    observation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_digest_and_result(self) -> Self:
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"observation_sha256"})
        )
        if self.observation_sha256 != expected:
            raise ValueError("qualification observation digest does not match evidence")
        if self.status == "qualified" and (
            self.observed is None
            or self.observed.github_id != self.expected.github_id
            or self.observed.login_normalized != self.expected.login_normalized
        ):
            raise ValueError("qualified observation must retain the expected identity pair")
        if self.status == "administrator_not_admin" and self.entry_count >= 100:
            raise ValueError("full collaborator page is inconclusive")
        if self.status == "administrator_identity_mismatch" and self.entry_count >= 100:
            raise ValueError("full collaborator page is inconclusive")
        if self.status == "inconclusive_truncated_page" and self.entry_count != 100:
            raise ValueError("only a full page can be truncated")
        return self


class OrdinaryAgentQualificationAttestation(StrictFrozenModel):
    """Positive immutable observation; readiness still rechecks current authority."""

    schema_version: Literal[1] = 1
    request_id: str = Field(min_length=1, max_length=256)
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_revision: int = Field(ge=1)
    target: OrdinaryAgentTarget
    lease_action: Literal["preflight"] = "preflight"
    source_activation_operation_id: str = Field(min_length=1, max_length=256)
    source_activation_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    administrator: OrdinaryAgentQualificationIdentity
    principal_id: str = Field(min_length=1, max_length=256)
    credential_id: str = Field(min_length=1, max_length=256)
    credential_version: int = Field(ge=1)
    policy_managed_set_id: str = Field(min_length=1, max_length=256)
    policy_managed_rule_id: str = Field(min_length=1, max_length=256)
    custody_record_id: str = Field(min_length=1, max_length=256)
    custody_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repository_inventory_record_id: str = Field(min_length=1, max_length=256)
    repository_inventory_revision: int = Field(ge=1)
    repository_inventory_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    github_app_id: int = Field(gt=0, le=2**63 - 1)
    github_installation_id: int | None = Field(default=None, gt=0, le=2**63 - 1)
    managed_secret_binding_id: str = Field(min_length=1, max_length=256)
    managed_secret_id: str = Field(min_length=1, max_length=256)
    managed_secret_version_id: str = Field(min_length=1, max_length=256)
    provider_inspection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installed_permission_ceiling_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    effect_profile: Literal["merge_train_snapshot"] = "merge_train_snapshot"
    read_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    custody_attempt_id: str = Field(min_length=1, max_length=256)
    observation: OrdinaryRepositoryAdminObservation
    expires_at: int = Field(ge=1, le=2**63 - 1)
    attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_attestation(self) -> Self:
        if self.observation.status != "qualified":
            raise ValueError("qualification attestation requires positive observation")
        if self.expires_at <= self.observation.observed_at:
            raise ValueError("qualification attestation must outlive its observation")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"attestation_sha256"})
        )
        if self.attestation_sha256 != expected:
            raise ValueError("qualification attestation digest does not match evidence")
        return self


def qualification_identity(*, github_id: int, login: str) -> OrdinaryAgentQualificationIdentity:
    return OrdinaryAgentQualificationIdentity(
        github_id=github_id, login=login, login_normalized=login.casefold()
    )


def qualification_permission_ceiling_sha256(*, permissions: object) -> str:
    return canonical_json_sha256(
        {
            "domain": "ordinary-agent-qualification-installed-permission-ceiling-v1",
            "permissions": permissions,
        }
    )


def qualification_read_profile_sha256(*, permissions: tuple[str, ...]) -> str:
    return canonical_json_sha256(
        {
            "domain": "ordinary-agent-qualification-read-profile-v1",
            "effect_profile": "merge_train_snapshot",
            "permissions": list(permissions),
        }
    )
