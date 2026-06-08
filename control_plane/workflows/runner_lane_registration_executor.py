from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pydantic import BaseModel, ConfigDict, Field, model_validator
import json
import os
import pwd
import shlex
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from control_plane.contracts.runner_lane_inventory import RunnerLaneInventory
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationAuditRecord
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationAuditStatus
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


class RunnerLaneRegistrationExecutorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository: str
    host_name: str
    execution_lane: str
    service_user: str
    lane_name: str
    registration_root: str
    labels: tuple[str, ...]
    mutate: bool = False
    audit_record_key: str
    timeout_seconds: int = Field(default=120, ge=1)

    @model_validator(mode="after")
    def _normalize_request(self) -> "RunnerLaneRegistrationExecutorRequest":
        self.repository = repository_full_name(self.repository)
        self.host_name = _required_text(
            self.host_name, "runner lane registration executor requires host_name"
        )
        self.execution_lane = _required_text(
            self.execution_lane, "runner lane registration executor requires execution_lane"
        )
        self.service_user = _required_text(
            self.service_user, "runner lane registration executor requires service_user"
        )
        self.lane_name = _required_text(
            self.lane_name, "runner lane registration executor requires lane_name"
        )
        self.registration_root = _required_text(
            self.registration_root,
            "runner lane registration executor requires registration_root",
        ).rstrip("/")
        if not self.registration_root.startswith("/"):
            raise ValueError(
                "runner lane registration executor requires absolute registration_root"
            )
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

    validate_local_executor_environment(request=request)
    token, token_record = token_fetcher.fetch_registration_token(repository=request.repository)
    register_result = remote_runner(
        _registration_command(request=request),
        request.timeout_seconds,
        {"RUNNER_REGISTRATION_TOKEN": token},
    )
    post_inventory = inventory_reader(request.repository)
    if register_result.returncode == 0 and _post_inventory_has_lane(
        inventory=post_inventory,
        lane_name=request.lane_name,
        labels=request.labels,
    ):
        status: RunnerLaneRegistrationAuditStatus = "completed"
        message = "runner lane registration completed and GitHub inventory verified"
    else:
        status = "failed"
        detail = register_result.stderr.strip() or register_result.stdout.strip() or plan.summary
        if register_result.returncode == 0:
            detail = "registered command completed but GitHub inventory did not show expected lane"
        message = f"runner lane registration failed: {detail}"
    terminal_audit = RunnerLaneRegistrationAuditRecord(
        audit_record_key=request.audit_record_key,
        status=status,
        request=registration_request,
        plan=plan,
        pre_inventory=pre_inventory,
        post_inventory=post_inventory,
        message=message,
    )
    terminal_response = audit_poster(
        terminal_audit,
        f"runner-lane-registration:{request.audit_record_key}:{status}",
    )
    return RunnerLaneRegistrationExecutorResult(
        status=status,
        audit_record_key=request.audit_record_key,
        planned_response=planned_response,
        terminal_response=terminal_response,
        token_record=token_record,
        message=message,
    )


def validate_local_executor_environment(*, request: RunnerLaneRegistrationExecutorRequest) -> None:
    current_user = pwd.getpwuid(os.getuid()).pw_name
    if current_user != request.service_user:
        raise ValueError(f"runner lane registration executor must run as {request.service_user}.")
    github_repository = os.environ.get("GITHUB_REPOSITORY", "").strip().lower()
    if github_repository and github_repository != "cbusillo/launchplane":
        raise ValueError("runner lane registration executor must run from cbusillo/launchplane.")
    runner_labels = {
        label.strip().lower()
        for label in os.environ.get("RUNNER_LABELS", "").split(",")
        if label.strip()
    }
    if runner_labels and request.execution_lane.lower() not in runner_labels:
        raise ValueError(
            "runner lane registration executor is not running on the approved execution lane."
        )


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


def _registration_command(*, request: RunnerLaneRegistrationExecutorRequest) -> tuple[str, ...]:
    runner_directory = f"{request.registration_root}/{request.lane_name}"
    quoted_runner_directory = shlex.quote(runner_directory)
    quoted_repository_url = shlex.quote(f"https://github.com/{request.repository}")
    quoted_lane_name = shlex.quote(request.lane_name)
    quoted_labels = shlex.quote(",".join(request.labels))
    return (
        "bash",
        "-lc",
        "set -euo pipefail\n"
        f"mkdir -p {quoted_runner_directory}\n"
        f"cd {quoted_runner_directory}\n"
        "if [ ! -x ./config.sh ]; then\n"
        '  runner_arch="$(uname -m)"\n'
        '  case "$runner_arch" in\n'
        "    x86_64|amd64) runner_arch=x64 ;;\n"
        "    aarch64|arm64) runner_arch=arm64 ;;\n"
        '    *) echo "unsupported runner architecture: $runner_arch" >&2; exit 1 ;;\n'
        "  esac\n"
        '  runner_version="${ACTIONS_RUNNER_VERSION:-}"\n'
        '  if [ -z "$runner_version" ]; then\n'
        "    runner_version=\"$(python3 - <<'PY'\n"
        "import json\n"
        "import urllib.request\n"
        "request = urllib.request.Request(\n"
        "    'https://api.github.com/repos/actions/runner/releases/latest',\n"
        "    headers={'Accept': 'application/vnd.github+json'},\n"
        ")\n"
        "with urllib.request.urlopen(request, timeout=30) as response:\n"
        "    payload = json.load(response)\n"
        "print(str(payload['tag_name']).removeprefix('v'))\n"
        "PY\n"
        '    )"\n'
        "  fi\n"
        '  asset="actions-runner-linux-${runner_arch}-${runner_version}.tar.gz"\n'
        '  url="https://github.com/actions/runner/releases/download/v${runner_version}/${asset}"\n'
        '  archive="$(mktemp)"\n'
        '  curl -fsSL "$url" -o "$archive"\n'
        '  tar -xzf "$archive"\n'
        '  rm -f "$archive"\n'
        "fi\n"
        "test -x ./config.sh\n"
        "./config.sh "
        f"--url {quoted_repository_url} "
        '--token "$RUNNER_REGISTRATION_TOKEN" '
        f"--name {quoted_lane_name} "
        f"--labels {quoted_labels} "
        "--unattended --replace\n"
        'if [ -f .runner.pid ] && kill -0 "$(cat .runner.pid)" 2>/dev/null; then\n'
        "  :\n"
        "else\n"
        "  nohup ./run.sh > runner.log 2>&1 &\n"
        "  echo $! > .runner.pid\n"
        "fi\n"
        "sleep 12",
    )


def _post_inventory_has_lane(
    *, inventory: RunnerLaneInventory, lane_name: str, labels: tuple[str, ...]
) -> bool:
    expected_labels = set(labels)
    for lane in inventory.lanes:
        if lane.name != lane_name or lane.status != "online":
            continue
        if expected_labels.issubset({label.strip().lower() for label in lane.labels}):
            return True
    return False


def _required_text(value: str, message: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(message)
    return normalized_value
