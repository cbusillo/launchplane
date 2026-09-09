from __future__ import annotations

from typing import Annotated, Literal, NamedTuple, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget, PrincipalProfile
from control_plane.contracts.ordinary_agent_enrollment import (
    EnrollmentAction,
    OrdinaryAgentPolicyBinding,
    OrdinaryAgentPrincipalPreState,
)

from control_plane.contracts.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAttenuation


ORDINARY_AGENT_ENROLLMENT_MUTATION_SCOPE = "ordinary-agent-enrollment"
ORDINARY_AGENT_ENROLLMENT_MUTATION_ROUTE = (
    "/internal/privileged-operations/ordinary-agent-enrollment/apply"
)

LifecycleRecordStatus = Literal["active", "superseded", "revoked"]
OrdinaryAgentEffectProfile = Literal[
    "guarded_merge", "head_refresh", "merge_train_snapshot", "pr_disposition"
]
OrdinaryAgentProviderPermissionName = Literal[
    "administration",
    "checks",
    "metadata",
    "contents",
    "pull_requests",
    "issues",
    "statuses",
]
OrdinaryAgentProviderPermissionAccess = Literal["read", "write"]
OrdinaryAgentEnrollmentCompareWriteStatus = Literal[
    "written",
    "replayed",
    "idempotency_conflict",
    "reservation_in_progress",
    "reconciliation_required",
    "policy_drift",
    "administrator_denied",
    "principal_drift",
    "inventory_drift",
    "secret_drift",
    "custody_drift",
    "invalid_transition",
]


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class OrdinaryAgentAdministratorAuthorizationBinding(StrictFrozenModel):
    """Version-neutral provenance for an exact immutable human administrator rule."""

    policy_record_id: str = Field(min_length=1, max_length=256)
    policy_revision: int = Field(ge=1, le=2**63 - 1)
    policy_schema_version: int = Field(ge=1, le=2**31 - 1)
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_source: str = Field(min_length=1, max_length=512)
    managed_set_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
    managed_rule_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
    administrator_github_id: int = Field(gt=0, le=2**63 - 1)
    action: Literal["authz_policy_grant.write"] = "authz_policy_grant.write"


class OrdinaryAgentRepositoryInventoryBinding(StrictFrozenModel):
    record_id: str = Field(min_length=1, max_length=256)
    inventory_revision: int = Field(ge=1, le=2**63 - 1)
    inventory_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class OrdinaryAgentManagedSecretBinding(StrictFrozenModel):
    binding_id: str = Field(min_length=1, max_length=256)
    secret_id: str = Field(min_length=1, max_length=256)
    secret_version_id: str = Field(min_length=1, max_length=256)
    integration: str = Field(min_length=1, max_length=128)
    binding_key: str = Field(min_length=1, max_length=256)


class OrdinaryAgentProviderPermission(StrictFrozenModel):
    name: OrdinaryAgentProviderPermissionName
    access: OrdinaryAgentProviderPermissionAccess


class OrdinaryAgentAuthenticationCredentialCandidate(StrictFrozenModel):
    """Internal service-issued agent-to-Launchplane authentication material metadata."""

    candidate_kind: Literal["service_issued"] = "service_issued"
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    credential_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    credential_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid_from: int = Field(ge=0, le=2**63 - 1)
    expires_at: int = Field(ge=0, le=2**63 - 1)
    issuance_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_lifetime(self) -> OrdinaryAgentAuthenticationCredentialCandidate:
        if self.expires_at <= self.valid_from:
            raise ValueError("agent authentication credential expiry must follow valid_from")
        return self


class OrdinaryAgentCredentialCustodyCandidate(StrictFrozenModel):
    """Provider-inspected metadata; it never contains a private key or installation token."""

    candidate_kind: Literal["provider_inspected"] = "provider_inspected"
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    policy: OrdinaryAgentPolicyBinding
    repository_inventory: OrdinaryAgentRepositoryInventoryBinding
    target: OrdinaryAgentTarget
    purpose: Literal["guarded_merge"] = "guarded_merge"
    github_app_id: int = Field(gt=0, le=2**63 - 1)
    managed_secret: OrdinaryAgentManagedSecretBinding
    effect_profiles: tuple[OrdinaryAgentEffectProfile, ...] = Field(min_length=1)
    permissions: tuple[OrdinaryAgentProviderPermission, ...] = Field(min_length=1)
    valid_from: int = Field(ge=0, le=2**63 - 1)
    expires_at: int = Field(ge=0, le=2**63 - 1)
    provider_inspection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_record_id: str | None = Field(default=None, min_length=1, max_length=256)
    predecessor_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("effect_profiles", "permissions", mode="before")
    @classmethod
    def read_json_backed_tuples(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_candidate(self) -> OrdinaryAgentCredentialCustodyCandidate:
        if self.expires_at <= self.valid_from:
            raise ValueError("provider custody expiry must follow valid_from")
        if self.target != self.policy.target:
            raise ValueError("provider custody target must match its policy binding")
        if len(set(self.effect_profiles)) != len(self.effect_profiles):
            raise ValueError("provider custody effect profiles must be unique")
        permission_keys = tuple((item.name, item.access) for item in self.permissions)
        if len(set(permission_keys)) != len(permission_keys):
            raise ValueError("provider custody permissions must be unique")
        if ("metadata", "read") not in permission_keys:
            raise ValueError("provider custody requires metadata read permission")
        if bool(self.predecessor_record_id) != bool(self.predecessor_sha256):
            raise ValueError("provider custody predecessor ID and digest must be supplied together")
        return self


class OrdinaryAgentEnrollmentApplyBase(StrictFrozenModel):
    schema_version: Literal[1] = 1
    operation_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{2,255}$")
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    administrator: OrdinaryAgentAdministratorAuthorizationBinding


class OrdinaryAgentDeliveryBinding(StrictFrozenModel):
    receiver_claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    expires_at: int = Field(ge=0, le=2**63 - 1)


class OrdinaryAgentEnrollApplyEnvelope(OrdinaryAgentEnrollmentApplyBase):
    action: Literal["enroll"]
    policy: OrdinaryAgentPolicyBinding
    expected_principal_absent: Literal[True]
    authentication_credential: OrdinaryAgentAuthenticationCredentialCandidate
    delivery: OrdinaryAgentDeliveryBinding
    session_attenuation: OrdinaryAgentSessionAttenuation | None = None
    custody: OrdinaryAgentCredentialCustodyCandidate

    @model_validator(mode="after")
    def validate_bindings(self) -> OrdinaryAgentEnrollApplyEnvelope:
        _validate_enrollment_candidate_bindings(
            principal_id=self.principal_id,
            policy=self.policy,
            authentication_credential=self.authentication_credential,
            custody=self.custody,
        )
        if self.custody.predecessor_record_id is not None:
            raise ValueError("initial enrollment cannot declare custody predecessor evidence")
        return self


class OrdinaryAgentRotateCredentialApplyEnvelope(OrdinaryAgentEnrollmentApplyBase):
    action: Literal["rotate_credential"]
    policy: OrdinaryAgentPolicyBinding
    principal: OrdinaryAgentPrincipalPreState
    credential_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    credential_version: int = Field(ge=1, le=2**63 - 1)
    authentication_credential: OrdinaryAgentAuthenticationCredentialCandidate
    delivery: OrdinaryAgentDeliveryBinding
    session_attenuation: OrdinaryAgentSessionAttenuation | None = None
    custody: OrdinaryAgentCredentialCustodyCandidate

    @model_validator(mode="after")
    def validate_bindings(self) -> OrdinaryAgentRotateCredentialApplyEnvelope:
        _validate_enrollment_candidate_bindings(
            principal_id=self.principal_id,
            policy=self.policy,
            authentication_credential=self.authentication_credential,
            custody=self.custody,
        )
        if self.authentication_credential.credential_id != self.credential_id:
            raise ValueError("rotated authentication candidate must keep the credential ID")
        if self.custody.predecessor_record_id is None:
            raise ValueError("credential rotation requires custody predecessor evidence")
        return self


class OrdinaryAgentRevokePrincipalApplyEnvelope(OrdinaryAgentEnrollmentApplyBase):
    action: Literal["revoke_principal"]
    principal: OrdinaryAgentPrincipalPreState


OrdinaryAgentEnrollmentApplyEnvelope: TypeAlias = Annotated[
    OrdinaryAgentEnrollApplyEnvelope
    | OrdinaryAgentRotateCredentialApplyEnvelope
    | OrdinaryAgentRevokePrincipalApplyEnvelope,
    Field(discriminator="action"),
]


def _validate_enrollment_candidate_bindings(
    *,
    principal_id: str,
    policy: OrdinaryAgentPolicyBinding,
    authentication_credential: OrdinaryAgentAuthenticationCredentialCandidate,
    custody: OrdinaryAgentCredentialCustodyCandidate,
) -> None:
    if authentication_credential.principal_id != principal_id:
        raise ValueError("authentication candidate principal does not match the operation")
    if custody.principal_id != principal_id:
        raise ValueError("custody candidate principal does not match the operation")
    if custody.policy != policy or custody.target != policy.target:
        raise ValueError("custody candidate must preserve the exact policy binding")


class OrdinaryAgentPrincipalRecord(StrictFrozenModel):
    schema_version: Literal[1] = 1
    record_id: str = Field(min_length=1, max_length=256)
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    principal_revision: int = Field(ge=1, le=2**63 - 1)
    status: Literal["active", "revoked"]
    execution_profile: PrincipalProfile
    credential_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    credential_version: int = Field(ge=1, le=2**63 - 1)
    credential_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    custody_record_id: str = Field(min_length=1, max_length=256)
    custody_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: OrdinaryAgentPolicyBinding
    supersedes_record_id: str | None = Field(default=None, min_length=1, max_length=256)
    recorded_at: str = Field(min_length=1, max_length=64)
    record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_record(self) -> OrdinaryAgentPrincipalRecord:
        if (self.principal_revision == 1) != (self.supersedes_record_id is None):
            raise ValueError(
                "only the first ordinary-agent principal revision may omit its predecessor"
            )
        if self.record_sha256 != lifecycle_record_sha256(self, digest_field="record_sha256"):
            raise ValueError("ordinary-agent principal digest does not match payload")
        return self


class OrdinaryAgentAuthenticationCredentialRecord(StrictFrozenModel):
    schema_version: Literal[1] = 1
    record_id: str = Field(min_length=1, max_length=256)
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    credential_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    credential_version: int = Field(ge=1, le=2**63 - 1)
    status: LifecycleRecordStatus
    credential_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid_from: int = Field(ge=0, le=2**63 - 1)
    expires_at: int = Field(ge=0, le=2**63 - 1)
    revoked_at: int | None = Field(default=None, ge=0, le=2**63 - 1)
    issuance_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: OrdinaryAgentPolicyBinding
    recorded_at: str = Field(min_length=1, max_length=64)
    record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_record(self) -> OrdinaryAgentAuthenticationCredentialRecord:
        if self.expires_at <= self.valid_from:
            raise ValueError("agent authentication credential expiry must follow valid_from")
        if self.status == "revoked" and self.revoked_at is None:
            raise ValueError("revoked authentication credentials require revoked_at")
        if self.status != "revoked" and self.revoked_at is not None:
            raise ValueError("only revoked authentication credentials may set revoked_at")
        if self.record_sha256 != lifecycle_record_sha256(self, digest_field="record_sha256"):
            raise ValueError(
                "ordinary-agent authentication credential digest does not match payload"
            )
        return self


class OrdinaryAgentCredentialCustodyRecord(StrictFrozenModel):
    schema_version: Literal[1] = 1
    record_id: str = Field(min_length=1, max_length=256)
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    credential_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    credential_version: int = Field(ge=1, le=2**63 - 1)
    policy: OrdinaryAgentPolicyBinding
    repository_inventory: OrdinaryAgentRepositoryInventoryBinding
    target: OrdinaryAgentTarget
    purpose: Literal["guarded_merge"]
    github_app_id: int = Field(gt=0, le=2**63 - 1)
    managed_secret: OrdinaryAgentManagedSecretBinding
    effect_profiles: tuple[OrdinaryAgentEffectProfile, ...]
    permissions: tuple[OrdinaryAgentProviderPermission, ...]
    valid_from: int = Field(ge=0, le=2**63 - 1)
    expires_at: int = Field(ge=0, le=2**63 - 1)
    provider_inspection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_record_id: str | None = Field(default=None, min_length=1, max_length=256)
    predecessor_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    recorded_at: str = Field(min_length=1, max_length=64)
    custody_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("effect_profiles", "permissions", mode="before")
    @classmethod
    def read_json_backed_tuples(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_record(self) -> OrdinaryAgentCredentialCustodyRecord:
        if self.expires_at <= self.valid_from:
            raise ValueError("provider custody expiry must follow valid_from")
        if self.target != self.policy.target:
            raise ValueError("provider custody target must match its policy binding")
        if len(set(self.effect_profiles)) != len(self.effect_profiles):
            raise ValueError("provider custody effect profiles must be unique")
        permission_keys = tuple((item.name, item.access) for item in self.permissions)
        if len(set(permission_keys)) != len(permission_keys):
            raise ValueError("provider custody permissions must be unique")
        if ("metadata", "read") not in permission_keys:
            raise ValueError("provider custody requires metadata read permission")
        if bool(self.predecessor_record_id) != bool(self.predecessor_sha256):
            raise ValueError("provider custody predecessor ID and digest must be supplied together")
        if self.custody_sha256 != lifecycle_record_sha256(self, digest_field="custody_sha256"):
            raise ValueError("ordinary-agent custody digest does not match payload")
        return self


class OrdinaryAgentLifecycleAuditRecord(StrictFrozenModel):
    schema_version: Literal[1] = 1
    event_id: str = Field(min_length=1, max_length=256)
    operation_id: str = Field(min_length=1, max_length=256)
    action: EnrollmentAction
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    administrator: OrdinaryAgentAdministratorAuthorizationBinding
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_principal_record_id: str | None = Field(default=None, min_length=1)
    previous_principal_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    previous_credential_record_id: str | None = Field(default=None, min_length=1)
    previous_credential_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    resulting_principal_record_id: str = Field(min_length=1)
    resulting_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resulting_credential_record_id: str | None = Field(default=None, min_length=1)
    resulting_credential_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    custody_record_id: str | None = Field(default=None, min_length=1)
    custody_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    outcome: Literal["enrolled", "credential_rotated", "principal_revoked"]
    occurred_at: str = Field(min_length=1, max_length=64)
    audit_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_record(self) -> OrdinaryAgentLifecycleAuditRecord:
        previous_principal = bool(self.previous_principal_record_id)
        previous_credential = bool(self.previous_credential_record_id)
        if previous_principal != bool(self.previous_principal_sha256):
            raise ValueError("previous principal audit ID and digest must be supplied together")
        if previous_credential != bool(self.previous_credential_sha256):
            raise ValueError("previous credential audit ID and digest must be supplied together")
        if self.action != "revoke_principal" and previous_principal != previous_credential:
            raise ValueError("previous principal and credential audit evidence must align")
        resulting_credential = bool(self.resulting_credential_record_id)
        if resulting_credential != bool(self.resulting_credential_sha256):
            raise ValueError("resulting credential audit ID and digest must be supplied together")
        if self.action != "revoke_principal" and not resulting_credential:
            raise ValueError("enroll and rotation require resulting credential evidence")
        custody = bool(self.custody_record_id)
        if custody != bool(self.custody_sha256):
            raise ValueError("custody audit ID and digest must be supplied together")
        expected_outcome = {
            "enroll": "enrolled",
            "rotate_credential": "credential_rotated",
            "revoke_principal": "principal_revoked",
        }[self.action]
        if self.outcome != expected_outcome:
            raise ValueError("ordinary-agent lifecycle audit outcome does not match action")
        if self.action == "enroll" and previous_principal:
            raise ValueError("initial enrollment cannot include previous lifecycle evidence")
        if self.action != "enroll" and not previous_principal:
            raise ValueError("rotation and revocation require previous lifecycle evidence")
        if (self.action == "revoke_principal") == custody:
            raise ValueError("custody audit evidence is required only for enroll and rotation")
        if self.audit_sha256 != lifecycle_record_sha256(self, digest_field="audit_sha256"):
            raise ValueError("ordinary-agent lifecycle audit digest does not match payload")
        return self


class OrdinaryAgentEnrollmentReceipt(StrictFrozenModel):
    schema_version: Literal[1] = 1
    operation_id: str = Field(min_length=1, max_length=256)
    action: EnrollmentAction
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    principal_record_id: str = Field(min_length=1)
    principal_revision: int = Field(ge=1, le=2**63 - 1)
    principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    credential_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    credential_version: int = Field(ge=1, le=2**63 - 1)
    credential_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    custody_record_id: str | None = Field(default=None, min_length=1)
    custody_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    audit_event_id: str = Field(min_length=1)
    audit_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recorded_at: str = Field(min_length=1, max_length=64)
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_record(self) -> OrdinaryAgentEnrollmentReceipt:
        if self.action != "revoke_principal" and self.credential_sha256 is None:
            raise ValueError("enroll and rotation require credential receipt evidence")
        custody = bool(self.custody_record_id)
        if custody != bool(self.custody_sha256):
            raise ValueError("custody receipt ID and digest must be supplied together")
        if (self.action == "revoke_principal") == custody:
            raise ValueError("custody receipt evidence is required only for enroll and rotation")
        if self.result_sha256 != lifecycle_record_sha256(self, digest_field="result_sha256"):
            raise ValueError("ordinary-agent enrollment receipt digest does not match payload")
        return self


class OrdinaryAgentEnrollmentCompareWriteResult(NamedTuple):
    status: OrdinaryAgentEnrollmentCompareWriteStatus
    receipt: OrdinaryAgentEnrollmentReceipt | None = None
    current_principal: OrdinaryAgentPrincipalRecord | None = None
    idempotency_record: LaunchplaneIdempotencyRecord | None = None
    delivery_status: str | None = None


def lifecycle_record_sha256(record: BaseModel, *, digest_field: str) -> str:
    return canonical_json_sha256(record.model_dump(mode="json", exclude={digest_field}))


def ordinary_agent_enrollment_envelope_sha256(
    envelope: OrdinaryAgentEnrollmentApplyEnvelope,
) -> str:
    payload = envelope.model_dump(mode="json")
    if payload.get("session_attenuation") is None:
        payload.pop("session_attenuation", None)
    if not isinstance(envelope, OrdinaryAgentRevokePrincipalApplyEnvelope):
        # Intent is stable across independently randomized issuance attempts.
        candidate = payload["authentication_credential"]
        candidate.pop("credential_digest")
        candidate.pop("issuance_evidence_sha256")
    return canonical_json_sha256(payload)


def ordinary_agent_session_enrollment_intent_sha256(
    envelope: OrdinaryAgentEnrollApplyEnvelope | OrdinaryAgentRotateCredentialApplyEnvelope,
) -> str:
    """Exact administrator-approved intent, before randomized issuer preparation."""
    return OrdinaryAgentEnrollmentIntent.from_envelope(envelope).intent_sha256


class OrdinaryAgentEnrollmentApprovalReference(StrictFrozenModel):
    """Locator only; the apply transaction must verify persisted approval."""

    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    operation_id: str = Field(min_length=1, max_length=256)


class OrdinaryAgentPlannedAuthenticationCredential(StrictFrozenModel):
    """Approved identity/lifetime plan, before any secret is generated."""

    credential_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    valid_from: int = Field(ge=0, le=2**63 - 1)
    expires_at: int = Field(ge=1, le=2**63 - 1)

    @model_validator(mode="after")
    def validate_lifetime(self) -> OrdinaryAgentPlannedAuthenticationCredential:
        if self.expires_at <= self.valid_from:
            raise ValueError("planned credential expiry must follow valid_from")
        return self


class OrdinaryAgentEnrollmentIntent(StrictFrozenModel):
    """Immutable proposed scope; the actual browser approver is not known yet."""

    schema_version: Literal[1] = 1
    action: Literal["enroll", "rotate_credential"]
    operation_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{2,255}$")
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: OrdinaryAgentPolicyBinding
    principal: OrdinaryAgentPrincipalPreState | None = None
    credential_id: str | None = None
    credential_version: int | None = Field(default=None, ge=1, le=2**63 - 1)
    authentication_credential: OrdinaryAgentPlannedAuthenticationCredential
    delivery: OrdinaryAgentDeliveryBinding
    custody: OrdinaryAgentCredentialCustodyCandidate
    session_attenuation: OrdinaryAgentSessionAttenuation | None = None

    @model_validator(mode="after")
    def validate_intent(self) -> OrdinaryAgentEnrollmentIntent:
        if self.custody.principal_id != self.principal_id or self.custody.policy != self.policy:
            raise ValueError("planned custody must preserve principal and exact policy binding")
        if self.action == "enroll":
            if (
                self.principal is not None
                or self.credential_id is not None
                or self.credential_version is not None
                or self.custody.predecessor_record_id is not None
            ):
                raise ValueError("initial enrollment cannot declare rotation prestate")
        elif (
            self.principal is None
            or self.credential_id != self.authentication_credential.credential_id
            or self.credential_version is None
            or self.custody.predecessor_record_id is None
        ):
            raise ValueError("rotation requires exact current credential and custody prestate")
        return self

    @classmethod
    def from_envelope(
        cls, envelope: OrdinaryAgentEnrollApplyEnvelope | OrdinaryAgentRotateCredentialApplyEnvelope
    ) -> OrdinaryAgentEnrollmentIntent:
        payload = envelope.model_dump(
            exclude={"administrator", "approval_sha256", "expected_principal_absent"}
        )
        payload["authentication_credential"] = envelope.authentication_credential.model_dump(
            include={"credential_id", "valid_from", "expires_at"}
        )
        return cls.model_validate(payload)

    @property
    def intent_sha256(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))


class OrdinaryAgentApprovedEnrollmentIntent(StrictFrozenModel):
    """Private worker input: authenticated approval, still no generated credential."""

    intent: OrdinaryAgentEnrollmentIntent
    administrator: OrdinaryAgentAdministratorAuthorizationBinding
    approval_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def _envelope_payload(self) -> dict[str, object]:
        payload = self.intent.model_dump(mode="json")
        if self.intent.action == "enroll":
            for name in ("principal", "credential_id", "credential_version"):
                payload.pop(name)
            payload["expected_principal_absent"] = True
        payload["administrator"] = self.administrator.model_dump(mode="json")
        payload["approval_sha256"] = self.approval_sha256
        return payload

    @property
    def issuance_intent_sha256(self) -> str:
        payload = self._envelope_payload()
        payload["authentication_credential"] = {
            "candidate_kind": "service_issued",
            "principal_id": self.intent.principal_id,
            **self.intent.authentication_credential.model_dump(mode="json"),
        }
        if payload.get("session_attenuation") is None:
            payload.pop("session_attenuation", None)
        return canonical_json_sha256(payload)

    def apply_envelope(
        self, generated_candidate: OrdinaryAgentAuthenticationCredentialCandidate
    ) -> OrdinaryAgentEnrollApplyEnvelope | OrdinaryAgentRotateCredentialApplyEnvelope:
        planned = self.intent.authentication_credential
        if (
            generated_candidate.principal_id != self.intent.principal_id
            or generated_candidate.credential_id != planned.credential_id
            or generated_candidate.valid_from != planned.valid_from
            or generated_candidate.expires_at != planned.expires_at
        ):
            raise ValueError("generated credential does not match approved identity and lifetime")
        payload = {**self._envelope_payload(), "authentication_credential": generated_candidate}
        if self.intent.action == "enroll":
            return OrdinaryAgentEnrollApplyEnvelope.model_validate(payload)
        return OrdinaryAgentRotateCredentialApplyEnvelope.model_validate(payload)
