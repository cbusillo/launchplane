"""Evidence exchanged with a system-owned macOS release host."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.contracts.release_review import ReleaseVersion


class ReleaseEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MediaforceReleaseRequest(ReleaseEvidence):
    product: str = Field(min_length=1)
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")


class MediaforceRuntime(ReleaseEvidence):
    version: ReleaseVersion
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MediaforceReleasePlan(ReleaseEvidence):
    request: MediaforceReleaseRequest
    repository: str = Field(min_length=1)
    context: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    database_id: str = Field(min_length=1)
    source_provenance_record_id: str = Field(min_length=1)
    recovery_deployment_record_id: str = Field(min_length=1)
    candidate: MediaforceRuntime
    previous: MediaforceRuntime
    # Runtime-only recovery must never require an automatic database restore.
    database_compatibility: Literal["backward-compatible"]

    @model_validator(mode="after")
    def validate_candidate(self) -> "MediaforceReleasePlan":
        if self.request.source_commit != self.candidate.version.source_commit:
            raise ValueError("The candidate must be the requested main commit.")
        if self.candidate.version == self.previous.version:
            raise ValueError("The candidate must differ from the pinned prior runtime.")
        return self


class MediaforceHostReceipt(ReleaseEvidence):
    operation_key: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    fence_token: str = Field(min_length=1)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Host evidence timestamps require a timezone.")
        return value


class MediaforceQuiescence(MediaforceHostReceipt):
    # Counts deliberately retain the source endpoint's names, but are not summed.
    pending_encode: int = Field(ge=0, strict=True)
    active_encode: int = Field(ge=0, strict=True)
    active_calibration: int = Field(ge=0, strict=True)
    unfinished_scan_rows: int = Field(ge=0, strict=True)
    active_evidence: int = Field(ge=0, strict=True)
    scan_jobs: int = Field(ge=0, strict=True)
    pending_publish: int = Field(ge=0, strict=True)
    publishing_locks: int = Field(ge=0, strict=True)
    remote_workers: int = Field(ge=0, strict=True)
    admissions_closed: bool = Field(strict=True)
    producers_quiesced: bool = Field(strict=True)

    @property
    def idle(self) -> bool:
        return (
            self.admissions_closed
            and self.producers_quiesced
            and not any(
                (
                    self.pending_encode,
                    self.active_encode,
                    self.active_calibration,
                    self.unfinished_scan_rows,
                    self.active_evidence,
                    self.scan_jobs,
                    self.pending_publish,
                    self.publishing_locks,
                    self.remote_workers,
                )
            )
        )


class MediaforceBackup(MediaforceHostReceipt):
    database_id: str = Field(min_length=1)
    runtime: MediaforceRuntime
    backup_record_id: str = Field(min_length=1)
    backup_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verified: bool = Field(strict=True)


class MediaforceRuntimeCheck(MediaforceHostReceipt):
    runtime: MediaforceRuntime
    healthy: bool = Field(strict=True)
    post_checks_passed: bool = Field(strict=True)


class MediaforceReleaseResult(ReleaseEvidence):
    status: Literal["passed", "blocked", "failed_recovered"]
    plan: MediaforceReleasePlan
    decision_record_id: str = ""
    backup: MediaforceBackup | None = None
    runtime_check: MediaforceRuntimeCheck | None = None
    recovery_check: MediaforceRuntimeCheck | None = None
    quiescence: MediaforceQuiescence | None = None
    reason: Literal[
        "", "work_not_idle", "acceptance_changed", "backup_unverified", "post_checks_failed"
    ] = ""

    @model_validator(mode="after")
    def validate_completion(self) -> "MediaforceReleaseResult":
        if self.status == "blocked":
            if not self.reason:
                raise ValueError("A blocked release requires a reason.")
            return self
        backup, check, idle = self.backup, self.runtime_check, self.quiescence
        if (
            not self.decision_record_id
            or backup is None
            or not backup.verified
            or backup.database_id != self.plan.database_id
            or backup.runtime != self.plan.previous
            or check is None
            or check.runtime != self.plan.candidate
            or idle is None
            or not idle.idle
        ):
            raise ValueError(
                "Terminal releases require bound acceptance, backup and work evidence."
            )
        receipts = [backup, check, idle]
        if self.status == "passed":
            if not check.healthy or not check.post_checks_passed or self.reason:
                raise ValueError("Passing releases require passing post-checks.")
        else:
            recovery = self.recovery_check
            if (
                recovery is None
                or recovery.runtime != self.plan.previous
                or not recovery.healthy
                or not recovery.post_checks_passed
                or (check.healthy and check.post_checks_passed)
                or self.reason != "post_checks_failed"
            ):
                raise ValueError(
                    "Recovered releases require a failed candidate and passing recovery."
                )
            receipts.append(recovery)
        if (
            any(
                receipt.target_id != self.plan.target_id
                or receipt.operation_key != check.operation_key
                or receipt.fence_token != check.fence_token
                for receipt in receipts
            )
            or not backup.observed_at <= idle.observed_at <= check.observed_at
        ):
            raise ValueError("Release evidence must belong to one fence in execution order.")
        if self.recovery_check is not None and self.recovery_check.observed_at < check.observed_at:
            raise ValueError("Recovery must follow the failed candidate check.")
        return self


class MediaforceCompletionReceipt(MediaforceHostReceipt):
    result: MediaforceReleaseResult
    admissions_resumed: Literal[True]

    @model_validator(mode="after")
    def validate_result(self) -> "MediaforceCompletionReceipt":
        if self.result.plan.target_id != self.target_id:
            raise ValueError("Completion must identify the released target.")
        for evidence in (
            self.result.backup,
            self.result.runtime_check,
            self.result.recovery_check,
            self.result.quiescence,
        ):
            if evidence is not None and (
                evidence.operation_key != self.operation_key
                or evidence.fence_token != self.fence_token
                or evidence.observed_at > self.observed_at
            ):
                raise ValueError("Completion must follow this operation's evidence.")
        return self
