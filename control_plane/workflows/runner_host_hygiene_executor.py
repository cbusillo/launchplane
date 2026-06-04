from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from decimal import InvalidOperation
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
from control_plane.contracts.runner_host_hygiene import RunnerHostDockerToolchainObservation
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneImageInventoryItem
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneObservation
from control_plane.contracts.runner_host_hygiene import RunnerHostHygienePolicy
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneReport
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneVolumeInventoryItem
from control_plane.contracts.runner_host_hygiene import evaluate_runner_host_hygiene
from control_plane.contracts.runner_host_hygiene import plan_runner_host_hygiene_apply
from control_plane.workflows.ship import utc_now_timestamp


AUDIT_ROUTE_PATH = "/v1/evidence/runner-host-hygiene/audits"
DEFAULT_PRUNE_UNTIL = "168h"
HOST_LOCK_PATH = "/tmp/launchplane-runner-host-hygiene.lock"
_DOCKER_SYSTEM_DF_TYPES = ("Local Volumes", "Build Cache", "Containers", "Images")
_DOCKER_LOCAL_VOLUME_USAGE_HEADERS = (
    "local volumes space usage:",
    "local volumes:",
)
_RUNNER_WORKDIR_BYTES_COMMAND = (
    "find /opt/actions-runners -mindepth 2 -maxdepth 2 -type d -name _work "
    "-exec du -sb {} + 2>/dev/null | "
    "awk '{ total += $1 } END { print total + 0 }'"
)
_DOCKER_SIZE_UNITS = {
    "b": Decimal(1),
    "kb": Decimal(1000),
    "mb": Decimal(1000**2),
    "gb": Decimal(1000**3),
    "tb": Decimal(1000**4),
    "kib": Decimal(1024),
    "mib": Decimal(1024**2),
    "gib": Decimal(1024**3),
    "tib": Decimal(1024**4),
}


@dataclass(frozen=True)
class RemoteCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


RemoteCommandRunner = Callable[[Sequence[str], int], RemoteCommandResult]
AuditPoster = Callable[[RunnerHostHygieneApplyAuditRecord, str], dict[str, object]]
BearerTokenProvider = Callable[[], str]


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
    target_buildkit_state_volumes: tuple[str, ...] = ()
    allowed_buildkit_state_volumes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_request(self) -> "RunnerHostHygieneExecutorRequest":
        if self.action not in {"prune_docker_cache", "remove_buildkit_state_volumes"}:
            raise ValueError(
                "runner host hygiene executor only supports prune_docker_cache "
                "and remove_buildkit_state_volumes"
            )
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
        self.target_buildkit_state_volumes = tuple(
            sorted(
                {
                    volume_name.strip()
                    for volume_name in self.target_buildkit_state_volumes
                    if volume_name.strip()
                }
            )
        )
        self.allowed_buildkit_state_volumes = tuple(
            sorted(
                {
                    volume_name.strip()
                    for volume_name in self.allowed_buildkit_state_volumes
                    if volume_name.strip()
                }
            )
        )
        if self.action == "prune_docker_cache" and self.target_buildkit_state_volumes:
            raise ValueError("Docker cache prune requests cannot include target volumes")
        if self.action == "prune_docker_cache" and self.allowed_buildkit_state_volumes:
            raise ValueError("Docker cache prune requests cannot include allowed volumes")
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
        target_buildkit_state_volumes=request.target_buildkit_state_volumes,
        audit_record_key=request.audit_record_key,
    )
    apply_plan = plan_runner_host_hygiene_apply(
        policy=RunnerHostHygieneApplyPolicy(
            approved_hosts=(request.host_name,),
            required_retained_warm_builders=request.retained_warm_builders,
            require_healthy_report=request.action != "remove_buildkit_state_volumes",
            allow_docker_cache_prune=request.action == "prune_docker_cache",
            allow_buildkit_state_volume_remove=(request.action == "remove_buildkit_state_volumes"),
            allowed_buildkit_state_volumes=request.allowed_buildkit_state_volumes,
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

    action_result = _execute_apply_action(request=request, remote_runner=remote_runner)
    post_report = collect_runner_host_hygiene_report(
        request=request,
        remote_runner=remote_runner,
    )
    terminal_status: RunnerHostHygieneApplyAuditStatus = (
        "completed"
        if action_result.returncode == 0 and post_report.status == "healthy"
        else "failed"
    )
    terminal_message = _terminal_message(
        action=request.action,
        action_result=action_result,
        post_report=post_report,
    )
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


def _execute_apply_action(
    *, request: RunnerHostHygieneExecutorRequest, remote_runner: RemoteCommandRunner
) -> RemoteCommandResult:
    if request.action == "remove_buildkit_state_volumes":
        target_volume = request.target_buildkit_state_volumes[0]
        return remote_runner(
            (
                "flock",
                "-n",
                HOST_LOCK_PATH,
                "docker",
                "volume",
                "rm",
                target_volume,
            ),
            request.timeout_seconds,
        )
    return remote_runner(
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
    image_inventory = _collect_image_inventory(
        request=request,
        remote_runner=remote_runner,
    )
    container_inventory = _collect_container_inventory(
        request=request,
        remote_runner=remote_runner,
    )
    volume_inventory = _collect_volume_inventory(
        request=request,
        remote_runner=remote_runner,
    )
    observation = RunnerHostHygieneObservation(
        host_name=request.host_name,
        observed_at=utc_now_timestamp(),
        free_disk_bytes=_parse_df_available_bytes(df_result.stdout),
        docker_reclaimable_bytes=_parse_docker_system_df_reclaimable_bytes(docker_summary.stdout),
        runner_workdir_bytes=_collect_runner_workdir_bytes(
            request=request,
            remote_runner=remote_runner,
        ),
        docker_toolchain=_collect_docker_toolchain(
            request=request,
            remote_runner=remote_runner,
        ),
        warm_builders=warm_builders,
        image_inventory=image_inventory,
        volume_inventory=volume_inventory,
        orphan_buildkit_containers=_count_orphan_buildkit_containers(container_inventory),
        orphan_buildkit_volumes=_count_orphan_buildkit_volumes(volume_inventory),
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
        bounded_timeout = max(timeout_seconds, 1)
        try:
            completed = subprocess.run(
                tuple(command_args),
                capture_output=True,
                text=True,
                timeout=bounded_timeout,
            )
        except subprocess.TimeoutExpired as error:
            stdout = _timeout_output_text(error.stdout)
            stderr = _timeout_output_text(error.stderr)
            detail = stderr or f"command timed out after {bounded_timeout} seconds"
            return RemoteCommandResult(returncode=124, stdout=stdout, stderr=detail)
        return RemoteCommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    return run


def _timeout_output_text(output: str | bytes | bytearray | memoryview | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    if isinstance(output, bytearray):
        return bytes(output).decode(errors="replace")
    if isinstance(output, memoryview):
        return output.tobytes().decode(errors="replace")
    return output


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
    normalized_bearer_token = bearer_token.strip()
    if not normalized_bearer_token:
        raise click.ClickException("runner host hygiene executor requires bearer token")
    return build_refreshing_service_audit_poster(
        service_url=service_url,
        bearer_token_provider=lambda: normalized_bearer_token,
    )


def build_refreshing_service_audit_poster(
    *, service_url: str, bearer_token_provider: BearerTokenProvider
) -> AuditPoster:
    normalized_service_url = service_url.strip().rstrip("/")
    if not normalized_service_url:
        raise click.ClickException("runner host hygiene executor requires service_url")

    def post(audit: RunnerHostHygieneApplyAuditRecord, idempotency_key: str) -> dict[str, object]:
        bearer_token = bearer_token_provider().strip()
        if not bearer_token:
            raise click.ClickException("runner host hygiene executor requires bearer token")
        body = json.dumps(_audit_route_payload(audit)).encode()
        request = Request(
            f"{normalized_service_url}{AUDIT_ROUTE_PATH}",
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {bearer_token}",
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


def _collect_docker_toolchain(
    *,
    request: RunnerHostHygieneExecutorRequest,
    remote_runner: RemoteCommandRunner,
) -> RunnerHostDockerToolchainObservation | None:
    buildx_plugin_path = _first_existing_path(
        remote_runner(
            (
                "sh",
                "-c",
                "command -v docker-buildx 2>/dev/null || "
                "for path in /usr/libexec/docker/cli-plugins/docker-buildx "
                "/usr/local/lib/docker/cli-plugins/docker-buildx "
                "$HOME/.docker/cli-plugins/docker-buildx; do "
                '[ -x "$path" ] && { printf \'%s\\n\' "$path"; break; }; '
                "done",
            ),
            request.timeout_seconds,
        ).stdout
    )
    toolchain = RunnerHostDockerToolchainObservation(
        docker_engine_version=_command_output(
            remote_runner,
            ("docker", "version", "--format", "{{.Server.Version}}"),
            request.timeout_seconds,
        ),
        docker_cli_version=_command_output(
            remote_runner,
            ("docker", "version", "--format", "{{.Client.Version}}"),
            request.timeout_seconds,
        ),
        docker_buildx_version=_first_version(
            _command_output(
                remote_runner,
                ("docker", "buildx", "version"),
                request.timeout_seconds,
            )
        ),
        docker_buildx_plugin_path=buildx_plugin_path,
        docker_buildx_package=_docker_buildx_package(
            remote_runner=remote_runner,
            timeout_seconds=request.timeout_seconds,
        ),
        docker_buildx_source=_docker_buildx_source(buildx_plugin_path),
        buildkit_version=_first_version(
            _command_output(
                remote_runner,
                ("docker", "buildx", "inspect"),
                request.timeout_seconds,
            )
        ),
    )
    return toolchain if toolchain.has_evidence() else None


def _command_output(
    remote_runner: RemoteCommandRunner,
    command_args: tuple[str, ...],
    timeout_seconds: int,
) -> str:
    result = remote_runner(command_args, timeout_seconds)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _first_existing_path(output: str) -> str:
    for line in output.splitlines():
        value = line.strip()
        if value:
            return value
    return ""


def _docker_buildx_package(*, remote_runner: RemoteCommandRunner, timeout_seconds: int) -> str:
    package = _command_output(
        remote_runner,
        ("sh", "-c", "dpkg-query -W -f='${Package} ${Version}' docker-buildx 2>/dev/null"),
        timeout_seconds,
    )
    if package:
        return package
    return _command_output(
        remote_runner,
        ("sh", "-c", "rpm -q docker-buildx 2>/dev/null"),
        timeout_seconds,
    )


def _docker_buildx_source(plugin_path: str) -> str:
    if plugin_path.startswith("/usr/libexec/") or plugin_path.startswith("/usr/lib/"):
        return "system package"
    if plugin_path.startswith("/usr/local/"):
        return "managed plugin"
    if "/.docker/cli-plugins/" in plugin_path:
        return "user plugin"
    return ""


def _first_version(value: str) -> str:
    for token in value.replace(",", " ").split():
        normalized = token.strip().removeprefix("v")
        if normalized and normalized[0].isdigit() and "." in normalized:
            return normalized
    return ""


def _collect_image_inventory(
    *,
    request: RunnerHostHygieneExecutorRequest,
    remote_runner: RemoteCommandRunner,
) -> tuple[RunnerHostHygieneImageInventoryItem, ...]:
    image_result = _require_remote_success(
        remote_runner(
            (
                "docker",
                "image",
                "ls",
                "--all",
                "--no-trunc",
                "--format",
                "{{json .}}",
            ),
            request.timeout_seconds,
        ),
        evidence_name="image_inventory",
    )
    container_images = _collect_container_image_references(
        request=request,
        remote_runner=remote_runner,
    )
    inventory: list[RunnerHostHygieneImageInventoryItem] = []
    retained_builders = set(request.retained_warm_builders)
    for row in _parse_json_lines(image_result.stdout, evidence_name="image_inventory"):
        image_id = _docker_json_text(row, "ID").removeprefix("sha256:")
        repository = _docker_json_text(row, "Repository").lower()
        tag = _docker_json_text(row, "Tag").lower()
        if not image_id:
            raise click.ClickException(
                "runner host hygiene image inventory evidence did not include image IDs"
            )
        reference = _image_reference(repository=repository, tag=tag)
        inventory.append(
            RunnerHostHygieneImageInventoryItem(
                image_id=image_id,
                repository=repository,
                tag=tag,
                size_bytes=_parse_optional_docker_size_bytes(_docker_json_text(row, "Size")),
                created_at=_docker_json_text(row, "CreatedAt"),
                dangling=_is_dangling_image(repository=repository, tag=tag),
                in_use=reference in container_images or image_id in container_images,
                is_warm_builder=_is_warm_builder_image(
                    repository=repository,
                    reference=reference,
                    retained_builders=retained_builders,
                ),
            )
        )
    return tuple(inventory)


def _collect_container_inventory(
    *,
    request: RunnerHostHygieneExecutorRequest,
    remote_runner: RemoteCommandRunner,
) -> tuple[dict[str, object], ...]:
    result = _require_remote_success(
        remote_runner(
            (
                "docker",
                "ps",
                "--all",
                "--no-trunc",
                "--format",
                "{{json .}}",
            ),
            request.timeout_seconds,
        ),
        evidence_name="container_inventory",
    )
    return _parse_json_lines(result.stdout, evidence_name="container_inventory")


def _collect_container_image_references(
    *,
    request: RunnerHostHygieneExecutorRequest,
    remote_runner: RemoteCommandRunner,
) -> set[str]:
    return _container_image_references(
        _collect_container_inventory(request=request, remote_runner=remote_runner)
    )


def _container_image_references(rows: tuple[dict[str, object], ...]) -> set[str]:
    references: set[str] = set()
    for row in rows:
        image = _docker_json_text(row, "Image").lower()
        image_id = _docker_json_text(row, "ImageID").removeprefix("sha256:").lower()
        if image:
            references.add(image)
        if image_id:
            references.add(image_id)
    return references


def _count_orphan_buildkit_containers(rows: tuple[dict[str, object], ...]) -> int:
    return sum(1 for row in rows if _is_orphan_buildkit_container(row))


def _is_orphan_buildkit_container(row: Mapping[str, object]) -> bool:
    name = _docker_json_text(row, "Names").lower()
    image = _docker_json_text(row, "Image").lower()
    state = _docker_json_text(row, "State").lower()
    status = _docker_json_text(row, "Status").lower()
    is_buildkit = name.startswith("buildx_buildkit_") or "buildkit" in image
    if not is_buildkit:
        return False
    if state:
        return state not in {"running", "restarting"}
    return not status.startswith(("up ", "restarting"))


def _count_orphan_buildkit_volumes(
    volume_inventory: tuple[RunnerHostHygieneVolumeInventoryItem, ...],
) -> int:
    return sum(
        1
        for volume in volume_inventory
        if _is_buildkit_state_volume_name(volume.name)
        and (volume.dangling or volume.referenced_by_containers == 0)
    )


def _collect_runner_workdir_bytes(
    *,
    request: RunnerHostHygieneExecutorRequest,
    remote_runner: RemoteCommandRunner,
) -> int:
    result = _require_remote_success(
        remote_runner(
            ("bash", "-lc", _RUNNER_WORKDIR_BYTES_COMMAND),
            request.timeout_seconds,
        ),
        evidence_name="runner_workdir_bytes",
    )
    return _parse_non_negative_int_evidence(
        result.stdout,
        evidence_name="runner_workdir_bytes",
    )


def _is_buildkit_state_volume_name(value: str) -> bool:
    return value.startswith("buildx_buildkit_") and value.endswith("_state")


def _parse_non_negative_int_evidence(output: str, *, evidence_name: str) -> int:
    stripped_output = output.strip()
    if not stripped_output:
        raise click.ClickException(f"runner host hygiene {evidence_name} evidence was empty")
    first_line = stripped_output.splitlines()[0].strip()
    try:
        value = int(first_line)
    except ValueError as error:
        raise click.ClickException(
            f"runner host hygiene {evidence_name} evidence was not an integer"
        ) from error
    if value < 0:
        raise click.ClickException(f"runner host hygiene {evidence_name} evidence was negative")
    return value


def _collect_volume_inventory(
    *,
    request: RunnerHostHygieneExecutorRequest,
    remote_runner: RemoteCommandRunner,
) -> tuple[RunnerHostHygieneVolumeInventoryItem, ...]:
    volume_usage = _collect_volume_usage(
        request=request,
        remote_runner=remote_runner,
    )
    result = _require_remote_success(
        remote_runner(
            (
                "bash",
                "-lc",
                "docker volume ls -q | xargs -r docker volume inspect",
            ),
            request.timeout_seconds,
        ),
        evidence_name="volume_inventory",
    )
    if not result.stdout.strip():
        return ()
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise click.ClickException(
            "runner host hygiene volume inventory evidence was not valid JSON"
        ) from error
    if not isinstance(payload, list):
        raise click.ClickException(
            "runner host hygiene volume inventory evidence was not a JSON array"
        )

    inventory: list[RunnerHostHygieneVolumeInventoryItem] = []
    for row in payload:
        if not isinstance(row, dict):
            raise click.ClickException(
                "runner host hygiene volume inventory evidence contained a non-object row"
            )
        usage_data = row.get("UsageData")
        if not isinstance(usage_data, dict):
            usage_data = {}
        name = _docker_json_text(row, "Name")
        usage = volume_usage.get(name)
        inspect_ref_count = _non_negative_int(usage_data.get("RefCount"))
        ref_count = usage.links if usage is not None else inspect_ref_count
        size_bytes = (
            usage.size_bytes if usage is not None else _non_negative_int(usage_data.get("Size"))
        )
        inventory.append(
            RunnerHostHygieneVolumeInventoryItem(
                name=name,
                driver=_docker_json_text(row, "Driver"),
                mountpoint=_docker_json_text(row, "Mountpoint"),
                labels=_docker_labels(row.get("Labels")),
                size_bytes=size_bytes,
                referenced_by_containers=ref_count,
                dangling=ref_count == 0,
            )
        )
    return tuple(inventory)


class _VolumeUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    links: int = Field(default=0, ge=0)
    size_bytes: int = Field(default=0, ge=0)


def _collect_volume_usage(
    *,
    request: RunnerHostHygieneExecutorRequest,
    remote_runner: RemoteCommandRunner,
) -> dict[str, _VolumeUsage]:
    result = _require_remote_success(
        remote_runner(("docker", "system", "df", "-v"), request.timeout_seconds),
        evidence_name="volume_usage",
    )
    return _parse_volume_usage(result.stdout)


def _parse_docker_system_df_reclaimable_bytes(output: str) -> int:
    total = 0
    parsed_rows = 0
    for line in output.splitlines():
        stripped_line = line.strip()
        if not stripped_line:
            continue
        for row_type in _DOCKER_SYSTEM_DF_TYPES:
            prefix = f"{row_type} "
            if stripped_line.startswith(prefix):
                columns = stripped_line.removeprefix(prefix).split()
                if len(columns) < 3:
                    raise click.ClickException(
                        "runner host hygiene docker summary evidence was incomplete"
                    )
                total += _parse_docker_size_bytes(columns[2])
                parsed_rows += 1
                break
    if parsed_rows == 0:
        raise click.ClickException(
            "runner host hygiene docker summary evidence did not include reclaimable bytes"
        )
    return total


def _parse_volume_usage(output: str) -> dict[str, _VolumeUsage]:
    usage: dict[str, _VolumeUsage] = {}
    in_volume_section = False
    saw_volume_header = False
    for line in output.splitlines():
        stripped_line = line.strip()
        if stripped_line.lower() in _DOCKER_LOCAL_VOLUME_USAGE_HEADERS:
            in_volume_section = True
            continue
        if not in_volume_section:
            continue
        if not stripped_line:
            continue
        if stripped_line == "Build cache usage:":
            break
        if stripped_line.startswith("Build cache usage:"):
            break
        if stripped_line.startswith("VOLUME NAME"):
            saw_volume_header = True
            continue
        if not saw_volume_header:
            continue
        columns = stripped_line.split()
        if len(columns) < 3:
            raise click.ClickException(
                "runner host hygiene volume usage evidence contained an incomplete row"
            )
        name, links, size = columns[0], columns[1], columns[2]
        if not links.isdigit():
            raise click.ClickException(
                "runner host hygiene volume usage evidence contained non-numeric links"
            )
        usage[name] = _VolumeUsage(
            links=int(links),
            size_bytes=_parse_docker_size_bytes(size),
        )
    if in_volume_section and not saw_volume_header:
        raise click.ClickException(
            "runner host hygiene volume usage evidence did not include a volume header"
        )
    return usage


def _parse_json_lines(output: str, *, evidence_name: str) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for line in output.splitlines():
        stripped_line = line.strip()
        if not stripped_line:
            continue
        try:
            payload = json.loads(stripped_line)
        except json.JSONDecodeError as error:
            raise click.ClickException(
                f"runner host hygiene {evidence_name} evidence was not valid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise click.ClickException(
                f"runner host hygiene {evidence_name} evidence contained a non-object row"
            )
        rows.append(payload)
    return tuple(rows)


def _docker_json_text(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    return _optional_scalar_text(value)


def _docker_labels(value: object) -> tuple[str, ...]:
    if not isinstance(value, dict):
        return ()
    labels = []
    for label_key, label_value in value.items():
        normalized_key = _optional_scalar_text(label_key)
        if not normalized_key:
            continue
        labels.append(f"{normalized_key}={_optional_scalar_text(label_value)}")
    return tuple(labels)


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return max(value, 0)
    try:
        parsed_value = int(_optional_scalar_text(value))
    except ValueError:
        return 0
    return max(parsed_value, 0)


def _optional_scalar_text(value: object) -> str:
    if value is None or isinstance(value, bool | dict | list | tuple):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, int | float | Decimal):
        return f"{value}".strip()
    return ""


def _parse_optional_docker_size_bytes(value: str) -> int:
    if not value.strip() or value.strip() in {"N/A", "-"}:
        return 0
    return _parse_docker_size_bytes(value)


def _image_reference(*, repository: str, tag: str) -> str:
    if _is_dangling_image(repository=repository, tag=tag):
        return ""
    return f"{repository}:{tag}"


def _is_warm_builder_image(*, repository: str, reference: str, retained_builders: set[str]) -> bool:
    return repository in retained_builders or reference in retained_builders


def _is_dangling_image(*, repository: str, tag: str) -> bool:
    return repository in {"", "<none>"} or tag in {"", "<none>"}


def _parse_docker_size_bytes(value: str) -> int:
    size = value.split("(", 1)[0].strip()
    if not size:
        raise click.ClickException("runner host hygiene Docker size was empty")
    numeric_text = "".join(
        character for character in size if character.isdigit() or character == "."
    )
    unit_text = size[len(numeric_text) :].strip().lower()
    if not numeric_text:
        raise click.ClickException("runner host hygiene Docker size was not numeric")
    try:
        numeric_value = Decimal(numeric_text)
    except InvalidOperation as error:
        raise click.ClickException("runner host hygiene Docker size was invalid") from error
    multiplier = _DOCKER_SIZE_UNITS.get(unit_text or "b")
    if multiplier is None:
        raise click.ClickException(
            f"runner host hygiene Docker size used unsupported unit: {unit_text}"
        )
    return int(numeric_value * multiplier)


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
    *,
    action: RunnerHostHygieneApplyAction,
    action_result: RemoteCommandResult,
    post_report: RunnerHostHygieneReport,
) -> str:
    action_label = (
        "BuildKit state volume removal"
        if action == "remove_buildkit_state_volumes"
        else "Docker cache prune"
    )
    if action_result.returncode != 0:
        detail = action_result.stderr.strip() or action_result.stdout.strip() or "unknown error"
        return f"runner host {action_label} failed: {detail}"
    if post_report.status != "healthy":
        return (
            f"runner host {action_label} completed but post evidence is not healthy: "
            f"{post_report.summary}"
        )
    return f"runner host {action_label} completed and post evidence is healthy"
