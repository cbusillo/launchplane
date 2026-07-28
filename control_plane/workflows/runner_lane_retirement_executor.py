from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import os
import posixpath
import pwd
import re
from time import sleep

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.runner_lane_inventory import RunnerLaneInventory
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationAuditRecord
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationPolicy
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationRequest
from control_plane.contracts.runner_lane_registration import plan_runner_lane_retirement
from control_plane.github_payload import repository_full_name
from control_plane.workflows.runner_lane_registration_executor import AuditPoster
from control_plane.workflows.runner_lane_registration_executor import RemoteCommandRunner


ActiveRunReader = Callable[[str], tuple[int, ...]]
RunnerRetirer = Callable[[str, int], None]
InventoryReader = Callable[[str], RunnerLaneInventory]
POST_RETIREMENT_INVENTORY_ATTEMPTS = 6
POST_RETIREMENT_INVENTORY_INTERVAL_SECONDS = 5
RUNNER_SERVICE_RETIRE_HELPER = "/usr/local/sbin/launchplane-runner-service-retire"


class RunnerLaneRetirementExecutorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository: str
    host_name: str
    execution_lane: str
    service_user: str
    lane_name: str
    registration_root: str
    mutate: bool = False
    audit_record_key: str
    timeout_seconds: int = Field(default=120, ge=1)

    @model_validator(mode="after")
    def _normalize_request(self) -> "RunnerLaneRetirementExecutorRequest":
        self.repository = repository_full_name(self.repository)
        self.host_name = _required_text(
            self.host_name, "runner lane retirement executor requires host_name"
        ).lower()
        self.execution_lane = _required_text(
            self.execution_lane, "runner lane retirement executor requires execution_lane"
        ).lower()
        self.service_user = _required_text(
            self.service_user, "runner lane retirement executor requires service_user"
        )
        self.lane_name = _required_text(
            self.lane_name, "runner lane retirement executor requires lane_name"
        ).lower()
        _validate_slug(
            self.host_name,
            "runner lane retirement executor host_name must use letters, numbers, dots, underscores, or hyphens",
        )
        _validate_slug(
            self.execution_lane,
            "runner lane retirement executor execution_lane must use letters, numbers, dots, underscores, or hyphens",
        )
        _validate_slug(
            self.lane_name,
            "runner lane retirement executor lane_name must use letters, numbers, dots, underscores, or hyphens",
        )
        self.registration_root = _normalized_path(
            _required_text(
                self.registration_root,
                "runner lane retirement executor requires registration_root",
            )
        )
        self.audit_record_key = _required_text(
            self.audit_record_key,
            "runner lane retirement executor requires audit_record_key",
        )
        return self


class RunnerLaneRetirementExecutorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    audit_record_key: str
    planned_response: dict[str, object]
    terminal_response: dict[str, object] | None = None
    terminal_audit: RunnerLaneRegistrationAuditRecord | None = None
    message: str


def execute_runner_lane_retirement_executor(
    *,
    request: RunnerLaneRetirementExecutorRequest,
    policy: RunnerLaneRegistrationPolicy,
    pre_inventory: RunnerLaneInventory,
    inventory_reader: InventoryReader,
    active_run_reader: ActiveRunReader,
    runner_retirer: RunnerRetirer,
    remote_runner: RemoteCommandRunner,
    audit_poster: AuditPoster,
) -> RunnerLaneRetirementExecutorResult:
    active_run_ids = active_run_reader(request.repository)
    target_worker_active = _target_process_active(
        request=request,
        remote_runner=remote_runner,
        process_names=("Runner.Worker",),
    )
    retirement_request = RunnerLaneRegistrationRequest(
        operation="retire",
        repository=request.repository,
        host_name=request.host_name,
        lane_name=request.lane_name,
        registration_root=request.registration_root,
        labels=(),
        active_run_ids=active_run_ids,
        target_worker_active=target_worker_active,
        mutate=request.mutate,
        audit_record_key=request.audit_record_key,
    )
    plan = plan_runner_lane_retirement(
        policy=policy,
        request=retirement_request,
        inventory=pre_inventory,
    )
    planned_audit = RunnerLaneRegistrationAuditRecord(
        audit_record_key=request.audit_record_key,
        status="planned",
        request=retirement_request,
        plan=plan,
        pre_inventory=pre_inventory,
        message="planned runner lane retirement; no runner mutation was executed yet",
    )
    planned_response = audit_poster(
        planned_audit,
        f"runner-lane-retirement:{request.audit_record_key}:planned",
    )
    if plan.status != "ready":
        return RunnerLaneRetirementExecutorResult(
            status="blocked",
            audit_record_key=request.audit_record_key,
            planned_response=planned_response,
            message=plan.summary,
        )

    matching_lanes = tuple(
        lane for lane in pre_inventory.lanes if lane.name.strip().lower() == request.lane_name
    )
    runner_id = matching_lanes[0].github_id
    service_retired = False
    registration_deleted = False
    post_inventory: RunnerLaneInventory | None = None
    try:
        _verify_runner_directory(request=request, remote_runner=remote_runner)
        _retire_supervised_runner(request=request, remote_runner=remote_runner)
        service_retired = True
        _verify_target_processes_stopped(request=request, remote_runner=remote_runner)
        live_active_run_ids = active_run_reader(request.repository)
        post_inventory = inventory_reader(request.repository)
        _verify_pre_delete_state(
            request=request,
            policy=policy,
            expected_runner_id=runner_id,
            active_run_ids=live_active_run_ids,
            inventory=post_inventory,
        )
        runner_retirer(request.repository, runner_id)
        registration_deleted = True
        post_inventory = _read_verified_post_inventory(
            request=request,
            inventory_reader=inventory_reader,
        )
        _remove_runner_directory(request=request, remote_runner=remote_runner)
    except Exception as error:  # noqa: BLE001 - convert adapter failure into audit evidence.
        failure_message = _failure_message(error)
        if service_retired and not registration_deleted:
            rollback_error = _restore_supervised_runner(
                request=request,
                remote_runner=remote_runner,
            )
            if rollback_error:
                failure_message = f"{failure_message}; service rollback failed: {rollback_error}"
        terminal_audit = RunnerLaneRegistrationAuditRecord(
            audit_record_key=request.audit_record_key,
            status="failed",
            request=retirement_request,
            plan=plan,
            pre_inventory=pre_inventory,
            post_inventory=post_inventory,
            message=failure_message,
        )
        terminal_response, audit_delivery_pending = _post_terminal_audit(
            audit=terminal_audit,
            idempotency_key=f"runner-lane-retirement:{request.audit_record_key}:failed",
            audit_poster=audit_poster,
        )
        if audit_delivery_pending:
            failure_message = (
                f"{failure_message}; terminal audit delivery pending in workflow artifact"
            )
        return RunnerLaneRetirementExecutorResult(
            status="audit_delivery_pending" if audit_delivery_pending else "failed",
            audit_record_key=request.audit_record_key,
            planned_response=planned_response,
            terminal_response=terminal_response,
            terminal_audit=terminal_audit,
            message=failure_message,
        )

    terminal_audit = RunnerLaneRegistrationAuditRecord(
        audit_record_key=request.audit_record_key,
        status="completed",
        request=retirement_request,
        plan=plan,
        pre_inventory=pre_inventory,
        post_inventory=post_inventory,
        message="runner lane retirement completed with registration, service, and root removed",
    )
    terminal_response, audit_delivery_pending = _post_terminal_audit(
        audit=terminal_audit,
        idempotency_key=f"runner-lane-retirement:{request.audit_record_key}:completed",
        audit_poster=audit_poster,
    )
    message = terminal_audit.message
    if audit_delivery_pending:
        message = f"{message}; terminal audit delivery pending in workflow artifact"
    return RunnerLaneRetirementExecutorResult(
        status="audit_delivery_pending" if audit_delivery_pending else "completed",
        audit_record_key=request.audit_record_key,
        planned_response=planned_response,
        terminal_response=terminal_response,
        terminal_audit=terminal_audit,
        message=message,
    )


def validate_local_executor_environment(
    *,
    request: RunnerLaneRetirementExecutorRequest,
    env: Mapping[str, str] | None = None,
    current_user: str | None = None,
) -> None:
    execution_env = os.environ if env is None else env
    effective_user = current_user or pwd.getpwuid(os.getuid()).pw_name
    if effective_user != request.service_user:
        raise ValueError(f"runner lane retirement executor must run as {request.service_user}.")
    github_repository = execution_env.get("GITHUB_REPOSITORY", "").strip().lower()
    if github_repository != "cbusillo/launchplane":
        raise ValueError("runner lane retirement executor must run from cbusillo/launchplane.")
    runner_labels = {
        label.strip().lower()
        for label in execution_env.get("RUNNER_LABELS", "").split(",")
        if label.strip()
    }
    runner_name = execution_env.get("RUNNER_NAME", "").strip().lower()
    if request.execution_lane not in runner_labels and runner_name != request.execution_lane:
        raise ValueError(
            "runner lane retirement executor is not running on the approved execution lane."
        )


def _target_process_active(
    *,
    request: RunnerLaneRetirementExecutorRequest,
    remote_runner: RemoteCommandRunner,
    process_names: tuple[str, ...],
) -> bool:
    result = remote_runner(
        ("pgrep", "-af", "|".join(process_names)),
        request.timeout_seconds,
        {},
    )
    if result.returncode not in {0, 1}:
        raise ValueError("runner lane retirement could not inspect target processes")
    runner_directory = _runner_directory(request)
    runner_directory_pattern = re.compile(rf"{re.escape(runner_directory)}(?:/|\s|$)")
    return any(
        runner_directory_pattern.search(line) is not None
        and any(name in line for name in process_names)
        for line in result.stdout.splitlines()
    )


def _retire_supervised_runner(
    *,
    request: RunnerLaneRetirementExecutorRequest,
    remote_runner: RemoteCommandRunner,
) -> None:
    _run_retirement_command(
        step="stop and disable supervised runner service",
        command=(
            "sudo",
            "-n",
            RUNNER_SERVICE_RETIRE_HELPER,
            request.repository,
            request.lane_name,
            request.registration_root,
            request.service_user,
        ),
        request=request,
        remote_runner=remote_runner,
    )


def _verify_target_processes_stopped(
    *,
    request: RunnerLaneRetirementExecutorRequest,
    remote_runner: RemoteCommandRunner,
) -> None:
    if _target_process_active(
        request=request,
        remote_runner=remote_runner,
        process_names=("Runner.Listener", "Runner.Worker", "RunnerService.js"),
    ):
        raise ValueError("runner lane retirement left target runner processes active")


def _verify_runner_directory(
    *,
    request: RunnerLaneRetirementExecutorRequest,
    remote_runner: RemoteCommandRunner,
) -> None:
    script = "\n".join(
        (
            'runner_dir="$RUNNER_DIRECTORY"',
            'registration_root="$RUNNER_REGISTRATION_ROOT"',
            'test -d "$registration_root"',
            'test ! -L "$registration_root"',
            'test "$(realpath -e "$registration_root")" = "$registration_root"',
            'test "$(stat -c %U "$registration_root")" = "$RUNNER_SERVICE_USER"',
            'test -d "$runner_dir"',
            'test ! -L "$runner_dir"',
            'case "$runner_dir" in "$registration_root"/*) ;; *) exit 1 ;; esac',
            'test "$(realpath -e "$runner_dir")" = "$runner_dir"',
            'test "$(stat -c %U "$runner_dir")" = "$RUNNER_SERVICE_USER"',
        )
    )
    _run_retirement_command(
        step="verify runner directory identity",
        command=("sh", "-eu", "-c", script),
        request=request,
        remote_runner=remote_runner,
        extra_env={
            "RUNNER_DIRECTORY": _runner_directory(request),
            "RUNNER_REGISTRATION_ROOT": request.registration_root,
            "RUNNER_SERVICE_USER": request.service_user,
        },
    )


def _verify_pre_delete_state(
    *,
    request: RunnerLaneRetirementExecutorRequest,
    policy: RunnerLaneRegistrationPolicy,
    expected_runner_id: int,
    active_run_ids: tuple[int, ...],
    inventory: RunnerLaneInventory,
) -> None:
    if active_run_ids:
        raise ValueError(
            "runner lane retirement detected active repository runs after service stop: "
            + ", ".join(str(run_id) for run_id in active_run_ids)
        )
    if inventory.repository.strip().lower() != request.repository:
        raise ValueError("runner lane retirement live inventory repository mismatch")
    matching_lanes = tuple(
        lane for lane in inventory.lanes if lane.name.strip().lower() == request.lane_name
    )
    if len(matching_lanes) != 1:
        raise ValueError(
            "runner lane retirement live inventory must contain exactly one target lane"
        )
    lane = matching_lanes[0]
    if lane.github_id != expected_runner_id:
        raise ValueError("runner lane retirement target runner id changed before deletion")
    if lane.busy:
        raise ValueError("runner lane retirement target became busy before deletion")
    observed_labels = {label.strip().lower() for label in lane.labels if label.strip()}
    missing_labels = tuple(
        label for label in policy.required_labels if label not in observed_labels
    )
    if missing_labels:
        raise ValueError(
            "runner lane retirement target lost required managed labels before deletion: "
            + ", ".join(missing_labels)
        )


def _restore_supervised_runner(
    *,
    request: RunnerLaneRetirementExecutorRequest,
    remote_runner: RemoteCommandRunner,
) -> str:
    result = remote_runner(
        (
            "sudo",
            "-n",
            "systemctl",
            "enable",
            "--now",
            f"launchplane-runner@{request.lane_name}.service",
        ),
        request.timeout_seconds,
        {},
    )
    if result.returncode == 0:
        return ""
    return _redacted_command_output(result.stderr) or f"exit {result.returncode}"


def _read_verified_post_inventory(
    *,
    request: RunnerLaneRetirementExecutorRequest,
    inventory_reader: InventoryReader,
) -> RunnerLaneInventory:
    latest_error: Exception | None = None
    for attempt in range(POST_RETIREMENT_INVENTORY_ATTEMPTS):
        try:
            inventory = inventory_reader(request.repository)
            if not any(lane.name.strip().lower() == request.lane_name for lane in inventory.lanes):
                return inventory
            latest_error = ValueError(
                f"runner lane retirement post-inventory still contains lane: {request.lane_name}"
            )
        except Exception as error:  # noqa: BLE001 - retry bounded read failures.
            latest_error = error
        if attempt + 1 < POST_RETIREMENT_INVENTORY_ATTEMPTS:
            sleep(POST_RETIREMENT_INVENTORY_INTERVAL_SECONDS)
    if latest_error is not None:
        raise latest_error
    raise ValueError("runner lane retirement post-inventory could not be verified")


def _remove_runner_directory(
    *,
    request: RunnerLaneRetirementExecutorRequest,
    remote_runner: RemoteCommandRunner,
) -> None:
    script = "\n".join(
        (
            'runner_dir="$RUNNER_DIRECTORY"',
            'registration_root="$RUNNER_REGISTRATION_ROOT"',
            'test -d "$runner_dir"',
            'test ! -L "$runner_dir"',
            'case "$runner_dir" in "$registration_root"/*) ;; *) exit 1 ;; esac',
            'test "$(realpath -e "$registration_root")" = "$registration_root"',
            'test "$(realpath -e "$runner_dir")" = "$runner_dir"',
            'test "$(stat -c %U "$registration_root")" = "$RUNNER_SERVICE_USER"',
            'test "$(stat -c %U "$runner_dir")" = "$RUNNER_SERVICE_USER"',
            "command -v lsof >/dev/null 2>&1",
            "lsof_status=0",
            'lsof -t +D "$runner_dir" >/dev/null 2>&1 || lsof_status=$?',
            'if [ "$lsof_status" -eq 0 ] || [ "$lsof_status" -gt 1 ]; then exit 1; fi',
            'rm -rf --one-file-system -- "$runner_dir"',
            'test ! -e "$runner_dir"',
        )
    )
    _run_retirement_command(
        step="remove retired runner directory",
        command=("sh", "-eu", "-c", script),
        request=request,
        remote_runner=remote_runner,
        extra_env={
            "RUNNER_DIRECTORY": _runner_directory(request),
            "RUNNER_REGISTRATION_ROOT": request.registration_root,
            "RUNNER_SERVICE_USER": request.service_user,
        },
    )


def _run_retirement_command(
    *,
    step: str,
    command: Sequence[str],
    request: RunnerLaneRetirementExecutorRequest,
    remote_runner: RemoteCommandRunner,
    extra_env: Mapping[str, str] | None = None,
) -> None:
    result = remote_runner(command, request.timeout_seconds, dict(extra_env or {}))
    if result.returncode == 0:
        return
    message = f"runner lane retirement command failed during {step}: exit {result.returncode}"
    stderr = _redacted_command_output(result.stderr)
    if stderr:
        message = f"{message}: {stderr}"
    raise ValueError(message)


def _post_terminal_audit(
    *,
    audit: RunnerLaneRegistrationAuditRecord,
    idempotency_key: str,
    audit_poster: AuditPoster,
) -> tuple[dict[str, object] | None, bool]:
    try:
        return audit_poster(audit, idempotency_key), False
    except Exception:  # noqa: BLE001 - preserve terminal evidence in the workflow artifact.
        return None, True


def _failure_message(error: Exception) -> str:
    return str(error).strip() or "runner lane retirement failed"


def _runner_directory(request: RunnerLaneRetirementExecutorRequest) -> str:
    return f"{request.registration_root}/{request.lane_name}"


def _redacted_command_output(value: str) -> str:
    return value.replace("\n", " ")[:500]


def _normalized_path(value: str) -> str:
    normalized = posixpath.normpath(value.strip())
    if not normalized.startswith("/") or normalized == "/":
        raise ValueError("runner lane retirement executor requires scoped absolute root")
    return normalized


def _required_text(value: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(message)
    return normalized


def _validate_slug(value: str, message: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", value):
        raise ValueError(message)
