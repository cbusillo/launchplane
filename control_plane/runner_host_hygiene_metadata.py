from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote

import jwt
from pydantic import BaseModel, ConfigDict, Field

from control_plane import secrets
from control_plane.contracts.secret_record import SecretScope
from control_plane.runner_host_hygiene_configuration import (
    PRIVATE_KEY_BINDING,
    SERVICE_CONTEXT,
    RunnerHostHygieneConfiguration,
)
from control_plane.contracts.runner_host_hygiene_idle import RunnerHostHygieneIdleObservation
from control_plane.workflows.launchplane import github_api_request
from control_plane.workflows.runner_host_github_app import (
    read_github_app_installation_repositories,
)
from control_plane.workflows.runner_host_hygiene_executor import (
    build_github_idle_evidence_reader,
    build_github_run_state_reader,
)

READ_PERMISSIONS = {"actions": "read", "administration": "read", "metadata": "read"}


class RunnerHostHygieneMetadataRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configuration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_runner_name: str = Field(min_length=1, max_length=255)
    kind: Literal["idle", "run_states"]
    binding_index: int = Field(default=0, ge=0, le=255)
    cache_key: str = Field(default="", max_length=64)
    run_ids: tuple[int, ...] = Field(default=(), max_length=128)


class RunnerHostHygieneMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configuration_sha256: str
    observations: tuple[RunnerHostHygieneIdleObservation, ...] = ()
    run_states: dict[int, str] = Field(default_factory=dict)


def _secret_value(store: secrets.SecretReadStore) -> str:
    candidates = tuple(
        binding
        for binding in store.list_secret_bindings(
            integration=secrets.LAUNCHPLANE_SERVICE_SECRET_INTEGRATION,
            context_name=SERVICE_CONTEXT,
        )
        if binding.status == "configured"
        and binding.context == SERVICE_CONTEXT
        and not binding.instance
        and binding.binding_key == PRIVATE_KEY_BINDING
    )
    if len(candidates) != 1:
        raise ValueError("An exact service-only hygiene App key binding is required.")
    record = store.read_secret_record(candidates[0].secret_id)
    if (
        record.integration != secrets.LAUNCHPLANE_SERVICE_SECRET_INTEGRATION
        or record.scope != "context"
        or record.context != SERVICE_CONTEXT
        or record.instance
        or record.status != "configured"
    ):
        raise ValueError("The hygiene App key must be service-only and context isolated.")
    scopes = frozenset[SecretScope]({"context"})
    values = secrets.resolve_scoped_secret_values_for_integration_from_store(
        record_store=store,
        integration=secrets.LAUNCHPLANE_SERVICE_SECRET_INTEGRATION,
        context_name=SERVICE_CONTEXT,
        scopes=scopes,
        binding_keys=frozenset({PRIVATE_KEY_BINDING}),
    )
    value = values.get(PRIVATE_KEY_BINDING, ("", "context"))[0]
    if not value.strip():
        raise ValueError("The service-only hygiene App key is unavailable.")
    return value


@contextmanager
def _read_token(
    configuration: RunnerHostHygieneConfiguration,
    store: secrets.SecretReadStore,
) -> Iterator[str]:
    now = datetime.now(timezone.utc)
    app_jwt = jwt.encode(
        {
            "iat": int((now - timedelta(seconds=60)).timestamp()),
            "exp": int((now + timedelta(minutes=8)).timestamp()),
            "iss": configuration.github_app_client_id,
        },
        _secret_value(store),
        algorithm="RS256",
    )
    app = github_api_request(path="/app", token=app_jwt)
    if not isinstance(app, dict) or app.get("client_id") != configuration.github_app_client_id:
        raise ValueError("Hygiene App identity does not match its service record.")
    owner, repository = configuration.repository_scope.split("/", 1)
    installation = github_api_request(
        path=f"/repos/{quote(owner, safe='')}/{quote(repository, safe='')}/installation",
        token=app_jwt,
    )
    if (
        not isinstance(installation, dict)
        or installation.get("app_id") != app.get("id")
        or type(installation.get("id")) is not int
        or installation["id"] <= 0
        or not isinstance(installation.get("account"), dict)
        or str(installation["account"].get("login", "")).casefold() != owner.casefold()
        or installation.get("permissions") != READ_PERMISSIONS
    ):
        raise ValueError("Hygiene App installation must retain its exact read-only scope.")
    issued = github_api_request(
        method="POST",
        path=f"/app/installations/{installation['id']}/access_tokens",
        token=app_jwt,
        body={"permissions": READ_PERMISSIONS},
    )
    if (
        not isinstance(issued, dict)
        or not isinstance(issued.get("token"), str)
        or not issued["token"]
    ):
        raise ValueError("Hygiene App token issuance failed.")
    token = issued["token"]
    try:
        expires_at = datetime.fromisoformat(
            str(issued.get("expires_at", "")).replace("Z", "+00:00")
        )
        if (
            expires_at.tzinfo is None
            or expires_at <= datetime.now(timezone.utc)
            or issued.get("permissions") != READ_PERMISSIONS
        ):
            raise ValueError("Hygiene App token must be fresh and read-only.")
        expected = frozenset(
            binding.repository_scope.lower() for binding in configuration.github_idle_bindings
        ) | frozenset(
            root.source_repository.lower() for root in configuration.generated_run_cache_roots
        )
        if (
            read_github_app_installation_repositories(
                api_url="https://api.github.com",
                bearer_token=token,
            )
            != expected
        ):
            raise ValueError(
                "Hygiene App installation differs from the complete configured repository set."
            )
        yield token
    finally:
        github_api_request(method="DELETE", path="/installation/token", token=token)


def read_runner_host_hygiene_metadata(
    *,
    configuration: RunnerHostHygieneConfiguration,
    store: secrets.SecretReadStore,
    request: RunnerHostHygieneMetadataRequest,
    current_run_id: int,
) -> RunnerHostHygieneMetadata:
    if not any(
        binding.repository_scope == configuration.repository_scope.lower()
        and binding.execution_lane == configuration.execution_lane.lower()
        and binding.runner_name == request.current_runner_name
        for binding in configuration.github_idle_bindings
    ):
        raise ValueError("The current runner is not configured for this scheduler.")
    if request.kind == "idle":
        if (
            request.binding_index >= len(configuration.github_idle_bindings)
            or request.cache_key
            or request.run_ids
        ):
            raise ValueError("Idle metadata requires exactly one configured binding.")
    elif (
        request.binding_index
        or not request.run_ids
        or any(run_id <= 0 for run_id in request.run_ids)
        or request.cache_key not in {root.key for root in configuration.generated_run_cache_roots}
    ):
        raise ValueError("Run metadata requires a configured cache and bounded positive run IDs.")
    with _read_token(configuration, store) as token:
        if request.kind == "idle":
            binding = configuration.github_idle_bindings[request.binding_index]
            reader = build_github_idle_evidence_reader(
                bearer_token=token,
                current_run_id=current_run_id,
                current_runner_name=request.current_runner_name,
            )
            observations = reader(
                binding.repository_scope,
                binding.execution_lane,
                binding.runner_name,
                binding.repository_scope == configuration.repository_scope.lower()
                and binding.execution_lane == configuration.execution_lane.lower(),
            )
            return RunnerHostHygieneMetadata(
                configuration_sha256=request.configuration_sha256,
                observations=observations,
            )
        root = next(
            root
            for root in configuration.generated_run_cache_roots
            if root.key == request.cache_key
        )
        states: Mapping[int, str] = build_github_run_state_reader(bearer_token=token)(
            root.source_repository, request.run_ids
        )
        return RunnerHostHygieneMetadata(
            configuration_sha256=request.configuration_sha256,
            run_states=dict(states),
        )
