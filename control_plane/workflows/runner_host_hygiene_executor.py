from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import os
import pwd
import subprocess
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAction
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditStatus
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyPolicy
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyPlan
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyRequest
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneObservation
from control_plane.contracts.runner_host_hygiene import RunnerHostHygienePolicy
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneReport
from control_plane.contracts.runner_host_hygiene import evaluate_runner_host_hygiene
from control_plane.contracts.runner_host_hygiene import plan_runner_host_hygiene_apply
from control_plane.workflows.ship import utc_now_timestamp


AUDIT_ROUTE_PATH = "/v1/evidence/runner-host-hygiene/audits"
DEFAULT_PRUNE_UNTIL = "168h"
HOST_LOCK_PATH = "/tmp/launchplane-runner-host-hygiene.lock"


@dataclass(frozen=True)
class RemoteCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


RemoteCommandRunner = Callable[[Sequence[str], int], RemoteCommandResult]
AuditPoster = Callable[[RunnerHostHygieneApplyAuditRecord, str], dict[str, object]]


class RunnerHostHygieneExecutorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    action: RunnerHostHygieneApplyAction
    host_name: str
    execution_lane: str
    service_user: str
    repository_scope: str
    audit_record_key: str
    retained_warm_builders: tuple[str, ...]
    mutate: bool = False
    minimum_free_disk_bytes: int = Field(default=0, ge=0)
    timeout_seconds: int = Field(default=120, ge=1)
    prune_until: str = DEFAULT_PRUNE_UNTIL

    @model_validator(mode="after")
    def _validate_first_lane(self) -> "RunnerHostHygieneExecutorRequest":
        if self.action != "prune_docker_cache":
            raise ValueError("runner host hygiene executor only supports prune_docker_cache")
        (
            self.host_name,
            self.execution_lane,
            self.service_user,
            self.repository_scope,
            self.audit_record_key,
            self.prune_until,
        ) = _strip_text_fields(
            self.host_name,
            self.execution_lane,
            self.service_user,
            self.repository_scope,
            self.audit_record_key,
            self.prune_until,
        )
        self.retained_warm_builders = tuple(
            token.strip().lower() for token in self.retained_warm_builders if token.strip()
        )
        if not self.host_name:
            raise ValueError("runner host hygiene executor requires host_name")
        if not self.execution_lane:
            raise ValueError("runner host hygiene executor requires execution_lane")
        if not self.service_user:
            raise ValueError("runner host hygiene executor requires service_user")
        if "/" not in self.repository_scope:
            raise ValueError("runner host hygiene executor requires repository_scope as owner/name")
        if not self.audit_record_key:
            raise ValueError("runner host hygiene executor requires audit_record_key")
        if not self.retained_warm_builders:
            raise ValueError("runner host hygiene executor requires retained_warm_builders")
        if not self.prune_until:
            raise ValueError("runner host hygiene executor requires prune_until")
        return self


class RunnerHostHygieneExecutorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    audit_record_key: str
    planned_response: dict[str, object]
    terminal_response: dict[str, object] | None = None
    message: str


def _strip_text_fields(*values: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in values)


def execute_runner_host_hygiene_executor(
    *,
    request: RunnerHostHygieneExecutorRequest,
    remote_runner: RemoteCommandRunner,
    audit_poster: AuditPoster,
) -> RunnerHostHygieneExecutorResult:
    pre_report = collect_runner_host_hygiene_report(
        request=request,
        remote_runner=remote_runner,
    )
    apply_request = RunnerHostHygieneApplyRequest(
        action=request.action,
        host_name=request.host_name,
        mutate=request.mutate,
        retained_warm_builders=request.retained_warm_builders,
        audit_record_key=request.audit_record_key,
    )
    apply_plan = plan_runner_host_hygiene_apply(
        policy=RunnerHostHygieneApplyPolicy(
            approved_hosts=(request.host_name,),
            required_retained_warm_builders=request.retained_warm_builders,
            allow_docker_cache_prune=True,
        ),
        request=apply_request,
        report=pre_report,
    )
    planned_audit = RunnerHostHygieneApplyAuditRecord(
        audit_record_key=request.audit_record_key,
        status="planned",
        request=apply_request,
        plan=apply_plan,
        pre_apply_report=pre_report,
        message="planned runner host hygiene apply; no host mutation was executed yet",
    )
    planned_response = audit_poster(
        planned_audit,
        f"runner-host-hygiene:{request.audit_record_key}:planned",
    )
    if apply_plan.status != "ready":
        return RunnerHostHygieneExecutorResult(
            status="blocked",
            audit_record_key=request.audit_record_key,
            planned_response=planned_response,
            message=apply_plan.summary,
        )

    idle_result = _check_host_idle(request=request, remote_runner=remote_runner)
    if idle_result is not None:
        post_report = collect_runner_host_hygiene_report(
            request=request,
            remote_runner=remote_runner,
        )
        terminal_message = (
            f"runner host hygiene apply blocked by active build processes: {idle_result}"
        )
        terminal_response = _post_terminal_audit(
            audit_poster=audit_poster,
            request=apply_request,
            apply_plan=apply_plan,
            pre_report=pre_report,
            post_report=post_report,
            status="failed",
            message=terminal_message,
        )
        return RunnerHostHygieneExecutorResult(
            status="failed",
            audit_record_key=request.audit_record_key,
            planned_response=planned_response,
            terminal_response=terminal_response,
            message=terminal_message,
        )

    prune_result = remote_runner(
        (
            "flock",
            "-n",
            HOST_LOCK_PATH,
            "docker",
            "builder",
            "prune",
            "--force",
            "--filter",
            f"until={request.prune_until}",
        ),
        request.timeout_seconds,
    )
    post_report = collect_runner_host_hygiene_report(
        request=request,
        remote_runner=remote_runner,
    )
    terminal_status: RunnerHostHygieneApplyAuditStatus = (
        "completed"
        if prune_result.returncode == 0 and post_report.status == "healthy"
        else "failed"
    )
    terminal_message = _terminal_message(prune_result=prune_result, post_report=post_report)
    terminal_response = _post_terminal_audit(
        audit_poster=audit_poster,
        request=apply_request,
        apply_plan=apply_plan,
        pre_report=pre_report,
        post_report=post_report,
        status=terminal_status,
        message=terminal_message,
    )
    return RunnerHostHygieneExecutorResult(
        status=terminal_status,
        audit_record_key=request.audit_record_key,
        planned_response=planned_response,
        terminal_response=terminal_response,
        message=terminal_message,
    )


def collect_runner_host_hygiene_report(
    *,
    request: RunnerHostHygieneExecutorRequest,
    remote_runner: RemoteCommandRunner,
) -> RunnerHostHygieneReport:
    df_result = _require_remote_success(
        remote_runner(("df", "-B1", "-P", "/"), request.timeout_seconds),
        evidence_name="df",
    )
    docker_summary = _require_remote_success(
        remote_runner(
            (
                "docker",
                "system",
                "df",
                "--format",
                "{{.Type}} {{.TotalCount}} {{.Size}} {{.Reclaimable}}",
            ),
            request.timeout_seconds,
        ),
        evidence_name="docker_summary",
    )
    warm_builders = tuple(
        builder
        for builder in request.retained_warm_builders
        if remote_runner(
            ("docker", "image", "inspect", builder), request.timeout_seconds
        ).returncode
        == 0
    )
    observation = RunnerHostHygieneObservation(
        host_name=request.host_name,
        observed_at=utc_now_timestamp(),
        free_disk_bytes=_parse_df_available_bytes(df_result.stdout),
        warm_builders=warm_builders,
        notes=(
            f"execution_lane={request.execution_lane}",
            f"service_user={request.service_user}",
            f"repository_scope={request.repository_scope}",
            f"docker_summary={_compact_evidence(docker_summary.stdout)}",
        ),
    )
    return evaluate_runner_host_hygiene(
        policy=RunnerHostHygienePolicy(
            minimum_free_disk_bytes=request.minimum_free_disk_bytes,
            required_warm_builders=request.retained_warm_builders,
        ),
        observation=observation,
    )


def build_local_command_runner() -> RemoteCommandRunner:
    def run(command_args: Sequence[str], timeout_seconds: int) -> RemoteCommandResult:
        completed = subprocess.run(
            tuple(command_args),
            capture_output=True,
            text=True,
            timeout=max(timeout_seconds, 1),
        )
        return RemoteCommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    return run


def validate_local_executor_environment(
    *,
    request: RunnerHostHygieneExecutorRequest,
    env: Mapping[str, str] | None = None,
    current_user: str | None = None,
) -> None:
    resolved_env = os.environ if env is None else env
    resolved_current_user = current_user or pwd.getpwuid(os.geteuid()).pw_name
    if resolved_current_user != request.service_user:
        raise click.ClickException(
            "Runner host hygiene executor user must match the approved service user."
        )
    github_repository = resolved_env.get("GITHUB_REPOSITORY", "").strip()
    if github_repository and github_repository != request.repository_scope:
        raise click.ClickException(
            "Runner host hygiene repository scope must match GITHUB_REPOSITORY."
        )


def build_service_audit_poster(*, service_url: str, bearer_token: str) -> AuditPoster:
    normalized_service_url = service_url.strip().rstrip("/")
    normalized_bearer_token = bearer_token.strip()
    if not normalized_service_url:
        raise click.ClickException("runner host hygiene executor requires service_url")
    if not normalized_bearer_token:
        raise click.ClickException("runner host hygiene executor requires bearer token")

    def post(audit: RunnerHostHygieneApplyAuditRecord, idempotency_key: str) -> dict[str, object]:
        body = json.dumps(_audit_route_payload(audit)).encode()
        request = Request(
            f"{normalized_service_url}{AUDIT_ROUTE_PATH}",
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {normalized_bearer_token}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            response_text = error.read().decode(errors="replace")
            raise click.ClickException(
                response_text.strip() or f"Launchplane service returned HTTP {error.code}."
            ) from error
        if not isinstance(response_payload, dict):
            raise click.ClickException("Launchplane service returned a non-object response.")
        return response_payload

    return post


def _audit_route_payload(audit: RunnerHostHygieneApplyAuditRecord) -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "launchplane",
        "audit": audit.model_dump(mode="json"),
    }


def _required_env(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise click.ClickException(f"Missing required env var: {name}")
    return value


def _require_remote_success(
    result: RemoteCommandResult, *, evidence_name: str
) -> RemoteCommandResult:
    if result.returncode == 0:
        return result
    detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
    raise click.ClickException(f"runner host hygiene {evidence_name} evidence failed: {detail}")


def _parse_df_available_bytes(output: str) -> int:
    lines = [line.split() for line in output.splitlines() if line.strip()]
    if len(lines) < 2 or len(lines[1]) < 4:
        raise click.ClickException(
            "runner host hygiene df evidence did not include available bytes"
        )
    available = lines[1][3]
    if not available.isdigit():
        raise click.ClickException("runner host hygiene df available bytes was not numeric")
    return int(available)


def _compact_evidence(output: str) -> str:
    compact = " | ".join(line.strip() for line in output.splitlines() if line.strip())
    return compact[:500]


def _check_host_idle(
    *, request: RunnerHostHygieneExecutorRequest, remote_runner: RemoteCommandRunner
) -> str | None:
    active_processes = _require_remote_success(
        remote_runner(
            (
                "bash",
                "-lc",
                "pgrep -af '[d]ocker buildx|[d]ocker build|[b]uildctl' || true",
            ),
            request.timeout_seconds,
        ),
        evidence_name="active_build_processes",
    )
    lines = tuple(
        line.strip()
        for line in active_processes.stdout.splitlines()
        if line.strip() and "runner-host-hygiene" not in line
    )
    if lines:
        return _compact_evidence("\n".join(lines))
    return None


def _post_terminal_audit(
    *,
    audit_poster: AuditPoster,
    request: RunnerHostHygieneApplyRequest,
    apply_plan: RunnerHostHygieneApplyPlan,
    pre_report: RunnerHostHygieneReport,
    post_report: RunnerHostHygieneReport,
    status: RunnerHostHygieneApplyAuditStatus,
    message: str,
) -> dict[str, object]:
    terminal_audit = RunnerHostHygieneApplyAuditRecord(
        audit_record_key=request.audit_record_key,
        status=status,
        request=request,
        plan=apply_plan,
        pre_apply_report=pre_report,
        post_apply_report=post_report,
        message=message,
    )
    return audit_poster(
        terminal_audit,
        f"runner-host-hygiene:{request.audit_record_key}:{status}",
    )


def _terminal_message(
    *, prune_result: RemoteCommandResult, post_report: RunnerHostHygieneReport
) -> str:
    if prune_result.returncode != 0:
        detail = prune_result.stderr.strip() or prune_result.stdout.strip() or "unknown error"
        return f"runner host Docker cache prune failed: {detail}"
    if post_report.status != "healthy":
        return f"runner host Docker cache prune completed but post evidence is not healthy: {post_report.summary}"
    return "runner host Docker cache prune completed and post evidence is healthy"
