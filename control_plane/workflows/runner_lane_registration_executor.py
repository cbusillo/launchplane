from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pydantic import BaseModel, ConfigDict, Field, model_validator
import json
import os
import pwd
import re
from urllib.error import HTTPError
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
    del inventory_reader, token_fetcher, remote_runner
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

    terminal_audit = RunnerLaneRegistrationAuditRecord(
        audit_record_key=request.audit_record_key,
        status="failed",
        request=registration_request,
        plan=plan,
        pre_inventory=pre_inventory,
        message=(
            "runner lane registration requires a supervised host maintainer; "
            "starting run.sh from an Actions job is disabled"
        ),
    )
    terminal_response = audit_poster(
        terminal_audit,
        f"runner-lane-registration:{request.audit_record_key}:supervisor-required",
    )
    return RunnerLaneRegistrationExecutorResult(
        status="failed",
        audit_record_key=request.audit_record_key,
        planned_response=planned_response,
        terminal_response=terminal_response,
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
    if request.execution_lane.lower() not in runner_labels:
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


def _required_text(value: str, message: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(message)
    return normalized_value


def _validate_slug(value: str, message: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", value):
        raise ValueError(message)
