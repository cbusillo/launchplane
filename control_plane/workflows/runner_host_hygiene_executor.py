from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAction
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditStatus
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyPolicy
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyRequest
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneObservation
from control_plane.contracts.runner_host_hygiene import RunnerHostHygienePolicy
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneReport
from control_plane.contracts.runner_host_hygiene import evaluate_runner_host_hygiene
from control_plane.contracts.runner_host_hygiene import plan_runner_host_hygiene_apply
from control_plane.workflows.ship import utc_now_timestamp


SSH_PRIVATE_KEY_ENV_VAR = "LAUNCHPLANE_RUNNER_HOST_HYGIENE_SSH_PRIVATE_KEY"
SSH_KNOWN_HOSTS_ENV_VAR = "LAUNCHPLANE_RUNNER_HOST_HYGIENE_SSH_KNOWN_HOSTS"
SSH_HOST_ENV_VAR = "LAUNCHPLANE_RUNNER_HOST_HYGIENE_SSH_HOST"
SSH_USER_ENV_VAR = "LAUNCHPLANE_RUNNER_HOST_HYGIENE_SSH_USER"
AUDIT_ROUTE_PATH = "/v1/evidence/runner-host-hygiene/audits"


@dataclass(frozen=True)
class RemoteCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


RemoteCommandRunner = Callable[[Sequence[str], int], RemoteCommandResult]
AuditPoster = Callable[[RunnerHostHygieneApplyAuditRecord, str], dict[str, object]]


class RunnerHostHygieneSshExecutorRequest(BaseModel):
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

    @model_validator(mode="after")
    def _validate_first_lane(self) -> "RunnerHostHygieneSshExecutorRequest":
        if self.action != "prune_docker_cache":
            raise ValueError("runner host SSH executor only supports prune_docker_cache")
        self.host_name = self.host_name.strip()
        self.execution_lane = self.execution_lane.strip()
        self.service_user = self.service_user.strip()
        self.repository_scope = self.repository_scope.strip()
        self.audit_record_key = self.audit_record_key.strip()
        self.retained_warm_builders = tuple(
            token.strip().lower() for token in self.retained_warm_builders if token.strip()
        )
        if not self.host_name:
            raise ValueError("runner host SSH executor requires host_name")
        if not self.execution_lane:
            raise ValueError("runner host SSH executor requires execution_lane")
        if not self.service_user:
            raise ValueError("runner host SSH executor requires service_user")
        if "/" not in self.repository_scope:
            raise ValueError("runner host SSH executor requires repository_scope as owner/name")
        if not self.audit_record_key:
            raise ValueError("runner host SSH executor requires audit_record_key")
        if not self.retained_warm_builders:
            raise ValueError("runner host SSH executor requires retained_warm_builders")
        return self


class RunnerHostHygieneSshExecutorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    audit_record_key: str
    planned_response: dict[str, object]
    terminal_response: dict[str, object] | None = None
    message: str


def execute_runner_host_hygiene_ssh_executor(
    *,
    request: RunnerHostHygieneSshExecutorRequest,
    remote_runner: RemoteCommandRunner,
    audit_poster: AuditPoster,
) -> RunnerHostHygieneSshExecutorResult:
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
        return RunnerHostHygieneSshExecutorResult(
            status="blocked",
            audit_record_key=request.audit_record_key,
            planned_response=planned_response,
            message=apply_plan.summary,
        )

    prune_result = remote_runner(
        ("docker", "builder", "prune", "--all", "--force"), request.timeout_seconds
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
    terminal_audit = RunnerHostHygieneApplyAuditRecord(
        audit_record_key=request.audit_record_key,
        status=terminal_status,
        request=apply_request,
        plan=apply_plan,
        pre_apply_report=pre_report,
        post_apply_report=post_report,
        message=terminal_message,
    )
    terminal_response = audit_poster(
        terminal_audit,
        f"runner-host-hygiene:{request.audit_record_key}:{terminal_status}",
    )
    return RunnerHostHygieneSshExecutorResult(
        status=terminal_status,
        audit_record_key=request.audit_record_key,
        planned_response=planned_response,
        terminal_response=terminal_response,
        message=terminal_message,
    )


def collect_runner_host_hygiene_report(
    *,
    request: RunnerHostHygieneSshExecutorRequest,
    remote_runner: RemoteCommandRunner,
) -> RunnerHostHygieneReport:
    df_result = _require_remote_success(
        remote_runner(("df", "-B1", "-P", "/"), request.timeout_seconds),
        evidence_name="df",
    )
    docker_summary = _require_remote_success(
        remote_runner(("docker", "system", "df"), request.timeout_seconds),
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


def build_ssh_remote_runner(env: Mapping[str, str] | None = None) -> RemoteCommandRunner:
    resolved_env = env or os.environ

    def run(command_args: Sequence[str], timeout_seconds: int) -> RemoteCommandResult:
        with TemporaryDirectory(prefix="launchplane-runner-host-hygiene-") as material_dir_name:
            identity_file, known_hosts_file = _write_ssh_material(
                material_dir=Path(material_dir_name), env=resolved_env
            )
            command = _build_ssh_command(
                command_args,
                identity_file=identity_file,
                known_hosts_file=known_hosts_file,
                env=resolved_env,
            )
            completed = subprocess.run(
                command,
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


def validate_ssh_executor_environment(
    *, request: RunnerHostHygieneSshExecutorRequest, env: Mapping[str, str] | None = None
) -> None:
    resolved_env = env or os.environ
    configured_user = _required_env(resolved_env, SSH_USER_ENV_VAR)
    if configured_user != request.service_user:
        raise click.ClickException(
            "Runner host hygiene SSH user must match the approved service user."
        )
    _required_env(resolved_env, SSH_HOST_ENV_VAR)
    _required_env(resolved_env, SSH_PRIVATE_KEY_ENV_VAR)
    _required_env(resolved_env, SSH_KNOWN_HOSTS_ENV_VAR)


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


def _write_ssh_material(*, material_dir: Path, env: Mapping[str, str]) -> tuple[str, str]:
    private_key = _required_env(env, SSH_PRIVATE_KEY_ENV_VAR)
    known_hosts = _required_env(env, SSH_KNOWN_HOSTS_ENV_VAR)
    identity_file = material_dir / "runner-host-hygiene-key"
    known_hosts_file = material_dir / "known_hosts"
    _write_secret_file(path=identity_file, value=private_key)
    _write_secret_file(path=known_hosts_file, value=known_hosts)
    return str(identity_file), str(known_hosts_file)


def _write_secret_file(*, path: Path, value: str) -> None:
    path.write_text(f"{value.rstrip()}\n", encoding="utf-8")
    path.chmod(0o600)


def _build_ssh_command(
    command_args: Sequence[str],
    *,
    identity_file: str,
    known_hosts_file: str,
    env: Mapping[str, str],
) -> list[str]:
    ssh_host = _required_env(env, SSH_HOST_ENV_VAR)
    ssh_user = _required_env(env, SSH_USER_ENV_VAR)
    return [
        "ssh",
        "-F",
        "/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts_file}",
        "-i",
        identity_file,
        f"{ssh_user}@{ssh_host}",
        *command_args,
    ]


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


def _terminal_message(
    *, prune_result: RemoteCommandResult, post_report: RunnerHostHygieneReport
) -> str:
    if prune_result.returncode != 0:
        detail = prune_result.stderr.strip() or prune_result.stdout.strip() or "unknown error"
        return f"runner host Docker cache prune failed: {detail}"
    if post_report.status != "healthy":
        return f"runner host Docker cache prune completed but post evidence is not healthy: {post_report.summary}"
    return "runner host Docker cache prune completed and post evidence is healthy"
