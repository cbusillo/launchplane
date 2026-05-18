from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import urlencode

from control_plane.contracts.runner_queue_wait import RunnerQueueWaitJob
from control_plane.contracts.runner_queue_wait import RunnerQueueWaitSummary
from control_plane.contracts.runner_queue_wait import build_runner_queue_wait_job
from control_plane.contracts.runner_queue_wait import build_runner_queue_wait_summary
from control_plane.merge_train_github import MergeTrainGitHubError
from control_plane.merge_train_github import MergeTrainGitHubTransport


class RunnerQueueWaitClock(Protocol):
    def now(self) -> datetime: ...


class SystemRunnerQueueWaitClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc).replace(microsecond=0)


class GitHubRunnerQueueWaitReader:
    def __init__(
        self,
        *,
        transport: MergeTrainGitHubTransport,
        clock: RunnerQueueWaitClock | None = None,
    ) -> None:
        self.transport = transport
        self.clock = clock or SystemRunnerQueueWaitClock()

    def read_runner_queue_wait(
        self,
        *,
        repository: str,
        workflow_run_limit: int = 20,
        constrained_threshold_seconds: int = 300,
        inventory_capacity_constrained: bool | None = None,
        inventory_capacity_reason: str = "",
    ) -> RunnerQueueWaitSummary:
        repository_path = _repository_path(repository)
        observed_at = self.clock.now().isoformat().replace("+00:00", "Z")
        workflow_runs = self._read_workflow_runs(
            repository=repository_path, workflow_run_limit=workflow_run_limit
        )
        jobs: list[RunnerQueueWaitJob] = []
        for workflow_run in workflow_runs:
            workflow_run_object = _json_object(workflow_run, "GitHub workflow run entry")
            run_id = _required_int(
                workflow_run_object.get("id"), "GitHub workflow run requires id."
            )
            workflow_name = _optional_text(workflow_run_object.get("name"))
            jobs.extend(
                self._read_workflow_run_jobs(
                    repository=repository_path,
                    run_id=run_id,
                    workflow_name=workflow_name,
                )
            )
        return build_runner_queue_wait_summary(
            repository=repository_path,
            observed_at=observed_at,
            workflow_runs_scanned=len(workflow_runs),
            jobs=tuple(jobs),
            constrained_threshold_seconds=constrained_threshold_seconds,
            inventory_capacity_constrained=inventory_capacity_constrained,
            inventory_capacity_reason=inventory_capacity_reason,
        )

    def _read_workflow_runs(
        self, *, repository: str, workflow_run_limit: int
    ) -> tuple[object, ...]:
        if workflow_run_limit < 1:
            raise ValueError("workflow run limit must be at least 1.")
        query = urlencode({"per_page": str(min(workflow_run_limit, 100))})
        payload = self.transport.request(
            method="GET", path=f"/repos/{repository}/actions/runs?{query}"
        )
        payload_object = _json_object(payload, "GitHub workflow run list response")
        workflow_runs = payload_object.get("workflow_runs")
        if not isinstance(workflow_runs, list):
            raise MergeTrainGitHubError(
                "GitHub workflow run list response must include workflow_runs."
            )
        return tuple(workflow_runs[:workflow_run_limit])

    def _read_workflow_run_jobs(
        self, *, repository: str, run_id: int, workflow_name: str
    ) -> tuple[RunnerQueueWaitJob, ...]:
        jobs: list[RunnerQueueWaitJob] = []
        page = 1
        while True:
            query = urlencode({"per_page": "100", "page": str(page)})
            payload = self.transport.request(
                method="GET", path=f"/repos/{repository}/actions/runs/{run_id}/jobs?{query}"
            )
            payload_object = _json_object(payload, "GitHub workflow run jobs response")
            jobs_payload = payload_object.get("jobs")
            if not isinstance(jobs_payload, list):
                raise MergeTrainGitHubError("GitHub workflow run jobs response must include jobs.")
            page_jobs = tuple(
                _runner_queue_wait_job(
                    repository=repository,
                    run_id=run_id,
                    workflow_name=workflow_name,
                    payload=_json_object(job, "GitHub workflow run job entry"),
                )
                for job in jobs_payload
            )
            jobs.extend(page_jobs)
            if len(page_jobs) < 100:
                return tuple(jobs)
            page += 1


def _runner_queue_wait_job(
    *, repository: str, run_id: int, workflow_name: str, payload: dict[str, object]
) -> RunnerQueueWaitJob:
    return build_runner_queue_wait_job(
        github_id=_required_int(payload.get("id"), "GitHub workflow run job requires id."),
        run_id=run_id,
        repository=repository,
        workflow_name=workflow_name,
        job_name=_required_text(payload.get("name"), "GitHub workflow run job requires name."),
        status=_required_text(payload.get("status"), "GitHub workflow run job requires status."),
        conclusion=_optional_text(payload.get("conclusion")),
        labels=_job_labels(payload.get("labels")),
        runner_name=_optional_text(payload.get("runner_name")),
        runner_group_name=_optional_text(payload.get("runner_group_name")),
        html_url=_optional_text(payload.get("html_url")),
        created_at=_optional_text(payload.get("created_at")),
        started_at=_optional_text(payload.get("started_at")),
        completed_at=_optional_text(payload.get("completed_at")),
    )


def _job_labels(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise MergeTrainGitHubError("GitHub workflow run job labels must be a JSON array.")
    return tuple(
        _required_text(label, "GitHub workflow run job label requires value.") for label in value
    )


def _repository_path(repository: str) -> str:
    normalized_repository = "/".join(part.strip() for part in repository.strip().split("/"))
    if normalized_repository.count("/") != 1:
        raise ValueError("GitHub repository must be formatted as owner/name.")
    owner, repo = normalized_repository.split("/", 1)
    if not owner or not repo:
        raise ValueError("GitHub repository must be formatted as owner/name.")
    return normalized_repository


def _json_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise MergeTrainGitHubError(f"{label} must be a JSON object.")
    return value


def _required_text(value: object, message: str) -> str:
    normalized_value = str(value or "").strip()
    if not normalized_value:
        raise MergeTrainGitHubError(message)
    return normalized_value


def _optional_text(value: object) -> str:
    return str(value or "").strip()


def _required_int(value: object, message: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise MergeTrainGitHubError(message)
    return value
