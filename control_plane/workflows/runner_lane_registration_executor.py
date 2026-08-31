from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pydantic import BaseModel, ConfigDict, Field, model_validator
import json
import os
import pwd
import re
from shlex import quote
from time import sleep
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from control_plane.contracts.runner_lane_inventory import RunnerLaneInventory
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationAuditRecord
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationPolicy
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationRequest
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationTokenRecord
from control_plane.contracts.runner_lane_registration import plan_runner_lane_registration
from control_plane.github_payload import repository_full_name
from control_plane.runner_lane_github import GitHubRunnerLaneRegistrationTokenFetcher
from control_plane.workflows.runner_host_hygiene_executor import RemoteCommandResult


RemoteCommandRunner = Callable[[Sequence[str], int, Mapping[str, str]], RemoteCommandResult]
AuditPoster = Callable[[RunnerLaneRegistrationAuditRecord, str], dict[str, object]]
InventoryReader = Callable[[str], RunnerLaneInventory]
BearerTokenProvider = Callable[[], str]
AUDIT_ROUTE_PATH = "/v1/evidence/runner-lane-registration/audits"
POST_REGISTRATION_INVENTORY_ATTEMPTS = 6
POST_REGISTRATION_INVENTORY_INTERVAL_SECONDS = 5


class RunnerLaneRegistrationExecutorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository: str
    host_name: str
    execution_lane: str
    service_user: str
    lane_name: str
    registration_root: str
    runner_package_url: str = ""
    labels: tuple[str, ...]
    mutate: bool = False
    audit_record_key: str
    timeout_seconds: int = Field(default=120, ge=1)

    @model_validator(mode="after")
    def _normalize_request(self) -> "RunnerLaneRegistrationExecutorRequest":
        self.repository = repository_full_name(self.repository)
        self.host_name = _required_text(
            self.host_name, "runner lane registration executor requires host_name"
        ).lower()
        self.execution_lane = _required_text(
            self.execution_lane, "runner lane registration executor requires execution_lane"
        ).lower()
        self.service_user = _required_text(
            self.service_user, "runner lane registration executor requires service_user"
        )
        self.lane_name = _required_text(
            self.lane_name, "runner lane registration executor requires lane_name"
        ).lower()
        _validate_slug(
            self.host_name,
            "runner lane registration executor host_name must use letters, numbers, dots, underscores, or hyphens",
        )
        _validate_slug(
            self.execution_lane,
            "runner lane registration executor execution_lane must use letters, numbers, dots, underscores, or hyphens",
        )
        _validate_slug(
            self.lane_name,
            "runner lane registration executor lane_name must use letters, numbers, dots, underscores, or hyphens",
        )
        self.registration_root = _required_text(
            self.registration_root,
            "runner lane registration executor requires registration_root",
        )
        self.registration_root = _normalized_path(self.registration_root)
        self.runner_package_url = self.runner_package_url.strip()
        self.labels = tuple(
            sorted({label.strip().lower() for label in self.labels if label.strip()})
        )
        if not self.labels:
            raise ValueError("runner lane registration executor requires labels")
        self.audit_record_key = _required_text(
            self.audit_record_key,
            "runner lane registration executor requires audit_record_key",
        )
        return self


class RunnerLaneRegistrationExecutorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    audit_record_key: str
    planned_response: dict[str, object]
    terminal_response: dict[str, object] | None = None
    token_record: RunnerLaneRegistrationTokenRecord | None = None
    message: str


def execute_runner_lane_registration_executor(
    *,
    request: RunnerLaneRegistrationExecutorRequest,
    policy: RunnerLaneRegistrationPolicy,
    pre_inventory: RunnerLaneInventory,
    inventory_reader: InventoryReader,
    token_fetcher: GitHubRunnerLaneRegistrationTokenFetcher,
    remote_runner: RemoteCommandRunner,
    audit_poster: AuditPoster,
) -> RunnerLaneRegistrationExecutorResult:
    registration_request = RunnerLaneRegistrationRequest(
        repository=request.repository,
        host_name=request.host_name,
        lane_name=request.lane_name,
        registration_root=request.registration_root,
        labels=request.labels,
        mutate=request.mutate,
        audit_record_key=request.audit_record_key,
    )
    plan = plan_runner_lane_registration(
        policy=policy,
        request=registration_request,
        inventory=pre_inventory,
    )
    planned_audit = RunnerLaneRegistrationAuditRecord(
        audit_record_key=request.audit_record_key,
        status="planned",
        request=registration_request,
        plan=plan,
        pre_inventory=pre_inventory,
        message="planned runner lane registration; no runner mutation was executed yet",
    )
    planned_response = audit_poster(
        planned_audit,
        f"runner-lane-registration:{request.audit_record_key}:planned",
    )
    if plan.status != "ready":
        return RunnerLaneRegistrationExecutorResult(
            status="blocked",
            audit_record_key=request.audit_record_key,
            planned_response=planned_response,
            message=plan.summary,
        )

    token_record: RunnerLaneRegistrationTokenRecord | None = None
    post_inventory: RunnerLaneInventory | None = None
    try:
        _validate_runner_package_url_for_create(request)
        token, token_record = token_fetcher.fetch_registration_token(repository=request.repository)
        _prepare_runner_directory(request=request, remote_runner=remote_runner)
        _configure_runner(request=request, token=token, remote_runner=remote_runner)
        _start_supervised_runner(request=request, remote_runner=remote_runner)
        post_inventory = _read_verified_post_inventory(
            request=request,
            inventory_reader=inventory_reader,
        )
    except Exception as error:  # noqa: BLE001 - convert any adapter failure into audit evidence.
        failure_message = _failure_message(error)
        terminal_audit = RunnerLaneRegistrationAuditRecord(
            audit_record_key=request.audit_record_key,
            status="failed",
            request=registration_request,
            plan=plan,
            pre_inventory=pre_inventory,
            post_inventory=post_inventory,
            message=failure_message,
        )
        terminal_response = audit_poster(
            terminal_audit,
            f"runner-lane-registration:{request.audit_record_key}:failed",
        )
        return RunnerLaneRegistrationExecutorResult(
            status="failed",
            audit_record_key=request.audit_record_key,
            planned_response=planned_response,
            terminal_response=terminal_response,
            token_record=token_record,
            message=failure_message,
        )

    terminal_audit = RunnerLaneRegistrationAuditRecord(
        audit_record_key=request.audit_record_key,
        status="completed",
        request=registration_request,
        plan=plan,
        pre_inventory=pre_inventory,
        post_inventory=post_inventory,
        message="runner lane registration completed with supervised systemd service",
    )
    terminal_response = audit_poster(
        terminal_audit,
        f"runner-lane-registration:{request.audit_record_key}:completed",
    )
    return RunnerLaneRegistrationExecutorResult(
        status="completed",
        audit_record_key=request.audit_record_key,
        planned_response=planned_response,
        terminal_response=terminal_response,
        token_record=token_record,
        message=terminal_audit.message,
    )


def validate_local_executor_environment(
    *,
    request: RunnerLaneRegistrationExecutorRequest,
    env: Mapping[str, str] | None = None,
    current_user: str | None = None,
) -> None:
    execution_env = os.environ if env is None else env
    effective_user = current_user or pwd.getpwuid(os.getuid()).pw_name
    if effective_user != request.service_user:
        raise ValueError(f"runner lane registration executor must run as {request.service_user}.")
    github_repository = execution_env.get("GITHUB_REPOSITORY", "").strip().lower()
    if github_repository != "cbusillo/launchplane":
        raise ValueError("runner lane registration executor must run from cbusillo/launchplane.")
    runner_labels = {
        label.strip().lower()
        for label in execution_env.get("RUNNER_LABELS", "").split(",")
        if label.strip()
    }
    runner_name = execution_env.get("RUNNER_NAME", "").strip().lower()
    execution_lane = request.execution_lane.lower()
    if execution_lane not in runner_labels and runner_name != execution_lane:
        raise ValueError(
            "runner lane registration executor is not running on the approved execution lane."
        )


class RunnerLaneRegistrationCommandError(ValueError):
    pass


def _prepare_runner_directory(
    *,
    request: RunnerLaneRegistrationExecutorRequest,
    remote_runner: RemoteCommandRunner,
) -> None:
    runner_directory = _runner_directory(request)
    command = (
        "sh",
        "-eu",
        "-c",
        "\n".join(
            (
                f"runner_dir={quote(runner_directory)}",
                f"runner_package_url={quote(request.runner_package_url)}",
                f"registration_root={quote(request.registration_root)}",
                'mkdir -p "$registration_root"',
                'if [ -e "$runner_dir" ]; then',
                '  echo "runner directory already exists" >&2',
                "  exit 1",
                "fi",
                'mkdir "$runner_dir"',
                'temp_dir="$(mktemp -d)"',
                "trap 'rm -rf \"$temp_dir\"' EXIT",
                'curl --fail --silent --show-error --location "$runner_package_url" --output "$temp_dir/actions-runner.tar.gz"',
                'tar -xzf "$temp_dir/actions-runner.tar.gz" -C "$runner_dir"',
                'test -x "$runner_dir/config.sh"',
            )
        ),
    )
    _run_registration_command(
        step="prepare runner directory",
        command=command,
        request=request,
        remote_runner=remote_runner,
    )


def _configure_runner(
    *,
    request: RunnerLaneRegistrationExecutorRequest,
    token: str,
    remote_runner: RemoteCommandRunner,
) -> None:
    command = (
        "sh",
        "-eu",
        "-c",
        "\n".join(
            (
                'cd "$RUNNER_DIRECTORY"',
                './config.sh --unattended --url "$RUNNER_REPOSITORY_URL" --token "$RUNNER_REGISTRATION_TOKEN" --name "$RUNNER_NAME" --labels "$RUNNER_LABELS"',
            )
        ),
    )
    _run_registration_command(
        step="configure runner",
        command=command,
        request=request,
        remote_runner=remote_runner,
        extra_env={"RUNNER_REGISTRATION_TOKEN": token},
    )


def _start_supervised_runner(
    *,
    request: RunnerLaneRegistrationExecutorRequest,
    remote_runner: RemoteCommandRunner,
) -> None:
    unit_name = _systemd_unit_name(request)
    command = (
        "sudo",
        "-n",
        "systemctl",
        "enable",
        "--now",
        unit_name,
    )
    _run_registration_command(
        step="start supervised runner service",
        command=command,
        request=request,
        remote_runner=remote_runner,
    )
    _run_registration_command(
        step="verify supervised runner service",
        command=("sudo", "-n", "systemctl", "is-active", "--quiet", unit_name),
        request=request,
        remote_runner=remote_runner,
    )


def _verify_registered_lane(
    *,
    request: RunnerLaneRegistrationExecutorRequest,
    post_inventory: RunnerLaneInventory,
) -> None:
    matching_lanes = tuple(
        lane for lane in post_inventory.lanes if lane.name.strip().lower() == request.lane_name
    )
    if len(matching_lanes) != 1:
        raise ValueError(
            "runner lane registration post-inventory must include exactly one matching lane"
        )
    lane = matching_lanes[0]
    if lane.repository != request.repository:
        raise ValueError("runner lane registration post-inventory lane repository mismatch")
    if lane.status != "online":
        raise ValueError("runner lane registration post-inventory lane is not online")
    observed_labels = {label.strip().lower() for label in lane.labels if label.strip()}
    expected_labels = set(request.labels)
    missing_labels = sorted(expected_labels - observed_labels)
    if missing_labels:
        raise ValueError(
            "runner lane registration post-inventory lane is missing expected labels: "
            + ", ".join(missing_labels)
        )


def _read_verified_post_inventory(
    *,
    request: RunnerLaneRegistrationExecutorRequest,
    inventory_reader: InventoryReader,
) -> RunnerLaneInventory:
    latest_error: ValueError | None = None
    for attempt in range(POST_REGISTRATION_INVENTORY_ATTEMPTS):
        post_inventory = inventory_reader(request.repository)
        try:
            _verify_registered_lane(request=request, post_inventory=post_inventory)
            return post_inventory
        except ValueError as error:
            latest_error = error
            if attempt < POST_REGISTRATION_INVENTORY_ATTEMPTS - 1:
                sleep(POST_REGISTRATION_INVENTORY_INTERVAL_SECONDS)
    if latest_error is not None:
        raise latest_error
    raise ValueError("runner lane registration post-inventory could not be verified")


def _run_registration_command(
    *,
    step: str,
    command: Sequence[str],
    request: RunnerLaneRegistrationExecutorRequest,
    remote_runner: RemoteCommandRunner,
    extra_env: Mapping[str, str] | None = None,
) -> None:
    result = remote_runner(
        command,
        request.timeout_seconds,
        {
            "RUNNER_DIRECTORY": _runner_directory(request),
            "RUNNER_LABELS": ",".join(request.labels),
            "RUNNER_NAME": request.lane_name,
            "RUNNER_REPOSITORY_URL": f"https://github.com/{request.repository}",
            **(extra_env or {}),
        },
    )
    if result.returncode != 0:
        message = f"runner lane registration command failed during {step}: exit {result.returncode}"
        stderr = result.stderr.strip()
        if stderr:
            message = f"{message}: {_redacted_command_output(stderr)}"
        raise RunnerLaneRegistrationCommandError(message)


def _failure_message(error: Exception) -> str:
    message = str(error).strip()
    if not message:
        message = "runner lane registration failed"
    return message


def _runner_directory(request: RunnerLaneRegistrationExecutorRequest) -> str:
    return f"{request.registration_root}/{request.lane_name}"


def _systemd_unit_name(request: RunnerLaneRegistrationExecutorRequest) -> str:
    return f"launchplane-runner@{request.lane_name}.service"


def _validate_runner_package_url_for_create(
    request: RunnerLaneRegistrationExecutorRequest,
) -> None:
    assert request.mutate
    if not request.runner_package_url:
        raise ValueError(
            "runner lane registration executor requires runner_package_url when mutate=true"
        )
    if not _is_allowed_runner_package_url(request.runner_package_url):
        raise ValueError(
            "runner lane registration executor runner_package_url must be an actions/runner tarball URL"
        )


def _is_allowed_runner_package_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.netloc != "github.com":
        return False
    if parsed.params or parsed.query or parsed.fragment:
        return False
    if any(segment in {".", ".."} for segment in parsed.path.split("/")):
        return False
    pattern = re.compile(
        r"^/actions/runner/releases/download/v[0-9][0-9A-Za-z._-]*/"
        r"actions-runner-[A-Za-z0-9._-]+-[0-9][0-9A-Za-z._-]*\.tar\.gz$"
    )
    return bool(pattern.fullmatch(parsed.path))


def _redacted_command_output(value: str) -> str:
    return value.replace("\n", " ")[:500]


def _normalized_path(value: str) -> str:
    normalized = value.strip()
    if ".." in normalized.split("/"):
        raise ValueError(
            "runner lane registration executor registration_root must not contain "
            "parent-directory components"
        )
    normalized = normalized.rstrip("/")
    if not normalized.startswith("/"):
        raise ValueError("runner lane registration executor requires absolute registration_root")
    segments: list[str] = []
    for segment in normalized.split("/"):
        if segment in {"", "."}:
            continue
        segments.append(segment)
    if not segments:
        raise ValueError(
            "runner lane registration executor requires scoped absolute registration_root"
        )
    return "/" + "/".join(segments)


def build_local_command_runner() -> RemoteCommandRunner:
    from subprocess import run

    def _run(
        command: Sequence[str], timeout_seconds: int, env: Mapping[str, str]
    ) -> RemoteCommandResult:
        command_env = {**os.environ, **env}
        completed = run(
            tuple(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=command_env,
        )
        return RemoteCommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    return _run


def dry_run_audit_poster(
    audit: RunnerLaneRegistrationAuditRecord, idempotency_key: str
) -> dict[str, object]:
    return {
        "status": "accepted-local-dry-run",
        "idempotency_key": idempotency_key,
        "audit_record_key": audit.audit_record_key,
        "audit_status": audit.status,
    }


def build_service_audit_poster(*, service_url: str, bearer_token: str) -> AuditPoster:
    normalized_bearer_token = bearer_token.strip()
    if not normalized_bearer_token:
        raise ValueError("runner lane registration executor requires bearer token")
    return build_refreshing_service_audit_poster(
        service_url=service_url,
        bearer_token_provider=lambda: normalized_bearer_token,
    )


def build_refreshing_service_audit_poster(
    *, service_url: str, bearer_token_provider: BearerTokenProvider
) -> AuditPoster:
    normalized_service_url = service_url.strip().rstrip("/")
    if not normalized_service_url:
        raise ValueError("runner lane registration executor requires service_url")

    def post(audit: RunnerLaneRegistrationAuditRecord, idempotency_key: str) -> dict[str, object]:
        bearer_token = bearer_token_provider().strip()
        if not bearer_token:
            raise ValueError("runner lane registration executor requires bearer token")
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
            raise ValueError(
                response_text.strip() or f"Launchplane service returned HTTP {error.code}."
            ) from error
        if not isinstance(response_payload, dict):
            raise ValueError("Launchplane service returned a non-object response.")
        return response_payload

    return post


def _audit_route_payload(audit: RunnerLaneRegistrationAuditRecord) -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "launchplane",
        "audit": audit.model_dump(mode="json"),
    }


def _required_text(value: str, message: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(message)
    return normalized_value


def _validate_slug(value: str, message: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", value):
        raise ValueError(message)
