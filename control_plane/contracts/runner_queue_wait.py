from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


RunnerQueueWaitStatus = Literal["unknown", "not_capacity_constrained", "capacity_constrained"]


class RunnerQueueWaitJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    github_id: int = Field(ge=1)
    run_id: int = Field(ge=1)
    repository: str
    workflow_name: str = ""
    job_name: str
    status: str
    conclusion: str = ""
    labels: tuple[str, ...] = ()
    runner_name: str = ""
    runner_group_name: str = ""
    html_url: str = ""
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""
    queue_wait_seconds: int | None = Field(default=None, ge=0)
    queue_wait_reason: str

    @model_validator(mode="after")
    def _normalize_job(self) -> "RunnerQueueWaitJob":
        self.repository = _required_text(
            self.repository, "runner queue wait job requires repository"
        )
        self.workflow_name = self.workflow_name.strip()
        self.job_name = _required_text(self.job_name, "runner queue wait job requires job_name")
        self.status = _required_text(self.status, "runner queue wait job requires status").lower()
        self.conclusion = self.conclusion.strip().lower()
        self.labels = tuple(sorted({label.strip() for label in self.labels if label.strip()}))
        self.runner_name = self.runner_name.strip()
        self.runner_group_name = self.runner_group_name.strip()
        self.html_url = self.html_url.strip()
        self.created_at = self.created_at.strip()
        self.started_at = self.started_at.strip()
        self.completed_at = self.completed_at.strip()
        self.queue_wait_reason = _required_text(
            self.queue_wait_reason, "runner queue wait job requires queue_wait_reason"
        )
        return self


class RunnerQueueWaitSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository: str
    observed_at: str
    workflow_runs_scanned: int = Field(ge=0)
    jobs_scanned: int = Field(ge=0)
    known_wait_jobs: int = Field(ge=0)
    unknown_wait_jobs: int = Field(ge=0)
    constrained_threshold_seconds: int = Field(ge=0)
    max_queue_wait_seconds: int | None = Field(default=None, ge=0)
    average_queue_wait_seconds: float | None = Field(default=None, ge=0)
    queue_wait_status: RunnerQueueWaitStatus
    capacity_constrained: bool | None
    capacity_reason: str
    inventory_capacity_constrained: bool | None = None
    inventory_capacity_reason: str = ""
    jobs: tuple[RunnerQueueWaitJob, ...]

    @model_validator(mode="after")
    def _normalize_summary(self) -> "RunnerQueueWaitSummary":
        self.repository = _required_text(
            self.repository, "runner queue wait summary requires repository"
        )
        self.observed_at = _required_text(
            self.observed_at, "runner queue wait summary requires observed_at"
        )
        self.capacity_reason = _required_text(
            self.capacity_reason, "runner queue wait summary requires capacity_reason"
        )
        self.inventory_capacity_reason = self.inventory_capacity_reason.strip()
        self.jobs = tuple(
            sorted(self.jobs, key=lambda job: (job.run_id, job.job_name, job.github_id))
        )
        return self


def build_runner_queue_wait_job(
    *,
    github_id: int,
    run_id: int,
    repository: str,
    workflow_name: str = "",
    job_name: str,
    status: str,
    conclusion: str = "",
    labels: tuple[str, ...] = (),
    runner_name: str = "",
    runner_group_name: str = "",
    html_url: str = "",
    created_at: str = "",
    started_at: str = "",
    completed_at: str = "",
) -> RunnerQueueWaitJob:
    queue_wait_seconds, queue_wait_reason = _queue_wait_seconds(
        created_at=created_at, started_at=started_at
    )
    return RunnerQueueWaitJob(
        github_id=github_id,
        run_id=run_id,
        repository=repository,
        workflow_name=workflow_name,
        job_name=job_name,
        status=status,
        conclusion=conclusion,
        labels=labels,
        runner_name=runner_name,
        runner_group_name=runner_group_name,
        html_url=html_url,
        created_at=created_at,
        started_at=started_at,
        completed_at=completed_at,
        queue_wait_seconds=queue_wait_seconds,
        queue_wait_reason=queue_wait_reason,
    )


def build_runner_queue_wait_summary(
    *,
    repository: str,
    observed_at: str,
    workflow_runs_scanned: int,
    jobs: tuple[RunnerQueueWaitJob, ...],
    constrained_threshold_seconds: int = 300,
    inventory_capacity_constrained: bool | None = None,
    inventory_capacity_reason: str = "",
) -> RunnerQueueWaitSummary:
    known_waits = tuple(
        job.queue_wait_seconds for job in jobs if job.queue_wait_seconds is not None
    )
    max_queue_wait_seconds = max(known_waits) if known_waits else None
    average_queue_wait_seconds = (
        round(sum(known_waits) / len(known_waits), 3) if known_waits else None
    )
    queue_wait_status: RunnerQueueWaitStatus = "unknown"
    capacity_constrained: bool | None = None
    capacity_reason = "GitHub Actions job timestamps are not available for recent jobs"
    if not jobs:
        capacity_reason = "no recent GitHub Actions jobs discovered"
    elif known_waits:
        if (
            max_queue_wait_seconds is not None
            and max_queue_wait_seconds >= constrained_threshold_seconds
        ):
            queue_wait_status = "capacity_constrained"
            capacity_constrained = True
            capacity_reason = (
                f"max queue wait {max_queue_wait_seconds}s meets or exceeds "
                f"{constrained_threshold_seconds}s threshold"
            )
        else:
            queue_wait_status = "not_capacity_constrained"
            capacity_constrained = False
            capacity_reason = (
                f"observed queue waits are below {constrained_threshold_seconds}s threshold"
            )
    return RunnerQueueWaitSummary(
        repository=repository,
        observed_at=observed_at,
        workflow_runs_scanned=workflow_runs_scanned,
        jobs_scanned=len(jobs),
        known_wait_jobs=len(known_waits),
        unknown_wait_jobs=len(jobs) - len(known_waits),
        constrained_threshold_seconds=constrained_threshold_seconds,
        max_queue_wait_seconds=max_queue_wait_seconds,
        average_queue_wait_seconds=average_queue_wait_seconds,
        queue_wait_status=queue_wait_status,
        capacity_constrained=capacity_constrained,
        capacity_reason=capacity_reason,
        inventory_capacity_constrained=inventory_capacity_constrained,
        inventory_capacity_reason=inventory_capacity_reason,
        jobs=jobs,
    )


def _queue_wait_seconds(*, created_at: str, started_at: str) -> tuple[int | None, str]:
    normalized_created_at = created_at.strip()
    normalized_started_at = started_at.strip()
    if not normalized_created_at and not normalized_started_at:
        return None, "missing created_at and started_at"
    if not normalized_created_at:
        return None, "missing created_at"
    if not normalized_started_at:
        return None, "missing started_at"
    created = _parse_github_timestamp(normalized_created_at)
    if created is None:
        return None, "invalid created_at"
    started = _parse_github_timestamp(normalized_started_at)
    if started is None:
        return None, "invalid started_at"
    wait_seconds = int((started - created).total_seconds())
    if wait_seconds < 0:
        return None, "started_at precedes created_at"
    return wait_seconds, "created_at and started_at available"


def _parse_github_timestamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _required_text(value: str, message: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(message)
    return normalized_value
