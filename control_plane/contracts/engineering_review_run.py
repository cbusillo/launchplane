from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


EngineeringReviewRunState = Literal[
    "pending", "dispatched", "running", "completed", "expired", "cancelled"
]
EngineeringReviewRunDecision = Literal["approved", "changes_requested", "blocked"]

ENGINEERING_REVIEW_RUN_CREDENTIAL_BYTES = 32
ENGINEERING_REVIEW_RUN_DEFAULT_LEASE_SECONDS = 3600
ENGINEERING_REVIEW_RUN_MAX_FINDINGS = 50
ENGINEERING_REVIEW_RUN_MAX_SUMMARY_LENGTH = 4000
ENGINEERING_REVIEW_RUN_MAX_FINDING_MESSAGE_LENGTH = 1000

NONTERMINAL_REVIEW_STATES: frozenset[str] = frozenset({"pending", "dispatched", "running"})
TERMINAL_REVIEW_STATES: frozenset[str] = frozenset({"completed", "expired", "cancelled"})


class EngineeringReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=ENGINEERING_REVIEW_RUN_MAX_FINDING_MESSAGE_LENGTH)
    path: str = ""
    line: int | None = None

    @model_validator(mode="after")
    def _validate_finding(self) -> "EngineeringReviewFinding":
        if not self.code.strip():
            raise ValueError("Engineering review finding requires code")
        if not self.message.strip():
            raise ValueError("Engineering review finding requires message")
        return self


class EngineeringReviewRunRecord(BaseModel):
    """Immutable dispatch record created by Launchplane before review process launch.

    Server owns: run_id, review_slot, all target/policy bindings, model selection,
    binary_digest, lease, credential, and evidence_digest after submission.
    Caller supplies only: decision, findings, summary on submit.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    run_id: str
    review_slot: int = Field(ge=1)
    state: EngineeringReviewRunState
    repository: str
    pr_number: int = Field(ge=1)
    head_sha: str
    tree_sha: str
    policy_revision: str
    work_request_id: str
    model_id: str
    model_family: str
    binary_digest: str
    created_at: str
    updated_at: str
    lease_expires_at: str
    run_credential_hash: str
    dispatched_at: str = ""
    dispatched_by_host: str = ""
    started_at: str = ""
    completed_at: str = ""
    decision: EngineeringReviewRunDecision | None = None
    findings: tuple[EngineeringReviewFinding, ...] = ()
    summary: str = ""
    evidence_digest: str = ""
    error_message: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "EngineeringReviewRunRecord":
        if not self.run_id.strip():
            raise ValueError("Engineering review run requires run_id")
        if not self.repository.strip() or "/" not in self.repository:
            raise ValueError("Engineering review run requires owner/repo repository")
        if not self.head_sha.strip():
            raise ValueError("Engineering review run requires head_sha")
        if not self.tree_sha.strip():
            raise ValueError("Engineering review run requires tree_sha")
        if not self.policy_revision.strip():
            raise ValueError("Engineering review run requires policy_revision")
        if not self.work_request_id.strip():
            raise ValueError("Engineering review run requires work_request_id")
        if not self.model_id.strip():
            raise ValueError("Engineering review run requires model_id")
        if not self.model_family.strip():
            raise ValueError("Engineering review run requires model_family")
        if not self.binary_digest.strip():
            raise ValueError("Engineering review run requires binary_digest")
        if not self.run_credential_hash.strip():
            raise ValueError("Engineering review run requires run_credential_hash")
        if not self.created_at.strip():
            raise ValueError("Engineering review run requires created_at")
        if not self.updated_at.strip():
            raise ValueError("Engineering review run requires updated_at")
        if not self.lease_expires_at.strip():
            raise ValueError("Engineering review run requires lease_expires_at")
        if self.state in {"dispatched", "running", "completed", "expired", "cancelled"}:
            if not self.dispatched_at.strip():
                raise ValueError(f"{self.state} engineering review run requires dispatched_at")
        if self.state in {"running", "completed"}:
            if not self.started_at.strip():
                raise ValueError(f"{self.state} engineering review run requires started_at")
        if self.state == "completed":
            if self.decision is None:
                raise ValueError("completed engineering review run requires decision")
            if not self.completed_at.strip():
                raise ValueError("completed engineering review run requires completed_at")
            if not self.evidence_digest.strip():
                raise ValueError("completed engineering review run requires evidence_digest")
        if self.state != "completed" and self.decision is not None:
            raise ValueError(f"{self.state} engineering review run must not have decision")
        if len(self.findings) > ENGINEERING_REVIEW_RUN_MAX_FINDINGS:
            raise ValueError(
                f"Engineering review run findings must not exceed {ENGINEERING_REVIEW_RUN_MAX_FINDINGS}"
            )
        if len(self.summary) > ENGINEERING_REVIEW_RUN_MAX_SUMMARY_LENGTH:
            raise ValueError(
                f"Engineering review run summary must not exceed {ENGINEERING_REVIEW_RUN_MAX_SUMMARY_LENGTH} characters"
            )
        return self


class EngineeringReviewRunSubmission(BaseModel):
    """Bounded submission accepted from the reviewer. No identity fields."""

    model_config = ConfigDict(extra="forbid")

    run_credential: str = Field(min_length=1)
    decision: EngineeringReviewRunDecision
    findings: tuple[EngineeringReviewFinding, ...] = ()
    summary: str = ""

    @model_validator(mode="after")
    def _validate_submission(self) -> "EngineeringReviewRunSubmission":
        if not self.run_credential.strip():
            raise ValueError("Engineering review submission requires run_credential")
        if len(self.findings) > ENGINEERING_REVIEW_RUN_MAX_FINDINGS:
            raise ValueError(
                f"Engineering review submission findings must not exceed {ENGINEERING_REVIEW_RUN_MAX_FINDINGS}"
            )
        if len(self.summary) > ENGINEERING_REVIEW_RUN_MAX_SUMMARY_LENGTH:
            raise ValueError(
                f"Engineering review submission summary must not exceed {ENGINEERING_REVIEW_RUN_MAX_SUMMARY_LENGTH} characters"
            )
        return self


def build_engineering_review_run_id(
    *,
    repository: str,
    pr_number: int,
    head_sha: str,
    review_slot: int,
    policy_revision: str,
) -> str:
    normalized_repo = repository.strip().lower()
    normalized_head = head_sha.strip().lower()
    normalized_policy = policy_revision.strip()
    if (
        not normalized_repo
        or pr_number < 1
        or not normalized_head
        or review_slot < 1
        or not normalized_policy
    ):
        raise ValueError(
            "Engineering review run id requires repository, pr_number, head_sha, review_slot, policy_revision"
        )
    digest = hashlib.sha256(
        json.dumps(
            {
                "repository": normalized_repo,
                "pr_number": pr_number,
                "head_sha": normalized_head,
                "review_slot": review_slot,
                "policy_revision": normalized_policy,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    repo_slug = normalized_repo.replace("/", "-")
    return f"eng-review-run-{repo_slug}-{pr_number}-s{review_slot}-{digest}"


def generate_engineering_review_run_credential() -> tuple[str, str]:
    """Return (plaintext_credential, credential_hash). Store only the hash."""
    plaintext = secrets.token_hex(ENGINEERING_REVIEW_RUN_CREDENTIAL_BYTES)
    credential_hash = _hash_credential(plaintext)
    return plaintext, credential_hash


def _hash_credential(plaintext: str) -> str:
    return hashlib.sha256(plaintext.strip().encode("utf-8")).hexdigest()


def verify_engineering_review_run_credential(plaintext: str, stored_hash: str) -> bool:
    return secrets.compare_digest(_hash_credential(plaintext.strip()), stored_hash.strip())


def build_engineering_review_evidence_digest(
    run: EngineeringReviewRunRecord,
    *,
    decision: EngineeringReviewRunDecision,
    findings: tuple[EngineeringReviewFinding, ...],
    summary: str,
) -> str:
    payload = {
        "run_id": run.run_id,
        "repository": run.repository,
        "pr_number": run.pr_number,
        "head_sha": run.head_sha,
        "tree_sha": run.tree_sha,
        "policy_revision": run.policy_revision,
        "work_request_id": run.work_request_id,
        "model_id": run.model_id,
        "model_family": run.model_family,
        "binary_digest": run.binary_digest,
        "review_slot": run.review_slot,
        "decision": decision,
        "findings": [f.model_dump(mode="json") for f in findings],
        "summary": summary,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def dispatch_engineering_review_run(
    record: EngineeringReviewRunRecord,
    *,
    dispatched_at: str,
    dispatched_by_host: str,
    lease_seconds: int = ENGINEERING_REVIEW_RUN_DEFAULT_LEASE_SECONDS,
) -> EngineeringReviewRunRecord:
    normalized_host = dispatched_by_host.strip()
    if not normalized_host:
        raise ValueError("Engineering review run dispatch requires dispatched_by_host")
    if not dispatched_at.strip():
        raise ValueError("Engineering review run dispatch requires dispatched_at")
    if record.state != "pending":
        raise ValueError(
            f"Engineering review run dispatch requires pending state, got {record.state!r}"
        )
    new_lease = _add_seconds_to_timestamp(dispatched_at, lease_seconds)
    return record.model_copy(
        update={
            "state": "dispatched",
            "dispatched_at": dispatched_at,
            "dispatched_by_host": normalized_host,
            "lease_expires_at": new_lease,
            "updated_at": dispatched_at,
        }
    )


def mark_engineering_review_run_running(
    record: EngineeringReviewRunRecord,
    *,
    started_at: str,
    run_credential: str,
) -> EngineeringReviewRunRecord:
    if not started_at.strip():
        raise ValueError("Engineering review run start requires started_at")
    if record.state != "dispatched":
        raise ValueError(
            f"Engineering review run start requires dispatched state, got {record.state!r}"
        )
    if not verify_engineering_review_run_credential(run_credential, record.run_credential_hash):
        raise ValueError("Engineering review run start: invalid credential")
    return record.model_copy(
        update={
            "state": "running",
            "started_at": started_at,
            "updated_at": started_at,
        }
    )


def submit_engineering_review_run(
    record: EngineeringReviewRunRecord,
    submission: EngineeringReviewRunSubmission,
    *,
    completed_at: str,
) -> EngineeringReviewRunRecord:
    if not completed_at.strip():
        raise ValueError("Engineering review run submission requires completed_at")
    if record.state not in {"dispatched", "running"}:
        raise ValueError(
            f"Engineering review run submission requires dispatched or running state, got {record.state!r}"
        )
    if not verify_engineering_review_run_credential(
        submission.run_credential, record.run_credential_hash
    ):
        raise ValueError("Engineering review run submission: invalid credential (fail closed)")
    evidence_digest = build_engineering_review_evidence_digest(
        record,
        decision=submission.decision,
        findings=submission.findings,
        summary=submission.summary,
    )
    return record.model_copy(
        update={
            "state": "completed",
            "started_at": record.started_at or completed_at,
            "completed_at": completed_at,
            "updated_at": completed_at,
            "decision": submission.decision,
            "findings": submission.findings,
            "summary": submission.summary,
            "evidence_digest": evidence_digest,
        }
    )


def expire_engineering_review_run(
    record: EngineeringReviewRunRecord,
    *,
    expired_at: str,
) -> EngineeringReviewRunRecord:
    if not expired_at.strip():
        raise ValueError("Engineering review run expiry requires expired_at")
    if record.state not in NONTERMINAL_REVIEW_STATES:
        raise ValueError(
            f"Engineering review run expiry requires non-terminal state, got {record.state!r}"
        )
    return record.model_copy(
        update={
            "state": "expired",
            "updated_at": expired_at,
            "error_message": f"Review run lease expired at {record.lease_expires_at}",
        }
    )


def cancel_engineering_review_run(
    record: EngineeringReviewRunRecord,
    *,
    cancelled_at: str,
    reason: str = "",
) -> EngineeringReviewRunRecord:
    if not cancelled_at.strip():
        raise ValueError("Engineering review run cancellation requires cancelled_at")
    if record.state in TERMINAL_REVIEW_STATES:
        raise ValueError(
            f"Engineering review run cancellation requires non-terminal state, got {record.state!r}"
        )
    return record.model_copy(
        update={
            "state": "cancelled",
            "updated_at": cancelled_at,
            "error_message": reason.strip() or "Review run cancelled.",
        }
    )


def _add_seconds_to_timestamp(timestamp: str, seconds: int) -> str:
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return timestamp
    result = dt.astimezone(UTC) + timedelta(seconds=seconds)
    return result.replace(microsecond=0).isoformat().replace("+00:00", "Z")
