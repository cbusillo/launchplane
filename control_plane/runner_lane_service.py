from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.client import HTTPException
import logging
from typing import Literal, Protocol

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import SQLAlchemyError

from control_plane import secrets
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.github_app_configuration import (
    RUNNER_LIFECYCLE_GITHUB_APP_CLIENT_ID_KEY,
    RUNNER_LIFECYCLE_GITHUB_APP_ID_KEY,
    RUNNER_LIFECYCLE_GITHUB_APP_INTEGRATION,
    validate_app_id,
    validate_onboarding_app_client_id,
    service_github_app_values,
)
from control_plane.github_app_identity import (
    GitHubAppIdentity,
    GitHubAppInstallationToken,
    mint_runner_lifecycle_installation_token,
    revoke_installation_token,
)
from control_plane.github_payload import repository_full_name
from control_plane.merge_train_github import UrllibMergeTrainGitHubTransport
from control_plane.product_repository_identity import current_tracked_inventory_record
from control_plane.runner_lane_github import (
    GitHubRepositoryActiveRunReader,
    GitHubRunnerLaneInventoryReader,
    GitHubRunnerLaneRegistrationTokenFetcher,
    GitHubRunnerLaneRetirer,
)
from control_plane.runtime_environments import (
    RuntimeEnvironmentRecordStore,
    build_runtime_environment_definition_from_records,
)

SERVICE_CONTEXT = "launchplane"
RUNNER_LANE_SERVICE_ROUTE = "/v1/service/runner-lanes/operation"
_LOGGER = logging.getLogger(__name__)
CredentialStage = Literal["identity", "repository_inventory", "token_mint"]


class RunnerLifecycleCredentialsUnavailable(ValueError):
    def __init__(self, stage: CredentialStage) -> None:
        self.stage = stage
        super().__init__(f"Runner lifecycle credentials are unavailable at {stage}.")


@dataclass
class RunnerLifecycleCredential:
    token: GitHubAppInstallationToken = field(repr=False)
    revoked: bool = False


class RunnerLaneStore(secrets.SecretReadStore, RuntimeEnvironmentRecordStore, Protocol):
    def list_repository_inventory_records(self) -> tuple[RepositoryInventoryRecord, ...]: ...


class RunnerLaneServiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    operation: Literal["inventory", "active_runs", "registration_token", "retire"]
    runner_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_target(self) -> "RunnerLaneServiceRequest":
        self.repository = repository_full_name(self.repository)
        if (self.operation == "retire") != (self.runner_id is not None):
            raise ValueError("Only retirement requires an exact runner id.")
        return self


class RunnerLaneServiceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    operation: str
    app_id: int
    installation_id: int
    repository_id: int
    expires_at: str
    credential_revoked: bool = True
    trace_id: str = ""
    payload: dict[str, object] = Field(repr=False)


def runner_lifecycle_identity(store: RunnerLaneStore) -> GitHubAppIdentity:
    records = tuple(
        record
        for record in store.list_runtime_environment_records(context_name=SERVICE_CONTEXT)
        if record.scope == "context" and record.context == SERVICE_CONTEXT and not record.instance
    )
    values = service_github_app_values(build_runtime_environment_definition_from_records(records))
    app_id = validate_app_id(values.get(RUNNER_LIFECYCLE_GITHUB_APP_ID_KEY, ""))
    client_id = validate_onboarding_app_client_id(
        values.get(RUNNER_LIFECYCLE_GITHUB_APP_CLIENT_ID_KEY, "")
    )
    private_key = secrets.resolve_context_secret_value_from_store(
        record_store=store,
        integration=RUNNER_LIFECYCLE_GITHUB_APP_INTEGRATION,
        context_name=SERVICE_CONTEXT,
        binding_key="private_key",
    )
    if not private_key:
        raise ValueError("Runner lifecycle App key is unavailable.")
    return GitHubAppIdentity(app_id=int(app_id), client_id=client_id, private_key=private_key)


@contextmanager
def runner_lifecycle_token(
    store: RunnerLaneStore, repository: str
) -> Iterator[RunnerLifecycleCredential]:
    stage: CredentialStage = "repository_inventory"
    try:
        inventory = current_tracked_inventory_record(
            repository=repository.lower(),
            inventory_records=store.list_repository_inventory_records(),
        )
        stage = "identity"
        identity = runner_lifecycle_identity(store)
        stage = "token_mint"
        token = mint_runner_lifecycle_installation_token(
            identity=identity,
            repository=repository,
            repository_id=inventory.repository_id,
            repository_owner_id=inventory.repository_owner_id,
        )
    except (
        LookupError,
        OSError,
        TypeError,
        ValueError,
        click.ClickException,
        SQLAlchemyError,
    ) as error:
        raise RunnerLifecycleCredentialsUnavailable(stage) from error
    credential = RunnerLifecycleCredential(token)
    try:
        yield credential
    finally:
        try:
            revoke_installation_token(installation_token=token)
            credential.revoked = True
        except (ValueError, OSError, HTTPException, click.ClickException):
            _LOGGER.warning(
                "Runner lifecycle installation token revocation is unconfirmed; expiry remains active."
            )


def execute_runner_lane_service_request(
    store: RunnerLaneStore, request: RunnerLaneServiceRequest
) -> RunnerLaneServiceResponse:
    with runner_lifecycle_token(store, request.repository) as credential:
        token = credential.token
        transport = UrllibMergeTrainGitHubTransport(token=token.token)
        payload: dict[str, object]
        if request.operation == "inventory":
            inventory = GitHubRunnerLaneInventoryReader(
                transport=transport
            ).read_runner_lane_inventory(repository=request.repository)
            payload = inventory.model_dump(mode="json")
        elif request.operation == "active_runs":
            payload = {
                "run_ids": GitHubRepositoryActiveRunReader(transport=transport).read_active_run_ids(
                    repository=request.repository
                )
            }
        elif request.operation == "registration_token":
            registration_token, record = GitHubRunnerLaneRegistrationTokenFetcher(
                transport=transport
            ).fetch_registration_token(repository=request.repository)
            payload = {"token": registration_token, "record": record.model_dump(mode="json")}
        else:
            if request.runner_id is None:
                raise ValueError("Retirement requires a runner id.")
            GitHubRunnerLaneRetirer(transport=transport).delete_runner(
                repository=request.repository, runner_id=request.runner_id
            )
            payload = {}
        response = RunnerLaneServiceResponse(
            repository=request.repository,
            operation=request.operation,
            app_id=token.app_id,
            installation_id=token.installation_id,
            repository_id=token.repository_id,
            expires_at=token.expires_at,
            payload=payload,
        )
    response.credential_revoked = credential.revoked
    return response
