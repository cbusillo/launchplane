from __future__ import annotations

from typing import Literal, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


RECORD_KIND = "proposed_ordinary_agent_v1"
INPUT_DOMAIN_ID = "ordinary-agent-effective-inputs-v1"
EVALUATOR_SEMANTICS_VERSION = "ordinary-agent-eligibility-v1"

RecordKind = Literal["proposed_ordinary_agent_v1"]
AuthorityState = Literal["inert"]
PrincipalProfile = Literal["read_only", "guarded_executor"]
PrincipalStatus = Literal["active", "revoked"]
OrdinaryAgentAction = Literal["self_read", "preflight", "guarded_merge"]
PolicyDecision = Literal["allow", "deny"]
EligibilityDecision = Literal["eligible", "denied"]
OrdinaryAgentReasonCode = Literal[
    "eligible",
    "policy_allowed",
    "principal_revoked",
    "principal_read_only",
    "lease_target_mismatch",
    "credential_principal_mismatch",
    "session_principal_mismatch",
    "lease_principal_mismatch",
    "session_credential_mismatch",
    "credential_version_rotated",
    "credential_digest_rotated",
    "lease_session_mismatch",
    "request_chain_mismatch",
    "request_principal_mismatch",
    "session_outside_credential_lifetime",
    "lease_outside_session_lifetime",
    "credential_not_yet_valid",
    "credential_expired",
    "credential_revoked",
    "session_not_yet_valid",
    "session_expired",
    "session_revoked",
    "lease_not_yet_valid",
    "lease_expired",
    "lease_revoked",
    "bound_rule_missing",
    "bound_rule_ambiguous",
    "rule_principal_mismatch",
    "rule_target_mismatch",
    "action_not_allowed",
    "effective_decision_fingerprint_mismatch",
    "request_action_mismatch",
    "budget_window_inactive",
    "budget_exhausted",
]
EffectState = Literal[
    "in_flight",
    "unknown_reconciliation_required",
    "completed",
    "stopped_budget_exhausted",
    "partially_completed",
]


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class InertRecord(StrictFrozenModel):
    record_kind: RecordKind
    authority_state: AuthorityState
    authorizes_execution: Literal[False]

    @field_validator("authorizes_execution", mode="before")
    @classmethod
    def validate_literal_false(cls, value: object) -> object:
        if value is not False:
            raise ValueError("authorizes_execution must be the JSON boolean false")
        return value


class OrdinaryAgentPrincipal(InertRecord):
    record_id: str = Field(min_length=1, max_length=256)
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    execution_profile: PrincipalProfile
    status: PrincipalStatus


class OrdinaryAgentTarget(StrictFrozenModel):
    repository_id: int = Field(gt=0, le=2**63 - 1)
    repository: str = Field(max_length=140, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_branch: str = Field(min_length=1, max_length=255)

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        if any(component in {".", ".."} for component in value.split("/")):
            raise ValueError("repository owner and name must be canonical exact components")
        return value

    @field_validator("base_branch")
    @classmethod
    def validate_base_branch(cls, value: str) -> str:
        invalid_fragments = ("..", "@{", "\\", ":", "//")
        if (
            value != value.strip()
            or value == "@"
            or value.startswith(("/", ".", "-"))
            or value.endswith(("/", ".", ".lock"))
            or any(fragment in value for fragment in invalid_fragments)
            or any(
                character.isspace() or ord(character) < 32 or ord(character) == 127
                for character in value
            )
            or any(character in value for character in "*?[]~^")
            or any(
                component.startswith(".") or component.endswith(".lock")
                for component in value.split("/")
            )
        ):
            raise ValueError("base branch must be one exact canonical Git ref name")
        return value


class OrdinaryAgentCredentialEvidence(InertRecord):
    record_id: str = Field(min_length=1, max_length=256)
    credential_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    credential_version: int = Field(ge=1, le=2**63 - 1)
    credential_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    valid_from: int = Field(ge=0, le=2**63 - 1)
    expires_at: int = Field(ge=0, le=2**63 - 1)
    revoked_at: int | None = Field(ge=0, le=2**63 - 1)

    @model_validator(mode="after")
    def validate_lifetime(self) -> OrdinaryAgentCredentialEvidence:
        if self.expires_at <= self.valid_from:
            raise ValueError("credential expiry must be after valid_from")
        return self


class OrdinaryAgentSession(InertRecord):
    record_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    credential_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    credential_version: int = Field(ge=1, le=2**63 - 1)
    credential_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid_from: int = Field(ge=0, le=2**63 - 1)
    expires_at: int = Field(ge=0, le=2**63 - 1)
    revoked_at: int | None = Field(ge=0, le=2**63 - 1)

    @model_validator(mode="after")
    def validate_lifetime(self) -> OrdinaryAgentSession:
        if self.expires_at <= self.valid_from:
            raise ValueError("session expiry must be after valid_from")
        return self


class OrdinaryAgentPolicyRule(StrictFrozenModel):
    managed_set_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
    managed_rule_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    target: OrdinaryAgentTarget
    actions: tuple[OrdinaryAgentAction, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_actions(self) -> OrdinaryAgentPolicyRule:
        if len(set(self.actions)) != len(self.actions):
            raise ValueError("policy rule actions must be unique")
        return self


class OrdinaryAgentPolicySnapshot(InertRecord):
    record_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1, le=2**63 - 1)
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_domain_id: Literal["ordinary-agent-effective-inputs-v1"]
    evaluator_semantics_version: Literal["ordinary-agent-eligibility-v1"]
    rules: tuple[OrdinaryAgentPolicyRule, ...]


class OrdinaryAgentBudget(StrictFrozenModel):
    window_start: int = Field(ge=0, le=2**63 - 1)
    window_end: int = Field(ge=0, le=2**63 - 1)
    action_limit: int = Field(ge=0, le=2**63 - 1)
    actions_used: int = Field(ge=0, le=2**63 - 1)
    pull_request_limit: int = Field(ge=0, le=2**63 - 1)
    pull_requests_used: int = Field(ge=0, le=2**63 - 1)

    @model_validator(mode="after")
    def validate_window(self) -> OrdinaryAgentBudget:
        if self.window_end <= self.window_start:
            raise ValueError("budget window end must be after its start")
        return self


class OrdinaryAgentLease(InertRecord):
    record_id: str = Field(min_length=1, max_length=256)
    lease_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    session_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    target: OrdinaryAgentTarget
    managed_set_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
    managed_rule_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
    action: OrdinaryAgentAction
    effective_decision_fingerprint: str = Field(pattern=r"^oae-fp-v1:[0-9a-f]{64}$")
    valid_from: int = Field(ge=0, le=2**63 - 1)
    expires_at: int = Field(ge=0, le=2**63 - 1)
    revoked_at: int | None = Field(ge=0, le=2**63 - 1)
    budget: OrdinaryAgentBudget

    @model_validator(mode="after")
    def validate_lifetime(self) -> OrdinaryAgentLease:
        if self.expires_at <= self.valid_from:
            raise ValueError("lease expiry must be after valid_from")
        return self


class OrdinaryAgentPullRequest(StrictFrozenModel):
    number: int = Field(gt=0, le=2**63 - 1)
    head_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class OrdinaryAgentRequest(InertRecord):
    record_id: str = Field(min_length=1, max_length=256)
    request_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    idempotency_key: str = Field(min_length=1, max_length=256)
    lease_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    session_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    target: OrdinaryAgentTarget
    base_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    action: OrdinaryAgentAction
    pull_requests: tuple[OrdinaryAgentPullRequest, ...]
    permitted_stack_edit_pull_requests: tuple[int, ...]

    @model_validator(mode="after")
    def validate_pull_request_scope(self) -> OrdinaryAgentRequest:
        numbers = tuple(item.number for item in self.pull_requests)
        if len(set(numbers)) != len(numbers):
            raise ValueError("request pull requests must be unique")
        if len(set(self.permitted_stack_edit_pull_requests)) != len(
            self.permitted_stack_edit_pull_requests
        ):
            raise ValueError("permitted stack edit pull requests must be unique")
        if not set(self.permitted_stack_edit_pull_requests).issubset(numbers):
            raise ValueError("permitted stack edits must be a subset of request pull requests")
        if self.action == "guarded_merge" and not self.pull_requests:
            raise ValueError("guarded merge requests require at least one pull request")
        if self.action != "guarded_merge" and self.permitted_stack_edit_pull_requests:
            raise ValueError("only guarded merge requests may permit stack edits")
        return self


class OrdinaryAgentQualificationRequest(InertRecord):
    """Eligibility evidence for a read-only qualification without merge fields."""

    record_id: str = Field(min_length=1, max_length=256)
    request_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    idempotency_key: str = Field(min_length=1, max_length=256)
    lease_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    session_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    target: OrdinaryAgentTarget
    action: Literal["preflight"] = "preflight"


OrdinaryAgentEligibilityRequest: TypeAlias = (
    OrdinaryAgentRequest | OrdinaryAgentQualificationRequest
)


class OrdinaryAgentPolicyEvaluation(InertRecord):
    record_id: str = Field(min_length=1, max_length=256)
    decision: PolicyDecision
    reason_code: OrdinaryAgentReasonCode
    policy_record_id: str = Field(min_length=1, max_length=256)
    policy_revision: int = Field(ge=1, le=2**63 - 1)
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    managed_set_id: str | None = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
    managed_rule_id: str | None = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
    bound_rule_actions: tuple[OrdinaryAgentAction, ...]
    effective_decision_fingerprint: str = Field(pattern=r"^oae-fp-v1:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_decision(self) -> OrdinaryAgentPolicyEvaluation:
        if (self.decision == "allow") != (self.reason_code == "policy_allowed"):
            raise ValueError("policy decision and reason must agree")
        if self.reason_code == "eligible":
            raise ValueError("policy evaluation is not eligibility")
        if self.decision == "allow" and (
            not self.managed_set_id or not self.managed_rule_id or not self.bound_rule_actions
        ):
            raise ValueError("allow requires an exact bound policy rule")
        return self


class OrdinaryAgentEligibilityResult(InertRecord):
    record_id: str = Field(min_length=1, max_length=256)
    evaluated_at: int = Field(ge=0, le=2**63 - 1)
    request_id: str = Field(min_length=1, max_length=256)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    session_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    lease_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    decision: EligibilityDecision
    reason_code: OrdinaryAgentReasonCode
    policy_record_id: str = Field(min_length=1, max_length=256)
    policy_revision: int = Field(ge=1, le=2**63 - 1)
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_decision_fingerprint: str = Field(pattern=r"^oae-fp-v1:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_decision(self) -> OrdinaryAgentEligibilityResult:
        if (self.decision == "eligible") != (self.reason_code == "eligible"):
            raise ValueError("eligibility decision and reason must agree")
        if self.reason_code == "policy_allowed":
            raise ValueError("policy permission alone is not eligibility")
        return self


class OrdinaryAgentEffectRecord(InertRecord):
    record_id: str = Field(min_length=1, max_length=256)
    request_id: str = Field(min_length=1, max_length=256)
    state: EffectState
    completed_effects: tuple[str, ...]
    active_reservations: tuple[str, ...]
    active_fences: tuple[str, ...]
    provider_ttl_residual_seconds: int | None = Field(ge=0, le=2**63 - 1)
    success: bool

    @model_validator(mode="after")
    def validate_state(self) -> OrdinaryAgentEffectRecord:
        if self.state == "completed" and (
            not self.success or self.active_reservations or self.active_fences
        ):
            raise ValueError("completed effects require success and no active reservation or fence")
        if self.state != "completed" and self.success:
            raise ValueError("only completed effect records may report success")
        if self.state == "unknown_reconciliation_required" and not (
            self.active_reservations or self.active_fences
        ):
            raise ValueError("unknown effects must retain their observed reservations or fences")
        if self.state == "in_flight" and not (self.active_reservations or self.active_fences):
            raise ValueError("in-flight effects require observed protection")
        if self.state == "stopped_budget_exhausted" and self.active_reservations:
            raise ValueError("known budget stop cannot conceal outstanding effect reservations")
        if self.state == "partially_completed" and (
            not self.completed_effects or not (self.active_reservations or self.active_fences)
        ):
            raise ValueError("partial effects require completed effects and unresolved protection")
        return self


StoredOrdinaryAgentResult = OrdinaryAgentEligibilityResult | OrdinaryAgentEffectRecord


class OrdinaryAgentEvidenceStore(Protocol):
    def read_snapshot(self, record_id: str) -> OrdinaryAgentPolicySnapshot | None: ...

    def put_result(self, result: StoredOrdinaryAgentResult) -> StoredOrdinaryAgentResult: ...

    def get_result(self, record_id: str) -> StoredOrdinaryAgentResult | None: ...
