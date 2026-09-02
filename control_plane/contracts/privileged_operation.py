from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.merge_train_policy import (
    MergeTrainPolicyRecord,
    normalize_merge_train_policy_timestamp,
)
from control_plane.authz_grant_service import (
    AuthzManagedPolicyDiff,
    AuthzManagedPolicyReconcileEnvelope,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy


PRIVILEGED_SECRET_OPERATION_PLAN_ACTION = "privileged_secret_operation.plan"
PRIVILEGED_SECRET_OPERATION_READ_ACTION = "privileged_secret_operation.read"
PRIVILEGED_SECRET_OPERATION_CANCEL_ACTION = "privileged_secret_operation.cancel"
PRIVILEGED_SECRET_OPERATION_APPROVE_ACTION = "privileged_secret_operation.approve"
PRIVILEGED_SECRET_OPERATION_REVOKE_ACTION = "privileged_secret_operation.revoke"
PRIVILEGED_OPERATION_SUMMARY_READ_ACTION = "privileged_operation_summary.read"
AUTHZ_POLICY_OPERATION_PROPOSE_ACTION = "authz_policy_operation.propose"
AUTHZ_POLICY_OPERATION_READ_ACTION = "authz_policy_operation.read"
AUTHZ_POLICY_OPERATION_CANCEL_ACTION = "authz_policy_operation.cancel"
AUTHZ_POLICY_OPERATION_APPROVE_ACTION = "authz_policy_operation.approve"
AUTHZ_POLICY_OPERATION_REVOKE_ACTION = "authz_policy_operation.revoke"
PRIVILEGED_POLICY_OPERATION_SUMMARY_READ_ACTION = "privileged_policy_operation_summary.read"
MERGE_TRAIN_POLICY_OPERATION_PROPOSE_ACTION = "merge_train_policy_operation.propose"
MERGE_TRAIN_POLICY_OPERATION_READ_ACTION = "merge_train_policy_operation.read"
MERGE_TRAIN_POLICY_OPERATION_CANCEL_ACTION = "merge_train_policy_operation.cancel"
MERGE_TRAIN_POLICY_OPERATION_APPROVE_ACTION = "merge_train_policy_operation.approve"
MERGE_TRAIN_POLICY_OPERATION_REVOKE_ACTION = "merge_train_policy_operation.revoke"
MERGE_TRAIN_POLICY_OPERATION_SUMMARY_READ_ACTION = (
    "privileged_merge_train_policy_operation_summary.read"
)

PrivilegedOperationDescriptorId = Literal[
    "managed-secret-reencryption",
    "managed-authz-policy-set",
    "managed-merge-train-policy-import",
]
PrivilegedOperationSafetyClass = Literal["secret_backed", "policy_admin"]
PrivilegedOperationStatus = Literal[
    "planned",
    "approved",
    "revoked",
    "executing",
    "executed",
    "execution_failed",
    "expired",
    "cancelled",
]
PrivilegedOperationEventAction = Literal[
    "planned",
    "approved",
    "revoked",
    "executing",
    "executed",
    "execution_failed",
    "expired",
    "cancelled",
]
PrivilegedOperationEventWriteStatus = Literal["written", "replayed"]
PrivilegedOperationSourceKind = Literal["agent_api", "browser_api", "system"]

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OPERATION_ID_PATTERN = re.compile(r"^privileged-operation-[0-9a-f]{32}$")


class PrivilegedOperationConflictError(ValueError):
    pass


class PrivilegedOperationTransitionError(ValueError):
    pass


def _required_token(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must be non-empty")
    return normalized


def _sha256(value: str, field_name: str) -> str:
    normalized = _required_token(value, field_name).lower()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


def normalize_privileged_operation_source_event_id(value: str) -> str:
    normalized = _required_token(value, "source_event_id")
    if _SOURCE_EVENT_ID_PATTERN.fullmatch(normalized) is None:
        raise ValueError("Privileged-operation source_event_id is not canonical")
    return normalized


def _timestamp(value: str, field_name: str) -> str:
    normalized = _required_token(value, field_name)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _digest_payload(payload: object) -> str:
    return canonical_json_sha256(payload)


class PrivilegedOperationActor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identity_type: Literal["github_human", "system"]
    github_id: int = Field(default=0, ge=0)
    login: str

    @model_validator(mode="after")
    def _validate_actor(self) -> "PrivilegedOperationActor":
        object.__setattr__(self, "login", _required_token(self.login, "login"))
        if self.identity_type == "github_human":
            if self.github_id < 1:
                raise ValueError("GitHub-human privileged-operation actors require github_id")
        elif self.github_id != 0 or self.login != "system":
            raise ValueError("System privileged-operation actors must use the system identity")
        return self


class PrivilegedOperationAgentActor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identity_type: Literal["terminal_agent"] = "terminal_agent"
    login: Literal["terminal-agent"] = "terminal-agent"
    principal_sha256: str

    @model_validator(mode="after")
    def _validate_actor(self) -> "PrivilegedOperationAgentActor":
        object.__setattr__(
            self,
            "principal_sha256",
            _sha256(self.principal_sha256, "principal_sha256"),
        )
        return self


PrivilegedOperationRequester: TypeAlias = PrivilegedOperationActor | PrivilegedOperationAgentActor


class ManagedSecretReencryptionPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    reason: str = Field(min_length=1, max_length=240)
    source_label: str = Field(default="privileged-operation-plan", min_length=1, max_length=96)

    @model_validator(mode="after")
    def _validate_input(self) -> "ManagedSecretReencryptionPlanInput":
        if self.schema_version != 1:
            raise ValueError("Unsupported managed-secret re-encryption plan input schema version.")
        object.__setattr__(self, "reason", _required_token(self.reason, "reason"))
        object.__setattr__(
            self,
            "source_label",
            _required_token(self.source_label, "source_label"),
        )
        return self


class ManagedSecretReencryptionHumanEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    result_status: Literal["ok", "error"]
    plan_digest: str
    configured_secret_count: int = Field(ge=0)
    rotation_candidate_count: int = Field(ge=0)
    unchanged_count: int = Field(ge=0)
    unreadable_secret_count: int = Field(ge=0)
    active_key_id: str
    retirement_blocked_key_ids: tuple[str, ...] = ()
    retirement_ready_key_ids: tuple[str, ...] = ()
    legacy_compatibility_key_loaded: bool

    @model_validator(mode="after")
    def _validate_evidence(self) -> "ManagedSecretReencryptionHumanEvidence":
        if self.schema_version != 1:
            raise ValueError("Unsupported managed-secret re-encryption evidence schema version.")
        object.__setattr__(self, "plan_digest", _sha256(self.plan_digest, "plan_digest"))
        object.__setattr__(
            self,
            "active_key_id",
            _required_token(self.active_key_id, "active_key_id"),
        )
        blocked = tuple(
            _required_token(key_id, "retirement_blocked_key_ids")
            for key_id in self.retirement_blocked_key_ids
        )
        ready = tuple(
            _required_token(key_id, "retirement_ready_key_ids")
            for key_id in self.retirement_ready_key_ids
        )
        if len(set(blocked)) != len(blocked) or len(set(ready)) != len(ready):
            raise ValueError("Managed-secret retirement key IDs must be unique")
        if set(blocked).intersection(ready):
            raise ValueError("Managed-secret retirement key IDs cannot be both blocked and ready")
        object.__setattr__(self, "retirement_blocked_key_ids", tuple(sorted(blocked)))
        object.__setattr__(self, "retirement_ready_key_ids", tuple(sorted(ready)))
        if self.configured_secret_count != self.rotation_candidate_count + self.unchanged_count:
            raise ValueError("Configured-secret count must equal candidate plus unchanged counts")
        if self.result_status == "ok" and self.unreadable_secret_count:
            raise ValueError("Successful re-encryption evidence cannot contain unreadable secrets")
        if self.result_status == "error" and self.unreadable_secret_count < 1:
            raise ValueError("Error re-encryption evidence requires unreadable secrets")
        return self


class ManagedAuthzPolicySetProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    managed_set_id: str = Field(min_length=1, max_length=96)
    desired_policy: LaunchplaneAuthzPolicy
    administrator_quorum_change: int | None = Field(default=None, ge=1)
    reason: str = Field(min_length=1, max_length=240)
    related_issue: str = Field(default="", max_length=128)

    @model_validator(mode="after")
    def _validate_input(self) -> "ManagedAuthzPolicySetProposalInput":
        if self.schema_version != 1:
            raise ValueError("Unsupported managed authz policy proposal schema version.")
        reason = _required_token(self.reason, "reason")
        related_issue = self.related_issue.strip()
        reconcile_request = AuthzManagedPolicyReconcileEnvelope(
            schema_version=2,
            product="launchplane",
            mode="dry_run",
            managed_set_id=self.managed_set_id,
            schema_migration="reject",
            unmanaged_adoption="reject",
            administrator_quorum_change=self.administrator_quorum_change,
            reason=reason,
            related_issue=related_issue,
            desired_policy=self.desired_policy,
        )
        object.__setattr__(self, "managed_set_id", reconcile_request.managed_set_id)
        object.__setattr__(self, "desired_policy", reconcile_request.desired_policy)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "related_issue", related_issue)
        return self

    def reconcile_request(
        self,
        *,
        mode: Literal["dry_run", "apply"] = "dry_run",
        reviewed_plan_sha256: str = "",
    ) -> AuthzManagedPolicyReconcileEnvelope:
        return AuthzManagedPolicyReconcileEnvelope(
            schema_version=2,
            product="launchplane",
            mode=mode,
            managed_set_id=self.managed_set_id,
            schema_migration="reject",
            unmanaged_adoption="reject",
            administrator_quorum_change=self.administrator_quorum_change,
            reason=self.reason,
            related_issue=self.related_issue,
            reviewed_plan_sha256=reviewed_plan_sha256,
            desired_policy=self.desired_policy,
        )


class ManagedAuthzPolicySetHumanEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    result_status: Literal["ok", "blocked"]
    plan_digest: str
    diff: AuthzManagedPolicyDiff

    @model_validator(mode="after")
    def _validate_evidence(self) -> "ManagedAuthzPolicySetHumanEvidence":
        if self.schema_version != 1:
            raise ValueError("Unsupported managed authz policy evidence schema version.")
        object.__setattr__(self, "plan_digest", _sha256(self.plan_digest, "plan_digest"))
        if self.plan_digest != self.diff.plan_sha256:
            raise ValueError("Managed authz policy evidence must bind the exact plan digest.")
        blocked = bool(
            self.diff.policy_safety_blocker_count
            or self.diff.operational_readiness_blocked_rule_count
        )
        if blocked != (self.result_status == "blocked"):
            raise ValueError("Managed authz policy evidence status does not match blockers.")
        return self


class ManagedMergeTrainPolicyImportProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    record: MergeTrainPolicyRecord
    reason: str = Field(min_length=1, max_length=240)
    related_issue: str = Field(default="", max_length=128)

    @model_validator(mode="after")
    def _validate_input(self) -> "ManagedMergeTrainPolicyImportProposalInput":
        if self.schema_version != 1:
            raise ValueError("Unsupported merge-train policy import schema version.")
        if self.record.status != "active":
            raise ValueError("Merge-train policy import candidate record must be active.")
        normalize_merge_train_policy_timestamp(self.record.updated_at)
        object.__setattr__(self, "reason", _required_token(self.reason, "reason"))
        object.__setattr__(self, "related_issue", self.related_issue.strip())
        return self


class ManagedMergeTrainPolicyImportHumanEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    result_status: Literal["ok"] = "ok"
    plan_digest: str
    active_record_id: str
    active_status: Literal["active"] = "active"
    active_updated_at: str
    active_policy_sha256: str
    active_target_count: int = Field(ge=0)
    candidate_record_id: str
    candidate_policy_sha256: str
    candidate_target_count: int = Field(ge=0)
    added_policy_keys: tuple[str, ...] = ()
    removed_policy_keys: tuple[str, ...] = ()
    changed_policy_keys: tuple[str, ...] = ()
    unchanged_policy_keys: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_evidence(self) -> "ManagedMergeTrainPolicyImportHumanEvidence":
        if self.schema_version != 1:
            raise ValueError("Unsupported merge-train policy import evidence schema version.")
        for field_name in ("plan_digest", "active_policy_sha256", "candidate_policy_sha256"):
            object.__setattr__(
                self, field_name, _sha256(str(getattr(self, field_name)), field_name)
            )
        for field_name in ("active_record_id", "candidate_record_id"):
            object.__setattr__(
                self,
                field_name,
                _required_token(str(getattr(self, field_name)), field_name),
            )
        object.__setattr__(
            self, "active_updated_at", _timestamp(self.active_updated_at, "active_updated_at")
        )
        for field_name in (
            "added_policy_keys",
            "removed_policy_keys",
            "changed_policy_keys",
            "unchanged_policy_keys",
        ):
            policy_keys = tuple(
                sorted(
                    _required_token(policy_key, field_name)
                    for policy_key in getattr(self, field_name)
                )
            )
            if len(set(policy_keys)) != len(policy_keys):
                raise ValueError("Merge-train policy evidence keys must be unique")
            object.__setattr__(self, field_name, policy_keys)
        changed_sets = (
            set(self.added_policy_keys),
            set(self.removed_policy_keys),
            set(self.changed_policy_keys),
            set(self.unchanged_policy_keys),
        )
        if sum(len(policy_keys) for policy_keys in changed_sets) != len(set().union(*changed_sets)):
            raise ValueError("Merge-train policy evidence change buckets must not overlap")
        if self.active_target_count != len(self.removed_policy_keys) + len(
            self.changed_policy_keys
        ) + len(self.unchanged_policy_keys):
            raise ValueError("Active merge-train policy target count does not match change buckets")
        if self.candidate_target_count != len(self.added_policy_keys) + len(
            self.changed_policy_keys
        ) + len(self.unchanged_policy_keys):
            raise ValueError(
                "Candidate merge-train policy target count does not match change buckets"
            )
        return self


class PrivilegedOperationApproval(BaseModel):
    """Immutable browser-human authorization for a single planned operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    approver: PrivilegedOperationActor
    descriptor_id: PrivilegedOperationDescriptorId
    descriptor_version: int = Field(ge=1)
    request_digest: str
    evidence_digest: str
    plan_digest: str
    pre_state_digest: str
    policy_record_id: str
    policy_revision: int = Field(ge=1)
    policy_sha256: str
    policy_source: str
    managed_set_id: str
    managed_rule_id: str
    expires_at: str
    reason: str = Field(min_length=1, max_length=4000)
    rollback_class: Literal["key_retained", "policy_cas"] = "key_retained"

    @model_validator(mode="after")
    def _validate_approval(self) -> "PrivilegedOperationApproval":
        if self.schema_version != 1:
            raise ValueError("Unsupported privileged-operation approval schema version.")
        if self.approver.identity_type != "github_human":
            raise ValueError("Privileged-operation approvals require a GitHub-human approver")
        if self.descriptor_version != 1:
            raise ValueError("Unsupported privileged-operation approval descriptor version.")
        for field_name in (
            "request_digest",
            "evidence_digest",
            "plan_digest",
            "pre_state_digest",
            "policy_sha256",
        ):
            object.__setattr__(
                self, field_name, _sha256(str(getattr(self, field_name)), field_name)
            )
        for field_name in (
            "policy_record_id",
            "policy_source",
            "managed_set_id",
            "managed_rule_id",
            "reason",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_token(str(getattr(self, field_name)), field_name),
            )
        object.__setattr__(self, "expires_at", _timestamp(self.expires_at, "expires_at"))
        return self


class PrivilegedOperationExecutionEvidence(BaseModel):
    """Redacted terminal evidence written by the service-internal worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    result_status: Literal["ok", "error"]
    result_digest: str
    configured_secret_count: int = Field(ge=0)
    rotation_candidate_count: int = Field(ge=0)
    unchanged_count: int = Field(ge=0)
    unreadable_secret_count: int = Field(ge=0)
    reconciliation_required: bool
    failure_code: str = Field(default="", max_length=160)

    @model_validator(mode="after")
    def _validate_execution_evidence(self) -> "PrivilegedOperationExecutionEvidence":
        if self.schema_version != 1:
            raise ValueError("Unsupported privileged-operation execution evidence schema version.")
        object.__setattr__(self, "result_digest", _sha256(self.result_digest, "result_digest"))
        failure_code = self.failure_code.strip()
        object.__setattr__(self, "failure_code", failure_code)
        if self.result_status == "ok" and (failure_code or self.reconciliation_required):
            raise ValueError("Successful execution evidence cannot require reconciliation")
        if self.result_status == "error" and not failure_code:
            raise ValueError("Failed execution evidence requires a bounded failure code")
        return self


class ManagedAuthzPolicySetExecutionEvidence(BaseModel):
    """Redacted policy-record evidence written by the service-internal worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    result_status: Literal["ok", "error"]
    result_digest: str
    changed: bool
    previous_record_id: str = ""
    previous_revision: int = Field(default=0, ge=0)
    previous_policy_sha256: str = ""
    resulting_record_id: str = ""
    resulting_revision: int = Field(default=0, ge=0)
    resulting_policy_sha256: str = ""
    reconciliation_required: bool
    failure_code: str = Field(default="", max_length=160)

    @model_validator(mode="after")
    def _validate_execution_evidence(self) -> "ManagedAuthzPolicySetExecutionEvidence":
        if self.schema_version != 1:
            raise ValueError("Unsupported managed authz policy execution evidence schema version.")
        object.__setattr__(self, "result_digest", _sha256(self.result_digest, "result_digest"))
        failure_code = self.failure_code.strip()
        object.__setattr__(self, "failure_code", failure_code)
        if self.result_status == "ok":
            if failure_code or self.reconciliation_required:
                raise ValueError(
                    "Successful policy execution evidence cannot require reconciliation"
                )
            for field_name in (
                "previous_record_id",
                "previous_policy_sha256",
                "resulting_record_id",
                "resulting_policy_sha256",
            ):
                value = str(getattr(self, field_name))
                normalized = (
                    _sha256(value, field_name)
                    if field_name.endswith("sha256")
                    else _required_token(value, field_name)
                )
                object.__setattr__(self, field_name, normalized)
            if self.previous_revision < 1 or self.resulting_revision < 1:
                raise ValueError("Successful policy execution evidence requires policy revisions")
        elif not failure_code:
            raise ValueError("Failed policy execution evidence requires a bounded failure code")
        return self


class ManagedMergeTrainPolicyImportExecutionEvidence(BaseModel):
    """Redacted merge-train policy transition evidence written by the worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    result_status: Literal["ok", "error"]
    result_digest: str
    changed: bool
    previous_record_id: str = ""
    previous_policy_sha256: str = ""
    resulting_record_id: str = ""
    resulting_policy_sha256: str = ""
    superseded_record_id: str = ""
    superseded_policy_sha256: str = ""
    active_policy_count: int = Field(default=0, ge=0)
    reconciliation_required: bool
    failure_code: str = Field(default="", max_length=160)

    @model_validator(mode="after")
    def _validate_execution_evidence(self) -> "ManagedMergeTrainPolicyImportExecutionEvidence":
        if self.schema_version != 1:
            raise ValueError("Unsupported merge-train policy execution evidence schema version.")
        object.__setattr__(self, "result_digest", _sha256(self.result_digest, "result_digest"))
        failure_code = self.failure_code.strip()
        object.__setattr__(self, "failure_code", failure_code)
        if self.result_status == "ok":
            if failure_code or self.reconciliation_required:
                raise ValueError(
                    "Successful merge-train policy execution cannot require reconciliation"
                )
            if self.active_policy_count != 1:
                raise ValueError(
                    "Successful merge-train policy execution requires one active policy"
                )
            for field_name in (
                "previous_record_id",
                "resulting_record_id",
            ):
                object.__setattr__(
                    self,
                    field_name,
                    _required_token(str(getattr(self, field_name)), field_name),
                )
            for field_name in (
                "previous_policy_sha256",
                "resulting_policy_sha256",
            ):
                object.__setattr__(
                    self, field_name, _sha256(str(getattr(self, field_name)), field_name)
                )
            if self.changed:
                object.__setattr__(
                    self,
                    "superseded_record_id",
                    _required_token(self.superseded_record_id, "superseded_record_id"),
                )
                object.__setattr__(
                    self,
                    "superseded_policy_sha256",
                    _sha256(self.superseded_policy_sha256, "superseded_policy_sha256"),
                )
            else:
                if self.superseded_record_id or self.superseded_policy_sha256:
                    raise ValueError(
                        "Unchanged merge-train policy execution cannot supersede a record"
                    )
        elif not failure_code:
            raise ValueError("Failed merge-train policy execution requires a bounded failure code")
        return self


PrivilegedOperationRequest: TypeAlias = (
    ManagedSecretReencryptionPlanInput
    | ManagedAuthzPolicySetProposalInput
    | ManagedMergeTrainPolicyImportProposalInput
)
PrivilegedOperationHumanEvidence: TypeAlias = (
    ManagedSecretReencryptionHumanEvidence
    | ManagedAuthzPolicySetHumanEvidence
    | ManagedMergeTrainPolicyImportHumanEvidence
)
PrivilegedOperationTerminalEvidence: TypeAlias = (
    PrivilegedOperationExecutionEvidence
    | ManagedAuthzPolicySetExecutionEvidence
    | ManagedMergeTrainPolicyImportExecutionEvidence
)


class PrivilegedOperationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    operation_id: str
    descriptor_id: PrivilegedOperationDescriptorId
    descriptor_version: int = Field(default=1, ge=1)
    safety_class: PrivilegedOperationSafetyClass
    status: PrivilegedOperationStatus
    source_event_id: str = Field(min_length=1, max_length=128)
    requested_by: PrivilegedOperationRequester
    request: PrivilegedOperationRequest
    request_digest: str
    evidence: PrivilegedOperationHumanEvidence
    evidence_digest: str
    created_at: str
    updated_at: str
    expires_at: str
    approval: PrivilegedOperationApproval | None = None
    execution: PrivilegedOperationTerminalEvidence | None = None
    terminal_at: str = ""
    terminal_reason: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def _validate_record(self) -> "PrivilegedOperationRecord":
        if self.schema_version != 1:
            raise ValueError("Unsupported privileged-operation record schema version.")
        if self.descriptor_version != 1:
            raise ValueError("Unsupported privileged-operation descriptor version.")
        operation_id = _required_token(self.operation_id, "operation_id")
        if _OPERATION_ID_PATTERN.fullmatch(operation_id) is None:
            raise ValueError("Privileged-operation operation_id is not canonical")
        object.__setattr__(self, "operation_id", operation_id)
        source_event_id = normalize_privileged_operation_source_event_id(self.source_event_id)
        object.__setattr__(self, "source_event_id", source_event_id)
        if self.descriptor_id == "managed-secret-reencryption":
            if self.safety_class != "secret_backed":
                raise ValueError("Managed-secret operations require secret-backed safety")
            if self.requested_by.identity_type != "github_human":
                raise ValueError("Managed-secret operations require a GitHub-human requester")
            if not isinstance(self.request, ManagedSecretReencryptionPlanInput) or not isinstance(
                self.evidence, ManagedSecretReencryptionHumanEvidence
            ):
                raise ValueError("Managed-secret operation payload types do not match descriptor")
            if self.execution is not None and not isinstance(
                self.execution, PrivilegedOperationExecutionEvidence
            ):
                raise ValueError("Managed-secret execution evidence does not match descriptor")
            if self.approval is not None and self.approval.rollback_class != "key_retained":
                raise ValueError("Managed-secret approvals require key-retained rollback")
        elif self.descriptor_id == "managed-authz-policy-set":
            if self.safety_class != "policy_admin":
                raise ValueError("Managed-policy operations require policy-admin safety")
            if self.requested_by.identity_type not in {"github_human", "terminal_agent"}:
                raise ValueError("Managed-policy operations require a human or agent requester")
            if not isinstance(self.request, ManagedAuthzPolicySetProposalInput) or not isinstance(
                self.evidence, ManagedAuthzPolicySetHumanEvidence
            ):
                raise ValueError("Managed-policy operation payload types do not match descriptor")
            if self.execution is not None and not isinstance(
                self.execution, ManagedAuthzPolicySetExecutionEvidence
            ):
                raise ValueError("Managed-policy execution evidence does not match descriptor")
            if self.approval is not None and self.approval.rollback_class != "policy_cas":
                raise ValueError("Managed-policy approvals require policy-CAS rollback")
        elif self.descriptor_id == "managed-merge-train-policy-import":
            if self.safety_class != "policy_admin":
                raise ValueError("Merge-train policy operations require policy-admin safety")
            if self.requested_by.identity_type not in {"github_human", "terminal_agent"}:
                raise ValueError("Merge-train policy operations require a human or agent requester")
            if not isinstance(
                self.request, ManagedMergeTrainPolicyImportProposalInput
            ) or not isinstance(self.evidence, ManagedMergeTrainPolicyImportHumanEvidence):
                raise ValueError(
                    "Merge-train policy operation payload types do not match descriptor"
                )
            if self.execution is not None and not isinstance(
                self.execution, ManagedMergeTrainPolicyImportExecutionEvidence
            ):
                raise ValueError("Merge-train policy execution evidence does not match descriptor")
            if self.approval is not None and self.approval.rollback_class != "policy_cas":
                raise ValueError("Merge-train policy approvals require policy-CAS rollback")
        else:
            raise ValueError("Unknown privileged-operation descriptor")
        object.__setattr__(self, "request_digest", _sha256(self.request_digest, "request_digest"))
        object.__setattr__(
            self,
            "evidence_digest",
            _sha256(self.evidence_digest, "evidence_digest"),
        )
        if self.request_digest not in privileged_operation_request_digest_candidates(self.request):
            raise ValueError("Privileged-operation request_digest does not match request")
        expected_evidence_digest = privileged_operation_evidence_digest(self.evidence)
        if self.evidence_digest != expected_evidence_digest:
            raise ValueError("Privileged-operation evidence_digest does not match evidence")
        object.__setattr__(self, "created_at", _timestamp(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _timestamp(self.updated_at, "updated_at"))
        object.__setattr__(self, "expires_at", _timestamp(self.expires_at, "expires_at"))
        created_at = datetime.fromisoformat(self.created_at)
        updated_at = datetime.fromisoformat(self.updated_at)
        expires_at = datetime.fromisoformat(self.expires_at)
        if updated_at < created_at:
            raise ValueError("Privileged-operation updated_at cannot precede created_at")
        if expires_at <= created_at:
            raise ValueError("Privileged-operation expires_at must follow created_at")
        terminal_reason = self.terminal_reason.strip()
        object.__setattr__(self, "terminal_reason", terminal_reason)
        if self.status == "planned":
            if self.terminal_at or terminal_reason:
                raise ValueError("Planned privileged operations cannot contain terminal evidence")
            if self.approval is not None or self.execution is not None:
                raise ValueError(
                    "Planned privileged operations cannot contain approval or execution evidence"
                )
        elif self.status in {"approved", "executing"}:
            if self.terminal_at or terminal_reason:
                raise ValueError("Active privileged operations cannot contain terminal evidence")
            if self.approval is None:
                raise ValueError("Approved privileged operations require approval evidence")
            if self.approval.descriptor_id != self.descriptor_id:
                raise ValueError(
                    "Privileged-operation approval descriptor does not match the record"
                )
            if self.approval.descriptor_version != self.descriptor_version:
                raise ValueError("Privileged-operation approval version does not match the record")
            if self.approval.request_digest != self.request_digest:
                raise ValueError(
                    "Privileged-operation approval request digest does not match the record"
                )
            if self.approval.evidence_digest != self.evidence_digest:
                raise ValueError(
                    "Privileged-operation approval evidence digest does not match the record"
                )
            if self.approval.plan_digest != self.evidence.plan_digest:
                raise ValueError(
                    "Privileged-operation approval plan digest does not match the record"
                )
            if self.approval.pre_state_digest != privileged_operation_pre_state_digest(
                self.evidence
            ):
                raise ValueError(
                    "Privileged-operation approval pre-state digest does not match the record"
                )
            approval_expires_at = datetime.fromisoformat(self.approval.expires_at)
            if approval_expires_at > expires_at or approval_expires_at <= created_at:
                raise ValueError(
                    "Privileged-operation approval expiry must remain within the plan lifetime"
                )
            if self.approval.expires_at != self.expires_at:
                raise ValueError("Privileged-operation approval expiry does not match the record")
            if self.status == "approved" and self.execution is not None:
                raise ValueError("Approved privileged operations cannot contain execution evidence")
            if self.status == "executing" and self.execution is not None:
                raise ValueError(
                    "Executing privileged operations cannot contain terminal execution evidence"
                )
        else:
            terminal_at = _timestamp(self.terminal_at, "terminal_at")
            object.__setattr__(self, "terminal_at", terminal_at)
            if datetime.fromisoformat(terminal_at) < created_at:
                raise ValueError("Privileged-operation terminal_at cannot precede created_at")
            if not terminal_reason:
                raise ValueError("Terminal privileged operations require terminal_reason")
            if self.status in {"revoked", "executed", "execution_failed"} and self.approval is None:
                raise ValueError(
                    "Approved privileged-operation terminals require approval evidence"
                )
            if self.status in {"executed", "execution_failed"} and self.execution is None:
                raise ValueError("Execution terminals require execution evidence")
            if self.status == "executed" and self.execution is not None:
                if self.execution.result_status != "ok":
                    raise ValueError(
                        "Executed privileged operations require successful execution evidence"
                    )
            if self.status == "execution_failed" and self.execution is not None:
                if self.execution.result_status != "error":
                    raise ValueError(
                        "Failed privileged operations require failed execution evidence"
                    )
        return self


class PrivilegedOperationEventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    event_id: str = ""
    operation_id: str
    sequence: int = Field(ge=1, le=4)
    action: PrivilegedOperationEventAction
    occurred_at: str
    source_kind: PrivilegedOperationSourceKind
    source_event_id: str = Field(min_length=1, max_length=128)
    actor: PrivilegedOperationRequester
    reason: str = Field(default="", max_length=4000)
    resulting_record_digest: str

    @model_validator(mode="after")
    def _validate_event(self) -> "PrivilegedOperationEventRecord":
        if self.schema_version != 1:
            raise ValueError("Unsupported privileged-operation event schema version.")
        operation_id = _required_token(self.operation_id, "operation_id")
        if _OPERATION_ID_PATTERN.fullmatch(operation_id) is None:
            raise ValueError("Privileged-operation event operation_id is not canonical")
        object.__setattr__(self, "operation_id", operation_id)
        object.__setattr__(self, "occurred_at", _timestamp(self.occurred_at, "occurred_at"))
        source_event_id = normalize_privileged_operation_source_event_id(self.source_event_id)
        object.__setattr__(self, "source_event_id", source_event_id)
        reason = self.reason.strip()
        object.__setattr__(self, "reason", reason)
        object.__setattr__(
            self,
            "resulting_record_digest",
            _sha256(self.resulting_record_digest, "resulting_record_digest"),
        )
        if self.action == "planned":
            expected_source_kind = (
                "agent_api" if self.actor.identity_type == "terminal_agent" else "browser_api"
            )
            if self.sequence != 1 or self.source_kind != expected_source_kind:
                raise ValueError("Planned events must use the requester's API surface")
            if self.actor.identity_type not in {"github_human", "terminal_agent"} or reason:
                raise ValueError("Planned events require a human or agent and no event reason")
        elif self.action in {"approved", "revoked", "cancelled"}:
            if self.sequence not in {2, 3} or self.source_kind != "browser_api":
                raise ValueError("Human privileged-operation events must be browser-authored")
            if self.actor.identity_type != "github_human" or not reason:
                raise ValueError(
                    "Human privileged-operation events require a GitHub human and reason"
                )
        else:
            if self.sequence not in {2, 3, 4} or self.source_kind != "system":
                raise ValueError("System privileged-operation events must be system-authored")
            if self.actor.identity_type != "system" or not reason:
                raise ValueError(
                    "System privileged-operation events require the system actor and reason"
                )
        computed_event_id = build_privileged_operation_event_id(
            operation_id=self.operation_id,
            action=self.action,
            source_kind=self.source_kind,
            source_event_id=self.source_event_id,
        )
        if self.event_id:
            event_id = _required_token(self.event_id, "event_id")
            if event_id != computed_event_id:
                raise ValueError("Privileged-operation event_id does not match event payload")
            object.__setattr__(self, "event_id", event_id)
        else:
            object.__setattr__(self, "event_id", computed_event_id)
        return self


class PrivilegedOperationAgentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    operation_id: str
    descriptor_id: PrivilegedOperationDescriptorId
    descriptor_version: int
    status: PrivilegedOperationStatus
    result_status: Literal["ok", "error"]
    configured_secret_count: int = Field(ge=0)
    rotation_candidate_count: int = Field(ge=0)
    unchanged_count: int = Field(ge=0)
    unreadable_secret_count: int = Field(ge=0)
    retirement_blocked_key_count: int = Field(ge=0)
    retirement_ready_key_count: int = Field(ge=0)
    legacy_compatibility_key_loaded: bool
    created_at: str
    expires_at: str


class ManagedAuthzPolicySetAgentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    operation_id: str
    descriptor_id: Literal["managed-authz-policy-set"] = "managed-authz-policy-set"
    descriptor_version: int
    status: PrivilegedOperationStatus
    result_status: Literal["ok", "blocked"]
    changed: bool
    added_rule_count: int = Field(ge=0)
    adopted_rule_count: int = Field(ge=0)
    updated_rule_count: int = Field(ge=0)
    removed_rule_count: int = Field(ge=0)
    unchanged_rule_count: int = Field(ge=0)
    policy_safety_blocker_count: int = Field(ge=0)
    operational_readiness_blocked_rule_count: int = Field(ge=0)
    created_at: str
    expires_at: str


class ManagedMergeTrainPolicyImportAgentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    operation_id: str
    descriptor_id: Literal["managed-merge-train-policy-import"] = (
        "managed-merge-train-policy-import"
    )
    descriptor_version: int
    status: PrivilegedOperationStatus
    result_status: Literal["ok"] = "ok"
    active_target_count: int = Field(ge=0)
    candidate_target_count: int = Field(ge=0)
    added_policy_keys: tuple[str, ...] = ()
    removed_policy_keys: tuple[str, ...] = ()
    changed_policy_keys: tuple[str, ...] = ()
    unchanged_policy_key_count: int = Field(ge=0)
    created_at: str
    expires_at: str


PrivilegedOperationSummary: TypeAlias = (
    PrivilegedOperationAgentSummary
    | ManagedAuthzPolicySetAgentSummary
    | ManagedMergeTrainPolicyImportAgentSummary
)


def privileged_operation_request_digest(request: PrivilegedOperationRequest) -> str:
    return _digest_payload(request.model_dump(mode="json"))


def privileged_operation_request_digest_candidates(
    request: PrivilegedOperationRequest,
) -> frozenset[str]:
    payload = request.model_dump(mode="json")
    candidates = {_digest_payload(payload)}
    if (
        isinstance(request, ManagedAuthzPolicySetProposalInput)
        and request.administrator_quorum_change is None
    ):
        legacy_payload = dict(payload)
        legacy_payload.pop("administrator_quorum_change", None)
        candidates.add(_digest_payload(legacy_payload))
    return frozenset(candidates)


def privileged_operation_evidence_digest(
    evidence: PrivilegedOperationHumanEvidence,
) -> str:
    return _digest_payload(evidence.model_dump(mode="json"))


def privileged_operation_pre_state_digest(
    evidence: PrivilegedOperationHumanEvidence,
) -> str:
    if isinstance(evidence, ManagedSecretReencryptionHumanEvidence):
        payload = evidence.model_dump(mode="json")
        payload.pop("plan_digest")
        return _digest_payload(payload)
    if isinstance(evidence, ManagedMergeTrainPolicyImportHumanEvidence):
        return _digest_payload(
            {
                "active_record_id": evidence.active_record_id,
                "active_status": evidence.active_status,
                "active_updated_at": evidence.active_updated_at,
                "active_policy_sha256": evidence.active_policy_sha256,
                "active_target_count": evidence.active_target_count,
            }
        )
    return _digest_payload(
        {
            "previous_record_id": evidence.diff.previous_record_id,
            "previous_revision": evidence.diff.previous_revision,
            "previous_policy_sha256": evidence.diff.previous_policy_sha256,
        }
    )


def privileged_operation_record_digest(record: PrivilegedOperationRecord) -> str:
    return _digest_payload(record.model_dump(mode="json"))


def privileged_operation_plan_replay_digest(record: PrivilegedOperationRecord) -> str:
    return _digest_payload(
        {
            "schema_version": record.schema_version,
            "operation_id": record.operation_id,
            "descriptor_id": record.descriptor_id,
            "descriptor_version": record.descriptor_version,
            "safety_class": record.safety_class,
            "source_event_id": record.source_event_id,
            "requested_by": record.requested_by.model_dump(mode="json"),
            "request": record.request.model_dump(mode="json"),
            "request_digest": record.request_digest,
            "evidence": record.evidence.model_dump(mode="json"),
            "evidence_digest": record.evidence_digest,
            "created_at": record.created_at,
            "expires_at": record.expires_at,
        }
    )


def privileged_operation_event_replay_digest(record: PrivilegedOperationEventRecord) -> str:
    return _digest_payload(record.model_dump(mode="json"))


def build_privileged_operation_id(*, github_id: int, source_event_id: str) -> str:
    if github_id < 1:
        raise ValueError("Privileged-operation IDs require a positive GitHub ID")
    normalized_source_event_id = normalize_privileged_operation_source_event_id(source_event_id)
    digest = _digest_payload([github_id, normalized_source_event_id])[:32]
    return f"privileged-operation-{digest}"


def build_privileged_operation_id_for_actor(
    *,
    descriptor_id: PrivilegedOperationDescriptorId,
    actor: PrivilegedOperationRequester,
    source_event_id: str,
) -> str:
    if descriptor_id == "managed-secret-reencryption":
        if not isinstance(actor, PrivilegedOperationActor) or actor.identity_type != "github_human":
            raise ValueError("Managed-secret operation IDs require a GitHub-human actor")
        return build_privileged_operation_id(
            github_id=actor.github_id,
            source_event_id=source_event_id,
        )
    normalized_source_event_id = normalize_privileged_operation_source_event_id(source_event_id)
    if isinstance(actor, PrivilegedOperationAgentActor):
        principal = f"agent:{actor.principal_sha256}"
    elif actor.identity_type == "github_human":
        principal = f"github:{actor.github_id}"
    else:
        raise ValueError("Managed-policy operation IDs require a human or agent actor")
    digest = _digest_payload([descriptor_id, principal, normalized_source_event_id])[:32]
    return f"privileged-operation-{digest}"


def terminal_agent_principal_sha256(*, subject: str, token_label: str) -> str:
    return _digest_payload(
        [
            "launchplane-terminal-agent-privileged-operation-v1",
            _required_token(subject, "subject"),
            _required_token(token_label, "token_label"),
        ]
    )


def build_privileged_operation_event_id(
    *,
    operation_id: str,
    action: PrivilegedOperationEventAction,
    source_kind: PrivilegedOperationSourceKind,
    source_event_id: str,
) -> str:
    digest = _digest_payload([operation_id, action, source_kind, source_event_id])[:32]
    return f"privileged-operation-event-{digest}"


def validate_privileged_operation_transition(
    *,
    previous: PrivilegedOperationRecord | None,
    proposed: PrivilegedOperationRecord,
    event: PrivilegedOperationEventRecord,
) -> None:
    if event.operation_id != proposed.operation_id:
        raise PrivilegedOperationTransitionError(
            "Privileged-operation event must target the proposed operation record"
        )
    if event.resulting_record_digest != privileged_operation_record_digest(proposed):
        raise PrivilegedOperationTransitionError(
            "Privileged-operation event digest must bind the proposed operation record"
        )
    if previous is None:
        if proposed.status != "planned" or event.action != "planned" or event.sequence != 1:
            raise PrivilegedOperationTransitionError(
                "Privileged-operation creation requires a planned record and first planned event"
            )
        return
    if proposed.operation_id != previous.operation_id:
        raise PrivilegedOperationTransitionError("Privileged-operation ID cannot change")
    for field_name in (
        "descriptor_id",
        "descriptor_version",
        "safety_class",
        "source_event_id",
        "requested_by",
        "request",
        "request_digest",
        "evidence",
        "evidence_digest",
        "created_at",
        "expires_at",
    ):
        if getattr(proposed, field_name) != getattr(previous, field_name):
            raise PrivilegedOperationTransitionError(
                f"Privileged-operation transition cannot change {field_name}"
            )
    if proposed.status != "approved" and proposed.approval != previous.approval:
        raise PrivilegedOperationTransitionError(
            "Privileged-operation transition cannot change approval"
        )
    transitions: dict[str, tuple[tuple[str, ...], int]] = {
        "approved": (("planned",), 2),
        "revoked": (("approved",), 3),
        "executing": (("approved",), 3),
        "executed": (("executing",), 4),
        "execution_failed": (("executing",), 4),
        "expired": (("planned", "approved"), 2 if previous.status == "planned" else 3),
        "cancelled": (("planned",), 2),
    }
    expected = transitions.get(proposed.status)
    if expected is None or previous.status not in expected[0] or event.action != proposed.status:
        raise PrivilegedOperationTransitionError("Privileged-operation transition is not allowed")
    if event.sequence != expected[1] or event.occurred_at != proposed.updated_at:
        raise PrivilegedOperationTransitionError(
            "Privileged-operation event must bind the transition timestamp"
        )
    if proposed.status in {"revoked", "executed", "execution_failed", "expired", "cancelled"}:
        if event.occurred_at != proposed.terminal_at or event.reason != proposed.terminal_reason:
            raise PrivilegedOperationTransitionError(
                "Terminal privileged-operation event must bind terminal evidence"
            )
    elif proposed.status == "approved" and (
        proposed.approval is None or event.reason != proposed.approval.reason
    ):
        raise PrivilegedOperationTransitionError(
            "Approved privileged-operation event must bind approval evidence"
        )


def privileged_operation_agent_summary(
    record: PrivilegedOperationRecord,
) -> PrivilegedOperationSummary:
    evidence = record.evidence
    if isinstance(evidence, ManagedMergeTrainPolicyImportHumanEvidence):
        return ManagedMergeTrainPolicyImportAgentSummary(
            operation_id=record.operation_id,
            descriptor_version=record.descriptor_version,
            status=record.status,
            active_target_count=evidence.active_target_count,
            candidate_target_count=evidence.candidate_target_count,
            added_policy_keys=evidence.added_policy_keys,
            removed_policy_keys=evidence.removed_policy_keys,
            changed_policy_keys=evidence.changed_policy_keys,
            unchanged_policy_key_count=len(evidence.unchanged_policy_keys),
            created_at=record.created_at,
            expires_at=record.expires_at,
        )
    if isinstance(evidence, ManagedAuthzPolicySetHumanEvidence):
        return ManagedAuthzPolicySetAgentSummary(
            operation_id=record.operation_id,
            descriptor_version=record.descriptor_version,
            status=record.status,
            result_status=evidence.result_status,
            changed=evidence.diff.changed,
            added_rule_count=evidence.diff.added_rule_count,
            adopted_rule_count=evidence.diff.adopted_rule_count,
            updated_rule_count=evidence.diff.updated_rule_count,
            removed_rule_count=evidence.diff.removed_rule_count,
            unchanged_rule_count=evidence.diff.unchanged_rule_count,
            policy_safety_blocker_count=evidence.diff.policy_safety_blocker_count,
            operational_readiness_blocked_rule_count=(
                evidence.diff.operational_readiness_blocked_rule_count
            ),
            created_at=record.created_at,
            expires_at=record.expires_at,
        )
    return PrivilegedOperationAgentSummary(
        operation_id=record.operation_id,
        descriptor_id=record.descriptor_id,
        descriptor_version=record.descriptor_version,
        status=record.status,
        result_status=evidence.result_status,
        configured_secret_count=evidence.configured_secret_count,
        rotation_candidate_count=evidence.rotation_candidate_count,
        unchanged_count=evidence.unchanged_count,
        unreadable_secret_count=evidence.unreadable_secret_count,
        retirement_blocked_key_count=len(evidence.retirement_blocked_key_ids),
        retirement_ready_key_count=len(evidence.retirement_ready_key_ids),
        legacy_compatibility_key_loaded=evidence.legacy_compatibility_key_loaded,
        created_at=record.created_at,
        expires_at=record.expires_at,
    )
