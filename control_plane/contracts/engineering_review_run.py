from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


EngineeringReviewRunState = Literal[
    "pending", "claimed", "running", "completed", "failed", "expired", "cancelled"
]
EngineeringReviewRunDecision = Literal["approved", "changes_requested", "blocked"]
EngineeringReviewAuthorityStatus = Literal["active", "retired"]

ENGINEERING_REVIEW_AUTHORITY_READ_ACTION = "engineering_review_authority.read"
ENGINEERING_REVIEW_AUTHORITY_WRITE_ACTION = "engineering_review_authority.write"
ENGINEERING_REVIEW_RUN_CREATE_ACTION = "engineering_review_run.create"
ENGINEERING_REVIEW_RUN_READ_ACTION = "engineering_review_run.read"

ENGINEERING_REVIEW_RUN_CREDENTIAL_BYTES = 32
ENGINEERING_REVIEW_RUN_MAX_FINDINGS = 50
ENGINEERING_REVIEW_RUN_MAX_SUMMARY_LENGTH = 4000
ENGINEERING_REVIEW_RUN_MAX_FINDING_MESSAGE_LENGTH = 1000
ENGINEERING_REVIEW_RUN_MIN_LEASE_SECONDS = 300
ENGINEERING_REVIEW_RUN_MAX_LEASE_SECONDS = 7200

NONTERMINAL_REVIEW_STATES: frozenset[str] = frozenset({"pending", "claimed", "running"})
TERMINAL_REVIEW_STATES: frozenset[str] = frozenset({"completed", "failed", "expired", "cancelled"})

_GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_BINARY_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
_MODEL_FAMILY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class EngineeringReviewConflictError(ValueError):
    pass


class EngineeringReviewSequenceError(ValueError):
    pass


class EngineeringReviewUnavailableError(RuntimeError):
    pass


class EngineeringReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=ENGINEERING_REVIEW_RUN_MAX_FINDING_MESSAGE_LENGTH)
    path: str = Field(default="", max_length=500)
    line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_finding(self) -> EngineeringReviewFinding:
        if self.code.strip() != self.code or not self.code.strip():
            raise ValueError("Engineering review finding requires canonical code")
        if self.message.strip() != self.message or not self.message.strip():
            raise ValueError("Engineering review finding requires canonical message")
        if self.path.strip() != self.path:
            raise ValueError("Engineering review finding path must be canonical")
        return self


class EngineeringReviewModelSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_slot: int = Field(ge=1, le=4)
    model_id: str
    model_family: str

    @model_validator(mode="after")
    def _validate_slot(self) -> EngineeringReviewModelSlot:
        if not _MODEL_ID_PATTERN.fullmatch(self.model_id):
            raise ValueError("Engineering review model_id is not canonical")
        if not _MODEL_FAMILY_PATTERN.fullmatch(self.model_family):
            raise ValueError("Engineering review model_family is not canonical")
        return self


class EngineeringReviewAuthorityRecord(BaseModel):
    """DB-backed shadow review policy and controlled worker runtime identity."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    authority_id: str = ""
    repository: str
    status: EngineeringReviewAuthorityStatus
    policy_revision: int = Field(ge=1)
    model_slots: tuple[EngineeringReviewModelSlot, ...] = Field(min_length=2, max_length=4)
    worker_runtime_id: str
    worker_host: str
    executable_path: str
    binary_sha256: str
    lease_seconds: int = Field(
        ge=ENGINEERING_REVIEW_RUN_MIN_LEASE_SECONDS,
        le=ENGINEERING_REVIEW_RUN_MAX_LEASE_SECONDS,
    )
    rollout_mode: Literal["shadow"] = "shadow"
    authoritative: Literal[False] = False
    enforcement_effect: Literal["none"] = "none"
    recorded_at: str
    source: str
    reason: str
    supersedes_authority_id: str | None = None
    authority_digest: str = ""

    @model_validator(mode="after")
    def _validate_authority(self) -> EngineeringReviewAuthorityRecord:
        _validate_repository(self.repository)
        _validate_identifier(self.worker_runtime_id, "worker_runtime_id")
        if self.worker_host.strip() != self.worker_host or not self.worker_host:
            raise ValueError("Engineering review worker_host must be canonical")
        executable = Path(self.executable_path)
        if self.executable_path.strip() != self.executable_path or not executable.is_absolute():
            raise ValueError("Engineering review executable_path must be a canonical absolute path")
        if not _BINARY_SHA256_PATTERN.fullmatch(self.binary_sha256):
            raise ValueError("Engineering review binary_sha256 must be 64 lowercase hex characters")
        _parse_timestamp(self.recorded_at, "recorded_at")
        if self.source.strip() != self.source or not self.source:
            raise ValueError("Engineering review authority requires canonical source")
        if self.reason.strip() != self.reason or not self.reason:
            raise ValueError("Engineering review authority requires canonical reason")
        slots = tuple(slot.review_slot for slot in self.model_slots)
        if slots != tuple(range(1, len(slots) + 1)):
            raise ValueError("Engineering review model slots must be ordered and contiguous from 1")
        families = tuple(slot.model_family for slot in self.model_slots)
        if len(set(families)) != len(families):
            raise ValueError("Engineering review model slots require distinct model families")
        if self.policy_revision == 1 and self.supersedes_authority_id is not None:
            raise ValueError("Initial engineering review authority cannot supersede another record")
        if self.policy_revision > 1 and not self.supersedes_authority_id:
            raise ValueError(
                "Successor engineering review authority requires supersedes_authority_id"
            )
        if self.supersedes_authority_id is not None:
            _validate_identifier(self.supersedes_authority_id, "supersedes_authority_id")
        computed_authority_id = build_engineering_review_authority_id(
            repository=self.repository,
            policy_revision=self.policy_revision,
        )
        if self.authority_id:
            _validate_identifier(self.authority_id, "authority_id")
            if self.authority_id != computed_authority_id:
                raise ValueError(
                    "Engineering review authority_id does not match repository and revision"
                )
        else:
            self.authority_id = computed_authority_id
        computed_digest = build_engineering_review_authority_digest(self)
        if self.authority_digest:
            _validate_sha256(self.authority_digest, "authority_digest")
            if self.authority_digest != computed_digest:
                raise ValueError("Engineering review authority_digest does not match payload")
        else:
            self.authority_digest = computed_digest
        return self


class EngineeringReviewPullRequestTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    pr_number: int = Field(ge=1)
    pr_url: str
    head_sha: str
    tree_sha: str

    @model_validator(mode="after")
    def _validate_target(self) -> EngineeringReviewPullRequestTarget:
        _validate_repository(self.repository)
        if self.pr_url.strip() != self.pr_url or not self.pr_url:
            raise ValueError("Engineering review target requires canonical pr_url")
        _validate_git_sha(self.head_sha, "head_sha")
        _validate_git_sha(self.tree_sha, "tree_sha")
        return self


class EngineeringReviewRunRecord(BaseModel):
    """Internal persisted review assignment. Credential material is never an API view."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=2, ge=2)
    run_id: str
    assignment_fingerprint: str
    review_slot: int = Field(ge=1, le=4)
    state: EngineeringReviewRunState
    repository: str
    pr_number: int = Field(ge=1)
    pr_url: str
    head_sha: str
    tree_sha: str
    authority_id: str
    authority_digest: str
    policy_revision: int = Field(ge=1)
    work_request_id: str
    work_request_lifecycle_id: str
    model_id: str
    model_family: str
    worker_runtime_id: str
    worker_host: str
    executable_path: str
    binary_sha256: str
    lease_seconds: int = Field(
        ge=ENGINEERING_REVIEW_RUN_MIN_LEASE_SECONDS,
        le=ENGINEERING_REVIEW_RUN_MAX_LEASE_SECONDS,
    )
    rollout_mode: Literal["shadow"] = "shadow"
    authoritative: Literal[False] = False
    enforcement_effect: Literal["none"] = "none"
    created_at: str
    updated_at: str
    claimed_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    lease_expires_at: str = ""
    claim_attempt: int = Field(default=0, ge=0)
    fencing_token: int = Field(default=0, ge=0)
    credential_hash: str
    credential_ciphertext: str
    credential_key_id: str
    decision: EngineeringReviewRunDecision | None = None
    findings: tuple[EngineeringReviewFinding, ...] = ()
    summary: str = ""
    evidence_digest: str = ""
    submission_fingerprint: str = ""
    error_message: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> EngineeringReviewRunRecord:
        _validate_identifier(self.run_id, "run_id")
        _validate_sha256(self.assignment_fingerprint, "assignment_fingerprint")
        _validate_repository(self.repository)
        if self.pr_url.strip() != self.pr_url or not self.pr_url:
            raise ValueError("Engineering review run requires canonical pr_url")
        _validate_git_sha(self.head_sha, "head_sha")
        _validate_git_sha(self.tree_sha, "tree_sha")
        _validate_identifier(self.authority_id, "authority_id")
        _validate_sha256(self.authority_digest, "authority_digest")
        _validate_identifier(self.work_request_id, "work_request_id")
        _validate_identifier(self.work_request_lifecycle_id, "work_request_lifecycle_id")
        EngineeringReviewModelSlot(
            review_slot=self.review_slot,
            model_id=self.model_id,
            model_family=self.model_family,
        )
        _validate_identifier(self.worker_runtime_id, "worker_runtime_id")
        if self.worker_host.strip() != self.worker_host or not self.worker_host:
            raise ValueError("Engineering review run requires canonical worker_host")
        if (
            self.executable_path.strip() != self.executable_path
            or not Path(self.executable_path).is_absolute()
        ):
            raise ValueError("Engineering review run requires canonical absolute executable_path")
        if not _BINARY_SHA256_PATTERN.fullmatch(self.binary_sha256):
            raise ValueError("Engineering review run requires canonical binary_sha256")
        _parse_timestamp(self.created_at, "created_at")
        _parse_timestamp(self.updated_at, "updated_at")
        if not self.credential_hash or not _BINARY_SHA256_PATTERN.fullmatch(self.credential_hash):
            raise ValueError("Engineering review run requires credential_hash")
        if not self.credential_ciphertext.strip() or not self.credential_key_id.strip():
            raise ValueError("Engineering review run requires encrypted credential material")
        if self.state in {"claimed", "running", "completed", "failed", "expired"}:
            _parse_timestamp(self.claimed_at, "claimed_at")
            _parse_timestamp(self.lease_expires_at, "lease_expires_at")
            if self.claim_attempt < 1:
                raise ValueError(f"{self.state} engineering review run requires claim_attempt")
            if self.fencing_token < 1:
                raise ValueError(f"{self.state} engineering review run requires fencing_token")
        elif self.claim_attempt or self.fencing_token:
            raise ValueError("Pending engineering review run must not include claim fencing")
        if self.state in {"running", "completed", "failed"}:
            _parse_timestamp(self.started_at, "started_at")
        if self.state in TERMINAL_REVIEW_STATES:
            _parse_timestamp(self.finished_at, "finished_at")
        if self.state == "completed":
            if self.decision is None:
                raise ValueError("completed engineering review run requires decision")
            _validate_sha256(self.evidence_digest, "evidence_digest")
            _validate_sha256(self.submission_fingerprint, "submission_fingerprint")
        elif self.decision is not None or self.evidence_digest or self.submission_fingerprint:
            raise ValueError(
                f"{self.state} engineering review run must not include decision evidence"
            )
        if self.state == "failed" and not self.error_message.strip():
            raise ValueError("failed engineering review run requires error_message")
        if len(self.findings) > ENGINEERING_REVIEW_RUN_MAX_FINDINGS:
            raise ValueError(
                f"Engineering review run findings must not exceed {ENGINEERING_REVIEW_RUN_MAX_FINDINGS}"
            )
        if len(self.summary) > ENGINEERING_REVIEW_RUN_MAX_SUMMARY_LENGTH:
            raise ValueError(
                "Engineering review run summary must not exceed "
                f"{ENGINEERING_REVIEW_RUN_MAX_SUMMARY_LENGTH} characters"
            )
        if self.run_id != build_engineering_review_run_id(
            assignment_fingerprint=self.assignment_fingerprint
        ):
            raise ValueError("Engineering review run_id does not match assignment_fingerprint")
        return self


class EngineeringReviewRunView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    run_id: str
    review_slot: int
    state: EngineeringReviewRunState
    repository: str
    pr_number: int
    pr_url: str
    head_sha: str
    tree_sha: str
    authority_id: str
    authority_digest: str
    policy_revision: int
    work_request_id: str
    work_request_lifecycle_id: str
    model_id: str
    model_family: str
    worker_runtime_id: str
    binary_sha256: str
    rollout_mode: Literal["shadow"]
    authoritative: Literal[False]
    enforcement_effect: Literal["none"]
    created_at: str
    updated_at: str
    claimed_at: str
    started_at: str
    finished_at: str
    lease_expires_at: str
    claim_attempt: int
    fencing_token: int
    decision: EngineeringReviewRunDecision | None
    findings: tuple[EngineeringReviewFinding, ...]
    summary: str
    evidence_digest: str
    error_message: str


class EngineeringReviewRunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_request_id: str

    @model_validator(mode="after")
    def _validate_request(self) -> EngineeringReviewRunCreateRequest:
        _validate_identifier(self.work_request_id, "work_request_id")
        return self


class EngineeringReviewAuthorityApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["dry_run", "apply"] = "apply"
    expected_current_authority_id: str = ""
    expected_current_authority_digest: str = ""
    record: EngineeringReviewAuthorityRecord


class EngineeringReviewRunClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    worker_runtime_id: str
    worker_host: str

    @model_validator(mode="after")
    def _validate_claim(self) -> EngineeringReviewRunClaimRequest:
        _validate_identifier(self.run_id, "run_id")
        _validate_identifier(self.worker_runtime_id, "worker_runtime_id")
        if self.worker_host.strip() != self.worker_host or not self.worker_host:
            raise ValueError("Engineering review claim requires canonical worker_host")
        return self


class EngineeringReviewRunWorkerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    worker_runtime_id: str
    worker_host: str
    fencing_token: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_update(self) -> EngineeringReviewRunWorkerUpdate:
        _validate_identifier(self.run_id, "run_id")
        _validate_identifier(self.worker_runtime_id, "worker_runtime_id")
        if self.worker_host.strip() != self.worker_host or not self.worker_host:
            raise ValueError("Engineering review worker update requires canonical worker_host")
        return self


class EngineeringReviewRunWorkerFailure(EngineeringReviewRunWorkerUpdate):
    error_code: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _validate_failure_update(self) -> EngineeringReviewRunWorkerFailure:
        if self.error_code.strip() != self.error_code:
            raise ValueError("Engineering review worker failure requires canonical error_code")
        if self.summary.strip() != self.summary:
            raise ValueError("Engineering review worker failure requires canonical summary")
        return self


class EngineeringReviewRunSubmission(BaseModel):
    """Credential plus bounded reviewer output; no target or identity authority."""

    model_config = ConfigDict(extra="forbid")

    credential: str = Field(min_length=1, max_length=256)
    decision: EngineeringReviewRunDecision
    findings: tuple[EngineeringReviewFinding, ...] = Field(default=(), max_length=50)
    summary: str = Field(default="", max_length=ENGINEERING_REVIEW_RUN_MAX_SUMMARY_LENGTH)

    @model_validator(mode="after")
    def _validate_submission(self) -> EngineeringReviewRunSubmission:
        if self.credential.strip() != self.credential or not self.credential:
            raise ValueError("Engineering review submission requires canonical credential")
        return self


class EngineeringReviewRunFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error_code: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _validate_failure(self) -> EngineeringReviewRunFailure:
        if self.error_code.strip() != self.error_code or not self.error_code:
            raise ValueError("Engineering review failure requires canonical error_code")
        if self.summary.strip() != self.summary or not self.summary:
            raise ValueError("Engineering review failure requires canonical summary")
        return self


class EngineeringReviewRunAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run: EngineeringReviewRunView
    credential: str
    worker_host: str
    executable_path: str


def build_engineering_review_authority_id(*, repository: str, policy_revision: int) -> str:
    _validate_repository(repository)
    if policy_revision < 1:
        raise ValueError("Engineering review authority policy_revision must be positive")
    repository_slug = repository.replace("/", "-").lower()
    return f"engineering-review-authority:{repository_slug}:{policy_revision}"


def build_engineering_review_authority_digest(
    authority: EngineeringReviewAuthorityRecord,
) -> str:
    return _canonical_digest(
        authority.model_dump(
            mode="json",
            exclude={"status", "authority_digest"},
        )
    )


def engineering_review_run_view(record: EngineeringReviewRunRecord) -> EngineeringReviewRunView:
    return EngineeringReviewRunView.model_validate(
        record.model_dump(
            mode="json",
            exclude={
                "assignment_fingerprint",
                "worker_host",
                "executable_path",
                "lease_seconds",
                "credential_hash",
                "credential_ciphertext",
                "credential_key_id",
                "submission_fingerprint",
            },
        )
    )


def build_engineering_review_assignment_fingerprint(
    *,
    target: EngineeringReviewPullRequestTarget,
    authority: EngineeringReviewAuthorityRecord,
    work_request_id: str,
    work_request_lifecycle_id: str,
    model_slot: EngineeringReviewModelSlot,
) -> str:
    payload = {
        "target": target.model_dump(mode="json"),
        "authority_id": authority.authority_id,
        "authority_digest": authority.authority_digest,
        "policy_revision": authority.policy_revision,
        "work_request_id": work_request_id,
        "work_request_lifecycle_id": work_request_lifecycle_id,
        "model_slot": model_slot.model_dump(mode="json"),
        "worker_runtime_id": authority.worker_runtime_id,
        "worker_host": authority.worker_host,
        "executable_path": authority.executable_path,
        "binary_sha256": authority.binary_sha256,
        "lease_seconds": authority.lease_seconds,
        "rollout_mode": authority.rollout_mode,
        "authoritative": authority.authoritative,
        "enforcement_effect": authority.enforcement_effect,
    }
    return _canonical_digest(payload)


def build_engineering_review_run_id(*, assignment_fingerprint: str) -> str:
    _validate_sha256(assignment_fingerprint, "assignment_fingerprint")
    return f"engineering-review-run:{assignment_fingerprint}"


def generate_engineering_review_run_credential() -> tuple[str, str]:
    plaintext = secrets.token_urlsafe(ENGINEERING_REVIEW_RUN_CREDENTIAL_BYTES)
    return plaintext, _hash_credential(plaintext)


def verify_engineering_review_run_credential(plaintext: str, stored_hash: str) -> bool:
    if not plaintext or not stored_hash:
        return False
    return secrets.compare_digest(_hash_credential(plaintext), stored_hash)


def claim_engineering_review_run(
    record: EngineeringReviewRunRecord,
    *,
    claimed_at: str,
) -> EngineeringReviewRunRecord:
    if record.state != "pending":
        raise ValueError(f"Engineering review claim requires pending state, got {record.state!r}")
    _parse_timestamp(claimed_at, "claimed_at")
    return record.model_copy(
        update={
            "state": "claimed",
            "claimed_at": claimed_at,
            "updated_at": claimed_at,
            "lease_expires_at": _add_seconds_to_timestamp(claimed_at, record.lease_seconds),
            "claim_attempt": record.claim_attempt + 1,
            "fencing_token": record.fencing_token + 1,
        }
    )


def start_engineering_review_run(
    record: EngineeringReviewRunRecord,
    *,
    started_at: str,
) -> EngineeringReviewRunRecord:
    _parse_timestamp(started_at, "started_at")
    if record.state == "running":
        return record
    if record.state != "claimed":
        raise ValueError(f"Engineering review start requires claimed state, got {record.state!r}")
    if _timestamp_after(started_at, record.lease_expires_at):
        raise ValueError("Engineering review start rejected because the lease expired")
    return record.model_copy(
        update={"state": "running", "started_at": started_at, "updated_at": started_at}
    )


def submit_engineering_review_run(
    record: EngineeringReviewRunRecord,
    submission: EngineeringReviewRunSubmission,
    *,
    completed_at: str,
) -> EngineeringReviewRunRecord:
    _parse_timestamp(completed_at, "completed_at")
    if not verify_engineering_review_run_credential(submission.credential, record.credential_hash):
        raise ValueError("Engineering review submission rejected")
    submission_fingerprint = build_engineering_review_submission_fingerprint(submission)
    if record.state == "completed":
        if record.submission_fingerprint == submission_fingerprint:
            return record
        raise ValueError("Engineering review submission conflicts with completed evidence")
    if record.state != "running":
        raise ValueError(
            f"Engineering review submission requires running state, got {record.state!r}"
        )
    if _timestamp_after(completed_at, record.lease_expires_at):
        raise ValueError("Engineering review submission rejected because the lease expired")
    evidence_digest = build_engineering_review_evidence_digest(
        record,
        decision=submission.decision,
        findings=submission.findings,
        summary=submission.summary,
    )
    return record.model_copy(
        update={
            "state": "completed",
            "finished_at": completed_at,
            "updated_at": completed_at,
            "decision": submission.decision,
            "findings": submission.findings,
            "summary": submission.summary,
            "evidence_digest": evidence_digest,
            "submission_fingerprint": submission_fingerprint,
        }
    )


def fail_engineering_review_run(
    record: EngineeringReviewRunRecord,
    failure: EngineeringReviewRunFailure,
    *,
    failed_at: str,
) -> EngineeringReviewRunRecord:
    _parse_timestamp(failed_at, "failed_at")
    if record.state == "failed":
        return record
    if record.state not in {"claimed", "running"}:
        raise ValueError(f"Engineering review failure requires active state, got {record.state!r}")
    return record.model_copy(
        update={
            "state": "failed",
            "started_at": record.started_at or failed_at,
            "finished_at": failed_at,
            "updated_at": failed_at,
            "error_message": f"{failure.error_code}: {failure.summary}",
        }
    )


def expire_engineering_review_run(
    record: EngineeringReviewRunRecord,
    *,
    expired_at: str,
) -> EngineeringReviewRunRecord:
    _parse_timestamp(expired_at, "expired_at")
    if record.state not in {"claimed", "running"}:
        raise ValueError(
            f"Engineering review expiry requires claimed or running state, got {record.state!r}"
        )
    return record.model_copy(
        update={
            "state": "expired",
            "started_at": record.started_at,
            "finished_at": expired_at,
            "updated_at": expired_at,
            "error_message": f"Review run lease expired at {record.lease_expires_at}",
        }
    )


def build_engineering_review_submission_fingerprint(
    submission: EngineeringReviewRunSubmission,
) -> str:
    return _canonical_digest(
        {
            "decision": submission.decision,
            "findings": [finding.model_dump(mode="json") for finding in submission.findings],
            "summary": submission.summary,
        }
    )


def build_engineering_review_evidence_digest(
    run: EngineeringReviewRunRecord,
    *,
    decision: EngineeringReviewRunDecision,
    findings: tuple[EngineeringReviewFinding, ...],
    summary: str,
) -> str:
    return _canonical_digest(
        {
            "run_id": run.run_id,
            "assignment_fingerprint": run.assignment_fingerprint,
            "repository": run.repository,
            "pr_number": run.pr_number,
            "head_sha": run.head_sha,
            "tree_sha": run.tree_sha,
            "authority_id": run.authority_id,
            "authority_digest": run.authority_digest,
            "policy_revision": run.policy_revision,
            "work_request_id": run.work_request_id,
            "work_request_lifecycle_id": run.work_request_lifecycle_id,
            "model_id": run.model_id,
            "model_family": run.model_family,
            "worker_runtime_id": run.worker_runtime_id,
            "binary_sha256": run.binary_sha256,
            "review_slot": run.review_slot,
            "fencing_token": run.fencing_token,
            "rollout_mode": run.rollout_mode,
            "authoritative": run.authoritative,
            "enforcement_effect": run.enforcement_effect,
            "decision": decision,
            "findings": [finding.model_dump(mode="json") for finding in findings],
            "summary": summary,
        }
    )


def _hash_credential(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _canonical_digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _validate_identifier(value: str, field_name: str) -> None:
    if not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"Engineering review {field_name} is not canonical")


def _validate_repository(repository: str) -> None:
    owner, separator, name = repository.partition("/")
    if repository.strip() != repository or not owner or not separator or not name or "/" in name:
        raise ValueError("Engineering review requires canonical owner/repo repository")


def _validate_git_sha(value: str, field_name: str) -> None:
    if not _GIT_SHA_PATTERN.fullmatch(value):
        raise ValueError(f"Engineering review {field_name} must be 40 lowercase hex characters")


def _validate_sha256(value: str, field_name: str) -> None:
    if not _BINARY_SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"Engineering review {field_name} must be 64 lowercase hex characters")


def _parse_timestamp(value: str, field_name: str) -> datetime:
    if not value or value.strip() != value:
        raise ValueError(f"Engineering review {field_name} must be canonical")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            f"Engineering review {field_name} must be an ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None:
        raise ValueError(f"Engineering review {field_name} must include timezone")
    return parsed.astimezone(UTC)


def _timestamp_after(left: str, right: str) -> bool:
    return _parse_timestamp(left, "timestamp") > _parse_timestamp(right, "lease_expires_at")


def _add_seconds_to_timestamp(timestamp: str, seconds: int) -> str:
    result = _parse_timestamp(timestamp, "timestamp") + timedelta(seconds=seconds)
    return result.replace(microsecond=0).isoformat().replace("+00:00", "Z")
