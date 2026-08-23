from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PRIVILEGED_SECRET_OPERATION_PLAN_ACTION = "privileged_secret_operation.plan"
PRIVILEGED_SECRET_OPERATION_READ_ACTION = "privileged_secret_operation.read"
PRIVILEGED_SECRET_OPERATION_CANCEL_ACTION = "privileged_secret_operation.cancel"
PRIVILEGED_SECRET_OPERATION_APPROVE_ACTION = "privileged_secret_operation.approve"
PRIVILEGED_SECRET_OPERATION_REVOKE_ACTION = "privileged_secret_operation.revoke"
PRIVILEGED_OPERATION_SUMMARY_READ_ACTION = "privileged_operation_summary.read"

PrivilegedOperationDescriptorId = Literal["managed-secret-reencryption"]
PrivilegedOperationSafetyClass = Literal["secret_backed"]
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
PrivilegedOperationSourceKind = Literal["browser_api", "system"]

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
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class PrivilegedOperationActor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identity_type: Literal["github_human", "system"]
    github_id: int = Field(default=0, ge=0)
    login: str

    @model_validator(mode="after")
    def _validate_actor(self) -> "PrivilegedOperationActor":
        object.__setattr__(self, "login", _required_token(self.login, "login"))
        if self.identity_type == "github_human" and self.github_id < 1:
            raise ValueError("GitHub-human privileged-operation actors require github_id")
        if self.identity_type == "system" and (self.github_id != 0 or self.login != "system"):
            raise ValueError("System privileged-operation actors must use the system identity")
        return self


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
    rollback_class: Literal["reconciliation_required"] = "reconciliation_required"

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


class PrivilegedOperationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    operation_id: str
    descriptor_id: PrivilegedOperationDescriptorId
    descriptor_version: int = Field(default=1, ge=1)
    safety_class: PrivilegedOperationSafetyClass
    status: PrivilegedOperationStatus
    source_event_id: str = Field(min_length=1, max_length=128)
    requested_by: PrivilegedOperationActor
    request: ManagedSecretReencryptionPlanInput
    request_digest: str
    evidence: ManagedSecretReencryptionHumanEvidence
    evidence_digest: str
    created_at: str
    updated_at: str
    expires_at: str
    approval: PrivilegedOperationApproval | None = None
    execution: PrivilegedOperationExecutionEvidence | None = None
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
        if self.requested_by.identity_type != "github_human":
            raise ValueError("Privileged-operation plans require a GitHub-human requester")
        object.__setattr__(self, "request_digest", _sha256(self.request_digest, "request_digest"))
        object.__setattr__(
            self,
            "evidence_digest",
            _sha256(self.evidence_digest, "evidence_digest"),
        )
        expected_request_digest = privileged_operation_request_digest(self.request)
        if self.request_digest != expected_request_digest:
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
    actor: PrivilegedOperationActor
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
            if self.sequence != 1 or self.source_kind != "browser_api":
                raise ValueError("Planned events must be the first browser-authored event")
            if self.actor.identity_type != "github_human" or reason:
                raise ValueError("Planned events require a GitHub human and no event reason")
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


def privileged_operation_request_digest(request: ManagedSecretReencryptionPlanInput) -> str:
    return _digest_payload(request.model_dump(mode="json"))


def privileged_operation_evidence_digest(
    evidence: ManagedSecretReencryptionHumanEvidence,
) -> str:
    return _digest_payload(evidence.model_dump(mode="json"))


def privileged_operation_pre_state_digest(
    evidence: ManagedSecretReencryptionHumanEvidence,
) -> str:
    payload = evidence.model_dump(mode="json")
    payload.pop("plan_digest")
    return _digest_payload(payload)


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
    transitions: dict[str, tuple[str, int]] = {
        "approved": ("planned", 2),
        "revoked": ("approved", 3),
        "executing": ("approved", 3),
        "executed": ("executing", 4),
        "execution_failed": ("executing", 4),
        "expired": ("planned", 2),
        "cancelled": ("planned", 2),
    }
    expected = transitions.get(proposed.status)
    if expected is None or previous.status != expected[0] or event.action != proposed.status:
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
) -> PrivilegedOperationAgentSummary:
    evidence = record.evidence
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
