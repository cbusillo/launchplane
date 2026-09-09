from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import re
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


EVERY_CODE_FEEDBACK_RESUME_REQUEST_ACTION: Final[Literal["every_code_feedback_resume.request"]] = (
    "every_code_feedback_resume.request"
)
EVERY_CODE_FEEDBACK_RESUME_EXECUTE_ACTION: Final[Literal["every_code_feedback_resume.execute"]] = (
    "every_code_feedback_resume.execute"
)
EVERY_CODE_FEEDBACK_MAX_AGE = timedelta(hours=24)
EVERY_CODE_FEEDBACK_MAX_FUTURE_SKEW = timedelta(minutes=5)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_POLICY_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,255}$")
_REPOSITORY = re.compile(r"^[^/\s]+/[^/\s]+$")

FeedbackKind = Literal["issue_comment", "pull_request_review", "pull_request_review_comment"]
FeedbackPolicyAction = Literal[
    "every_code_feedback_resume.request", "every_code_feedback_resume.execute"
]


def parse_every_code_feedback_timestamp(value: str, field_name: str = "timestamp") -> datetime:
    if not value or value.strip() != value:
        raise ValueError(f"{field_name} must be a canonical timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(UTC)


def _timestamp(value: str, field_name: str) -> str:
    parsed = parse_every_code_feedback_timestamp(value, field_name)
    canonical = parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if value != canonical:
        raise ValueError(f"{field_name} must use canonical UTC microsecond form")
    return value


def _token(value: str, field_name: str) -> str:
    if _TOKEN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a canonical identifier")
    return value


def _opaque_id(value: str, field_name: str) -> str:
    if (
        not value
        or len(value) > 512
        or not value.isascii()
        or not value.isprintable()
        or any(character.isspace() for character in value)
    ):
        raise ValueError(f"{field_name} must be a bounded canonical opaque identifier")
    return value


def _uuid4(value: str, field_name: str) -> str:
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as error:
        raise ValueError(f"{field_name} must be a canonical UUIDv4") from error
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{field_name} must be a canonical UUIDv4")
    return value


def _digest(value: str, field_name: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _canonical_digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def validate_every_code_feedback_revision_time(
    *, provider_updated_at: str, observed_at: str
) -> None:
    provider = parse_every_code_feedback_timestamp(provider_updated_at, "provider_updated_at")
    observed = parse_every_code_feedback_timestamp(observed_at, "observed_at")
    if provider < observed - EVERY_CODE_FEEDBACK_MAX_AGE:
        raise ValueError("feedback provider revision is older than 24 hours")
    if provider > observed + EVERY_CODE_FEEDBACK_MAX_FUTURE_SKEW:
        raise ValueError("feedback provider revision is more than five minutes in the future")


def every_code_feedback_eligible_until(*, first_received_at: str, provider_updated_at: str) -> str:
    received = parse_every_code_feedback_timestamp(first_received_at, "first_received_at")
    provider = parse_every_code_feedback_timestamp(provider_updated_at, "provider_updated_at")
    eligible = min(received + EVERY_CODE_FEEDBACK_MAX_AGE, provider + EVERY_CODE_FEEDBACK_MAX_AGE)
    return eligible.isoformat(timespec="microseconds").replace("+00:00", "Z")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class EveryCodeVerifiedFeedbackRevision(_StrictFrozenModel):
    repository_owner_id: StrictInt = Field(gt=0)
    repository_id: StrictInt = Field(gt=0)
    repository: str
    pull_request_number: StrictInt = Field(gt=0)
    pull_request_node_id: str
    feedback_id: str
    feedback_kind: FeedbackKind
    object_node_id: str
    object_id: StrictInt = Field(gt=0)
    actor_github_id: StrictInt = Field(gt=0)
    actor_login: str = ""
    provider_updated_at: str
    body_sha256: str
    revision_digest: str = ""

    @model_validator(mode="after")
    def validate_revision(self) -> EveryCodeVerifiedFeedbackRevision:
        if (
            _REPOSITORY.fullmatch(self.repository) is None
            or self.repository != self.repository.lower()
        ):
            raise ValueError("repository must be canonical lowercase owner/name")
        _token(self.feedback_id, "feedback_id")
        for name in ("pull_request_node_id", "object_node_id"):
            _opaque_id(getattr(self, name), name)
        if self.actor_login and self.actor_login.strip() != self.actor_login:
            raise ValueError("actor_login must be canonical display metadata")
        _timestamp(self.provider_updated_at, "provider_updated_at")
        _digest(self.body_sha256, "body_sha256")
        expected = build_every_code_feedback_revision_digest(self)
        if self.revision_digest and self.revision_digest != expected:
            raise ValueError("revision_digest does not match immutable feedback identity")
        object.__setattr__(self, "revision_digest", expected)
        return self


def build_every_code_feedback_revision_digest(revision: EveryCodeVerifiedFeedbackRevision) -> str:
    return _canonical_digest(
        {
            "repository_owner_id": revision.repository_owner_id,
            "repository_id": revision.repository_id,
            "pull_request_number": revision.pull_request_number,
            "pull_request_node_id": revision.pull_request_node_id,
            "feedback_kind": revision.feedback_kind,
            "object_node_id": revision.object_node_id,
            "object_id": revision.object_id,
            "actor_github_id": revision.actor_github_id,
            "provider_updated_at": revision.provider_updated_at,
            "body_sha256": revision.body_sha256,
        }
    )


class EveryCodeFeedbackPolicyDecisionProvenance(_StrictFrozenModel):
    action: FeedbackPolicyAction
    product: Literal["launchplane"] = "launchplane"
    context: Literal["launchplane"] = "launchplane"
    instance: str
    decision: Literal["allowed"] = "allowed"
    managed_set_id: str
    managed_rule_id: str
    policy_record_id: str
    policy_revision: StrictInt = Field(gt=0)
    policy_sha256: str

    @model_validator(mode="after")
    def validate_provenance(self) -> EveryCodeFeedbackPolicyDecisionProvenance:
        if not self.instance.startswith("github-repository:"):
            raise ValueError("instance must identify one GitHub repository")
        repository_id = self.instance.removeprefix("github-repository:")
        if (
            not repository_id.isascii()
            or not repository_id.isdecimal()
            or int(repository_id) < 1
            or str(int(repository_id)) != repository_id
        ):
            raise ValueError("instance must contain a positive repository ID")
        for name in ("managed_set_id", "managed_rule_id", "policy_record_id"):
            if _POLICY_IDENTIFIER.fullmatch(getattr(self, name)) is None:
                raise ValueError(f"{name} must be a canonical policy identifier")
        _digest(self.policy_sha256, "policy_sha256")
        return self


class EveryCodeFeedbackAcceptanceRecord(_StrictFrozenModel):
    schema_version: Literal[1] = 1
    acceptance_id: str
    request_id: str
    issue_number: StrictInt = Field(gt=0)
    issue_url: str
    retained_pull_request_url: str
    revision: EveryCodeVerifiedFeedbackRevision
    policy: EveryCodeFeedbackPolicyDecisionProvenance
    github_delivery_id: str
    received_at: str
    created_at: str
    eligible_until: str
    status: Literal["accepted"] = "accepted"
    reason_code: str
    acceptance_digest: str = ""

    @model_validator(mode="after")
    def validate_acceptance(self) -> EveryCodeFeedbackAcceptanceRecord:
        for name in ("acceptance_id", "request_id", "github_delivery_id", "reason_code"):
            _token(getattr(self, name), name)
        if self.policy.action != EVERY_CODE_FEEDBACK_RESUME_REQUEST_ACTION:
            raise ValueError("acceptance requires request-action policy provenance")
        if self.policy.instance != f"github-repository:{self.revision.repository_id}":
            raise ValueError("acceptance policy repository does not match feedback")
        for name in ("received_at", "created_at", "eligible_until"):
            _timestamp(getattr(self, name), name)
        received = parse_every_code_feedback_timestamp(self.received_at, "received_at")
        created = parse_every_code_feedback_timestamp(self.created_at, "created_at")
        eligible = parse_every_code_feedback_timestamp(self.eligible_until, "eligible_until")
        if created < received or created >= eligible:
            raise ValueError("created_at must be at or after receipt and before expiry")
        validate_every_code_feedback_revision_time(
            provider_updated_at=self.revision.provider_updated_at, observed_at=self.received_at
        )
        expected_expiry = every_code_feedback_eligible_until(
            first_received_at=self.received_at,
            provider_updated_at=self.revision.provider_updated_at,
        )
        if self.eligible_until != expected_expiry:
            raise ValueError("eligible_until must use the fixed earliest 24-hour bound")
        expected = build_every_code_feedback_acceptance_digest(self)
        if self.acceptance_digest and self.acceptance_digest != expected:
            raise ValueError("acceptance_digest does not match immutable acceptance evidence")
        object.__setattr__(self, "acceptance_digest", expected)
        return self


def build_every_code_feedback_acceptance_digest(
    record: EveryCodeFeedbackAcceptanceRecord,
) -> str:
    payload = record.model_dump(mode="json", exclude={"acceptance_digest"})
    payload["revision"].pop("actor_login", None)
    return _canonical_digest(payload)


class EveryCodeFeedbackPullRequestOpenObservation(_StrictFrozenModel):
    """Persisted provider evidence; this type itself supplies no authority."""

    repository_id: StrictInt = Field(gt=0)
    repository_owner_id: StrictInt = Field(gt=0)
    pull_request_number: StrictInt = Field(gt=0)
    pull_request_node_id: str
    state: Literal["open"] = "open"
    observed_at: str
    observation_digest: str = ""

    @model_validator(mode="after")
    def validate_observation(self) -> EveryCodeFeedbackPullRequestOpenObservation:
        _opaque_id(self.pull_request_node_id, "pull_request_node_id")
        _timestamp(self.observed_at, "observed_at")
        expected = _canonical_digest(self.model_dump(mode="json", exclude={"observation_digest"}))
        if self.observation_digest and self.observation_digest != expected:
            raise ValueError("observation_digest does not match PR observation")
        object.__setattr__(self, "observation_digest", expected)
        return self


class EveryCodeFeedbackResumeIntentRecord(_StrictFrozenModel):
    schema_version: Literal[1, 2] = 1
    intent_id: str
    request_id: str
    acceptance_id: str
    acceptance_digest: str
    expected_lifecycle_id: str
    expected_terminal_state: Literal["done", "blocked"]
    expected_fencing_token: StrictInt = Field(ge=0)
    retained_host: str
    retained_pull_request_url: str
    issued_at: str
    eligible_until: str
    worker_idempotency_key: str
    issuance_policy: EveryCodeFeedbackPolicyDecisionProvenance | None = None
    open_observation: EveryCodeFeedbackPullRequestOpenObservation | None = None
    intent_digest: str = ""

    @model_validator(mode="after")
    def validate_intent(self) -> EveryCodeFeedbackResumeIntentRecord:
        for name in (
            "intent_id",
            "request_id",
            "acceptance_id",
            "expected_lifecycle_id",
            "retained_host",
            "worker_idempotency_key",
        ):
            _token(getattr(self, name), name)
        _digest(self.acceptance_digest, "acceptance_digest")
        issued = parse_every_code_feedback_timestamp(self.issued_at, "issued_at")
        eligible = parse_every_code_feedback_timestamp(self.eligible_until, "eligible_until")
        _timestamp(self.issued_at, "issued_at")
        _timestamp(self.eligible_until, "eligible_until")
        if eligible <= issued or eligible > issued + EVERY_CODE_FEEDBACK_MAX_AGE:
            raise ValueError("intent expiry must be after issuance and within 24 hours")
        if self.schema_version == 1:
            if self.issuance_policy is not None or self.open_observation is not None:
                raise ValueError("legacy intent cannot claim verified issuance evidence")
        else:
            if self.issuance_policy is None or self.open_observation is None:
                raise ValueError("minted intent requires issuance policy and open observation")
            if (
                self.issuance_policy.action != EVERY_CODE_FEEDBACK_RESUME_REQUEST_ACTION
                or self.issuance_policy.instance
                != f"github-repository:{self.open_observation.repository_id}"
            ):
                raise ValueError("minted intent policy does not match PR observation")
        expected = build_every_code_feedback_resume_intent_digest(self)
        if self.intent_digest and self.intent_digest != expected:
            raise ValueError("intent_digest does not match immutable intent")
        object.__setattr__(self, "intent_digest", expected)
        return self


def build_every_code_feedback_resume_intent_digest(
    record: EveryCodeFeedbackResumeIntentRecord,
) -> str:
    payload = record.model_dump(mode="json", exclude={"intent_digest"})
    if record.schema_version == 1:
        # Preserve the historical v1 digest; absent v2 proof never becomes authority.
        payload.pop("issuance_policy", None)
        payload.pop("open_observation", None)
    return _canonical_digest(payload)


class EveryCodeFeedbackLaunchBinding(_StrictFrozenModel):
    request_id: str
    lifecycle_id: str
    fencing_token: StrictInt = Field(gt=0)
    host: str
    launch_nonce: str
    launch_attempt: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def validate_binding(self) -> EveryCodeFeedbackLaunchBinding:
        for name in ("request_id", "host", "launch_nonce"):
            _token(getattr(self, name), name)
        _uuid4(self.lifecycle_id, "lifecycle_id")
        return self


class EveryCodeFeedbackProcessBindingRecord(_StrictFrozenModel):
    binding: EveryCodeFeedbackLaunchBinding
    session_name: str
    process_id: StrictInt = Field(gt=0)
    process_group_id: StrictInt = Field(gt=0)
    process_start_marker: str
    registered_at: str
    process_binding_sha256: str = ""

    @model_validator(mode="after")
    def validate_process_binding(self) -> EveryCodeFeedbackProcessBindingRecord:
        for name in ("session_name", "process_start_marker"):
            _token(getattr(self, name), name)
        _timestamp(self.registered_at, "registered_at")
        expected = _canonical_digest(
            self.model_dump(mode="json", exclude={"process_binding_sha256"})
        )
        if self.process_binding_sha256 and self.process_binding_sha256 != expected:
            raise ValueError("process_binding_sha256 does not match the exact process binding")
        object.__setattr__(self, "process_binding_sha256", expected)
        return self


class EveryCodeFeedbackLaunchObservationRecord(_StrictFrozenModel):
    binding: EveryCodeFeedbackLaunchBinding
    evidence_status: Literal["exact_match", "proven_absent", "mismatch", "unavailable"]
    observed_process_binding: EveryCodeFeedbackProcessBindingRecord | None = None
    inspected_at: str
    evidence_sha256: str

    @model_validator(mode="after")
    def validate_observation(self) -> EveryCodeFeedbackLaunchObservationRecord:
        _timestamp(self.inspected_at, "inspected_at")
        _digest(self.evidence_sha256, "evidence_sha256")
        if self.evidence_status == "exact_match":
            if self.observed_process_binding is None:
                raise ValueError("exact-match observation requires a process binding")
            if self.observed_process_binding.binding != self.binding:
                raise ValueError("observed process binding does not match launch binding")
        elif self.observed_process_binding is not None:
            raise ValueError("non-matching observations cannot claim an exact process binding")
        return self


class EveryCodeFeedbackCancellationInstructionRecord(_StrictFrozenModel):
    operation_id: str
    process_binding: EveryCodeFeedbackProcessBindingRecord
    requested_at: str
    instruction_sha256: str = ""

    @model_validator(mode="after")
    def validate_instruction(self) -> EveryCodeFeedbackCancellationInstructionRecord:
        _token(self.operation_id, "operation_id")
        _timestamp(self.requested_at, "requested_at")
        expected = _canonical_digest(self.model_dump(mode="json", exclude={"instruction_sha256"}))
        if self.instruction_sha256 and self.instruction_sha256 != expected:
            raise ValueError("instruction_sha256 does not match cancellation instruction")
        object.__setattr__(self, "instruction_sha256", expected)
        return self


class EveryCodeFeedbackResumeOperationRecord(_StrictFrozenModel):
    schema_version: Literal[1] = 1
    operation_id: str
    intent_id: str
    acceptance_id: str
    binding: EveryCodeFeedbackLaunchBinding
    execution_policy: EveryCodeFeedbackPolicyDecisionProvenance
    state: Literal[
        "launch_pending",
        "registered",
        "released",
        "started",
        "handoff_accepted",
        "delivery_failed",
        "delivery_unknown",
        "reconcile_required",
        "cancelled",
        "cancellation_unknown",
    ]
    committed_at: str
    updated_at: str

    @model_validator(mode="after")
    def validate_operation(self) -> EveryCodeFeedbackResumeOperationRecord:
        for name in ("operation_id", "intent_id", "acceptance_id"):
            _token(getattr(self, name), name)
        if self.execution_policy.action != EVERY_CODE_FEEDBACK_RESUME_EXECUTE_ACTION:
            raise ValueError("resume operation requires execute-action policy provenance")
        _timestamp(self.committed_at, "committed_at")
        _timestamp(self.updated_at, "updated_at")
        if parse_every_code_feedback_timestamp(
            self.updated_at
        ) < parse_every_code_feedback_timestamp(self.committed_at):
            raise ValueError("operation updated_at cannot precede committed_at")
        return self


class _Receipt(_StrictFrozenModel):
    receipt_id: str
    operation_id: str
    binding: EveryCodeFeedbackLaunchBinding
    recorded_at: str
    evidence_sha256: str

    @model_validator(mode="after")
    def validate_receipt(self) -> _Receipt:
        _token(self.receipt_id, "receipt_id")
        _token(self.operation_id, "operation_id")
        _timestamp(self.recorded_at, "recorded_at")
        _digest(self.evidence_sha256, "evidence_sha256")
        return self


class EveryCodeFeedbackStartupReceiptRecord(_Receipt):
    receipt_kind: Literal["startup"] = "startup"
    process_binding_sha256: str

    @model_validator(mode="after")
    def validate_startup(self) -> EveryCodeFeedbackStartupReceiptRecord:
        _digest(self.process_binding_sha256, "process_binding_sha256")
        return self


class EveryCodeFeedbackHandoffReceiptRecord(_Receipt):
    receipt_kind: Literal["handoff"] = "handoff"
    handoff_sha256: str

    @model_validator(mode="after")
    def validate_handoff(self) -> EveryCodeFeedbackHandoffReceiptRecord:
        _digest(self.handoff_sha256, "handoff_sha256")
        return self


class EveryCodeLinkedPullRequestClosureRecord(_StrictFrozenModel):
    schema_version: Literal[1] = 1
    closure_id: str
    request_id: str
    repository_id: StrictInt = Field(gt=0)
    pull_request_number: StrictInt = Field(gt=0)
    pull_request_node_id: str
    merged: bool
    closed_at: str
    github_delivery_id: str
    closure_digest: str = ""

    @model_validator(mode="after")
    def validate_closure(self) -> EveryCodeLinkedPullRequestClosureRecord:
        for name in ("closure_id", "request_id", "github_delivery_id"):
            _token(getattr(self, name), name)
        _opaque_id(self.pull_request_node_id, "pull_request_node_id")
        _timestamp(self.closed_at, "closed_at")
        expected = _canonical_digest(self.model_dump(mode="json", exclude={"closure_digest"}))
        if self.closure_digest and self.closure_digest != expected:
            raise ValueError("closure_digest does not match closure evidence")
        object.__setattr__(self, "closure_digest", expected)
        return self


class EveryCodeFeedbackRecoveryDispositionRecord(_StrictFrozenModel):
    schema_version: Literal[1] = 1
    recovery_id: str
    operation_id: str
    binding: EveryCodeFeedbackLaunchBinding
    recovery_attempt: StrictInt = Field(gt=0)
    disposition: Literal["adopted", "observed_absent", "reconcile_required"]
    evidence_status: Literal["exact_match", "proven_absent", "mismatch", "unavailable"]
    evidence_sha256: str
    recorded_at: str
    reason_code: str

    @model_validator(mode="after")
    def validate_recovery(self) -> EveryCodeFeedbackRecoveryDispositionRecord:
        for name in ("recovery_id", "operation_id", "reason_code"):
            _token(getattr(self, name), name)
        _digest(self.evidence_sha256, "evidence_sha256")
        _timestamp(self.recorded_at, "recorded_at")
        if self.disposition == "adopted" and self.evidence_status != "exact_match":
            raise ValueError("adoption requires exact matching evidence")
        if self.disposition == "observed_absent" and self.evidence_status != "proven_absent":
            raise ValueError("absent disposition requires proven absence")
        if self.disposition == "reconcile_required" and self.evidence_status not in {
            "mismatch",
            "unavailable",
        }:
            raise ValueError("reconciliation requires mismatch or unavailable evidence")
        return self
