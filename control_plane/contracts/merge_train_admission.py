from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.merge_train_run_record import MergeTrainRunRecord


MergeTrainAdmissionStatus = Literal["admitted", "deferred"]
MergeTrainAdmissionReason = Literal[
    "no_prior_run",
    "dry_run_history_only",
    "reread_required",
    "poll_interval_elapsed",
    "poll_interval_pending",
    "backoff_elapsed",
    "backoff_pending",
]


class MergeTrainAdmissionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository: str
    base_branch: str
    status: MergeTrainAdmissionStatus
    reason_code: MergeTrainAdmissionReason
    requested_at: str
    next_allowed_at: str
    latest_run_id: str = ""
    latest_run_status: str = ""
    latest_run_recorded_at: str = ""
    detail: str

    @model_validator(mode="after")
    def _validate_decision(self) -> "MergeTrainAdmissionDecision":
        self.repository = _normalize_required_value(
            self.repository, "merge train admission decision requires repository"
        )
        self.base_branch = _normalize_required_value(
            self.base_branch, "merge train admission decision requires base_branch"
        )
        self.requested_at = _normalize_required_value(
            self.requested_at, "merge train admission decision requires requested_at"
        )
        self.next_allowed_at = _normalize_required_value(
            self.next_allowed_at, "merge train admission decision requires next_allowed_at"
        )
        self.latest_run_id = self.latest_run_id.strip()
        self.latest_run_status = self.latest_run_status.strip()
        self.latest_run_recorded_at = self.latest_run_recorded_at.strip()
        self.detail = _normalize_required_value(
            self.detail, "merge train admission decision requires detail"
        )
        return self

    @property
    def admitted(self) -> bool:
        return self.status == "admitted"


def evaluate_merge_train_admission(
    *,
    repository: str,
    base_branch: str,
    requested_at: str,
    latest_run: MergeTrainRunRecord | None,
    poll_interval_seconds: int = 60,
    backoff_seconds: int = 300,
) -> MergeTrainAdmissionDecision:
    normalized_repository = _normalize_required_value(
        repository, "merge train admission requires repository"
    )
    normalized_base_branch = _normalize_required_value(
        base_branch, "merge train admission requires base_branch"
    )
    requested_time = _parse_timestamp(requested_at)
    normalized_requested_at = _format_timestamp(requested_time)
    if poll_interval_seconds < 0:
        raise ValueError("merge train poll interval cannot be negative")
    if backoff_seconds < 0:
        raise ValueError("merge train backoff interval cannot be negative")

    if latest_run is None:
        return _decision(
            repository=normalized_repository,
            base_branch=normalized_base_branch,
            status="admitted",
            reason_code="no_prior_run",
            requested_at=normalized_requested_at,
            next_allowed_at=normalized_requested_at,
            detail="No prior merge-train run exists for this repository/base branch.",
        )

    _validate_latest_run_scope(
        latest_run=latest_run,
        repository=normalized_repository,
        base_branch=normalized_base_branch,
    )
    if latest_run.mode == "dry_run":
        return _decision(
            repository=normalized_repository,
            base_branch=normalized_base_branch,
            status="admitted",
            reason_code="dry_run_history_only",
            requested_at=normalized_requested_at,
            next_allowed_at=normalized_requested_at,
            latest_run=latest_run,
            detail="Latest merge-train record is dry-run evidence and does not throttle the scheduler.",
        )

    if latest_run.reread_required:
        return _decision(
            repository=normalized_repository,
            base_branch=normalized_base_branch,
            status="admitted",
            reason_code="reread_required",
            requested_at=normalized_requested_at,
            next_allowed_at=normalized_requested_at,
            latest_run=latest_run,
            detail="Latest merge-train mutation requires a fresh read before another decision.",
        )

    if latest_run.poll_required:
        return _interval_decision(
            repository=normalized_repository,
            base_branch=normalized_base_branch,
            requested_time=requested_time,
            requested_at=normalized_requested_at,
            latest_run=latest_run,
            interval_seconds=poll_interval_seconds,
            elapsed_reason_code="poll_interval_elapsed",
            pending_reason_code="poll_interval_pending",
            elapsed_detail="Merge-train poll interval has elapsed for the latest wait boundary.",
            pending_detail="Latest merge-train wait boundary is still inside the poll interval.",
        )

    return _interval_decision(
        repository=normalized_repository,
        base_branch=normalized_base_branch,
        requested_time=requested_time,
        requested_at=normalized_requested_at,
        latest_run=latest_run,
        interval_seconds=backoff_seconds,
        elapsed_reason_code="backoff_elapsed",
        pending_reason_code="backoff_pending",
        elapsed_detail="Merge-train backoff interval has elapsed for the latest mutation result.",
        pending_detail="Latest merge-train mutation result is still inside the backoff interval.",
    )


def _interval_decision(
    *,
    repository: str,
    base_branch: str,
    requested_time: datetime,
    requested_at: str,
    latest_run: MergeTrainRunRecord,
    interval_seconds: int,
    elapsed_reason_code: MergeTrainAdmissionReason,
    pending_reason_code: MergeTrainAdmissionReason,
    elapsed_detail: str,
    pending_detail: str,
) -> MergeTrainAdmissionDecision:
    latest_time = _parse_timestamp(latest_run.recorded_at)
    next_allowed_time = latest_time + timedelta(seconds=interval_seconds)
    if requested_time >= next_allowed_time:
        return _decision(
            repository=repository,
            base_branch=base_branch,
            status="admitted",
            reason_code=elapsed_reason_code,
            requested_at=requested_at,
            next_allowed_at=_format_timestamp(requested_time),
            latest_run=latest_run,
            detail=elapsed_detail,
        )
    return _decision(
        repository=repository,
        base_branch=base_branch,
        status="deferred",
        reason_code=pending_reason_code,
        requested_at=requested_at,
        next_allowed_at=_format_timestamp(next_allowed_time),
        latest_run=latest_run,
        detail=pending_detail,
    )


def _decision(
    *,
    repository: str,
    base_branch: str,
    status: MergeTrainAdmissionStatus,
    reason_code: MergeTrainAdmissionReason,
    requested_at: str,
    next_allowed_at: str,
    detail: str,
    latest_run: MergeTrainRunRecord | None = None,
) -> MergeTrainAdmissionDecision:
    return MergeTrainAdmissionDecision(
        repository=repository,
        base_branch=base_branch,
        status=status,
        reason_code=reason_code,
        requested_at=requested_at,
        next_allowed_at=next_allowed_at,
        latest_run_id=latest_run.run_id if latest_run is not None else "",
        latest_run_status=latest_run.status if latest_run is not None else "",
        latest_run_recorded_at=latest_run.recorded_at if latest_run is not None else "",
        detail=detail,
    )


def _validate_latest_run_scope(
    *, latest_run: MergeTrainRunRecord, repository: str, base_branch: str
) -> None:
    if latest_run.repository != repository or latest_run.base_branch != base_branch:
        raise ValueError(
            "latest merge train run scope does not match requested repository/base_branch"
        )


def _parse_timestamp(value: str) -> datetime:
    normalized = _normalize_required_value(value, "merge train admission requires timestamp")
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _normalize_required_value(value: str, error_message: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(error_message)
    return normalized_value
