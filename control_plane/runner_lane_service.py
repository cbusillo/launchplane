from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.client import HTTPException
import logging
from pathlib import Path
import re
from typing import Literal, Protocol

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import SQLAlchemyError

from control_plane import secrets
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.outbox_delivery import (
    OutboxDeliveryRecord,
    build_outbox_delivery_id,
    build_outbox_dedupe_key,
)
from control_plane.durable_operation_authorization import read_active_authz_policy_record
from control_plane.service_auth import authz_selector_matches
from control_plane.storage.postgres import OutboxWithIdempotencyRequest
from control_plane.runner_queue_wait_github import GitHubRunnerQueueWaitReader
from control_plane.contracts.runner_queue_wait import RunnerQueueWaitJob
from control_plane.timestamps import utc_now_timestamp
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
from control_plane.build_provenance import BuildProvenanceError, GitHubBuildProvenanceTransport
from control_plane.workflows.runner_lane_ephemeral import (
    ADDON_RUNNER_LABEL_PREFIX,
    AddonRunnerRequest,
    read_addon_job,
)
from control_plane.workflows.launchplane import (
    github_api_request,
    launchplane_github_token,
    resolve_launchplane_github_token,
)
from control_plane.runtime_environments import (
    RuntimeEnvironmentRecordStore,
    build_runtime_environment_definition_from_records,
)

SERVICE_CONTEXT = "launchplane"
RUNNER_LANE_SERVICE_ROUTE = "/v1/service/runner-lanes/operation"
RUNNER_LANE_LIFECYCLE_WORKFLOW_PATH = ".github/workflows/runner-lane-registration.yml"
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


class AddonRunnerDispatchStore(RunnerLaneStore, Protocol):
    def list_product_profile_records(self) -> tuple[LaunchplaneProductProfileRecord, ...]: ...

    def write_outbox_delivery_record(self, record: OutboxDeliveryRecord) -> None: ...
    def read_outbox_delivery_record(self, delivery_id: str) -> OutboxDeliveryRecord: ...
    def enqueue_outbox_delivery_with_idempotency(
        self, request: OutboxWithIdempotencyRequest
    ) -> OutboxDeliveryRecord: ...


def runner_lifecycle_workflow_source(store: object) -> tuple[str, str]:
    record = read_active_authz_policy_record(store)
    sources: set[tuple[str, str]] = set()
    for rule in record.policy.github_actions:
        if "runner_lane_registration_audit.write" not in rule.actions or not rule.allows_scope(
            action="runner_lane_registration_audit.write",
            product=SERVICE_CONTEXT,
            context=SERVICE_CONTEXT,
            target=None,
            schema_version=record.policy.schema_version,
        ):
            continue
        if (rule.event_names and "workflow_dispatch" not in rule.event_names) or rule.environments:
            continue
        prefix = f"{rule.repository}/{RUNNER_LANE_LIFECYCLE_WORKFLOW_PATH}@"
        for workflow in rule.workflow_refs:
            ref = workflow.removeprefix(prefix)
            if workflow == ref or not re.fullmatch(r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*", ref):
                continue
            if rule.refs and ref not in rule.refs:
                continue
            if rule.job_workflow_refs and not authz_selector_matches(
                workflow, rule.job_workflow_refs
            ):
                continue
            sources.add((rule.repository, ref))
    if len(sources) != 1:
        raise LookupError("One existing exact runner lifecycle dispatch binding is required.")
    return sources.pop()


def request_addon_runner_containers(
    store: AddonRunnerDispatchStore, *, control_plane_root: Path
) -> tuple[str, ...]:
    source_repository, source_ref = runner_lifecycle_workflow_source(store)
    repositories = {
        profile.repository for profile in store.list_product_profile_records() if profile.is_active
    }
    dispatched: list[str] = []
    for repository in sorted(repositories):
        try:
            dispatched.extend(
                _request_repository_addon_runners(
                    store, repository, source_repository, source_ref, control_plane_root
                )
            )
        except (
            LookupError,
            OSError,
            ValueError,
            RuntimeError,
            click.ClickException,
            SQLAlchemyError,
        ) as error:
            _LOGGER.warning("Addon runner request scan unavailable (%s).", type(error).__name__)
    return tuple(dispatched)


def _request_repository_addon_runners(
    store: AddonRunnerDispatchStore,
    repository: str,
    source_repository: str,
    source_ref: str,
    control_plane_root: Path,
) -> tuple[str, ...]:
    dispatched: list[str] = []
    with runner_lifecycle_token(store, repository) as credential:
        token = credential.token
        reader = GitHubRunnerQueueWaitReader(
            transport=UrllibMergeTrainGitHubTransport(token=token.token)
        )
        for status in ("queued", "in_progress"):
            queue = reader.read_runner_queue_wait(repository=repository, workflow_status=status)
            for job in queue.jobs:
                try:
                    delivery_id = _request_addon_runner(
                        store, token, job, source_repository, source_ref, control_plane_root
                    )
                    if delivery_id:
                        dispatched.append(delivery_id)
                except (
                    BuildProvenanceError,
                    OSError,
                    ValueError,
                    RuntimeError,
                    click.ClickException,
                    SQLAlchemyError,
                ) as error:
                    _LOGGER.warning(
                        "Addon runner job request unavailable (%s).", type(error).__name__
                    )
    return tuple(dispatched)


def _request_addon_runner(
    store: AddonRunnerDispatchStore,
    token: GitHubAppInstallationToken,
    job: RunnerQueueWaitJob,
    source_repository: str,
    source_ref: str,
    control_plane_root: Path,
) -> str:
    attempts = [
        match[1]
        for label in job.labels
        if (match := re.fullmatch(rf"{ADDON_RUNNER_LABEL_PREFIX}{job.run_id}-([1-9][0-9]*)", label))
    ]
    if len(attempts) != 1:
        return ""
    request = AddonRunnerRequest(
        repository=token.repository,
        run_id=job.run_id,
        run_attempt=int(attempts[0]),
        job_id=job.github_id,
    )
    delivery = _addon_runner_delivery(request, source_repository, source_ref)
    try:
        existing = store.read_outbox_delivery_record(delivery.delivery_id)
        observe_addon_launcher(store, existing, control_plane_root=control_plane_root)
        return ""
    except FileNotFoundError:
        pass
    if job.status != "queued":
        return ""
    observed = read_addon_job(
        transport=GitHubBuildProvenanceTransport(token=token),
        request=request,
        repository_id=token.repository_id,
        include_recipe=False,
    )
    if observed.get("status") != "queued":
        return ""
    stored = store.enqueue_outbox_delivery_with_idempotency(
        OutboxWithIdempotencyRequest(delivery=delivery)
    )
    return stored.delivery_id


def observe_addon_launcher(
    store: AddonRunnerDispatchStore,
    record: OutboxDeliveryRecord,
    *,
    control_plane_root: Path,
) -> None:
    if record.state not in {"delivered", "failed"} or not record.external_id.isdecimal():
        return
    repository = repository_full_name(str(record.payload["repository"]))
    expected_title = record.payload["expected_run_title"]
    with launchplane_github_token(
        token_resolver=resolve_launchplane_github_token,
        api_request=github_api_request,
        control_plane_root=control_plane_root,
        context_name=SERVICE_CONTEXT,
        repository=repository,
        purpose="workflow_dispatch",
        retry_provider_errors=True,
    ) as token:
        if not token:
            raise ValueError("Original launcher observation credentials are unavailable.")
        run = github_api_request(
            path=f"/repos/{repository}/actions/runs/{record.external_id}",
            token=token,
            method="GET",
        )
    if (
        not isinstance(run, dict)
        or run.get("id") != int(record.external_id)
        or run.get("event") != "workflow_dispatch"
        or run.get("display_title") != expected_title
        or run.get("path") != RUNNER_LANE_LIFECYCLE_WORKFLOW_PATH
        or run.get("head_branch") != record.payload["ref"]
    ):
        raise ValueError("Original addon launcher identity is unconfirmed.")
    if run.get("status") != "completed":
        return
    success = run.get("conclusion") == "success"
    state = "delivered" if success else "failed"
    error_code = "" if success else "ephemeral_addon_launcher_failed"
    if record.state == state and record.error_code == error_code:
        return
    store.write_outbox_delivery_record(
        record.model_copy(
            update={
                "state": state,
                "action": "addon_launcher_completed" if success else "addon_launcher_failed",
                "error_code": error_code,
                "updated_at": utc_now_timestamp(),
                "payload": {
                    **record.payload,
                    "run_status": "completed",
                    "run_conclusion": run.get("conclusion"),
                },
            }
        )
    )
    if not success:
        _LOGGER.warning(
            "Addon launcher failed; reconcile original outbox delivery %s.", record.delivery_id
        )


def _addon_runner_delivery(
    request: AddonRunnerRequest, source_repository: str, source_ref: str
) -> OutboxDeliveryRecord:
    key = build_outbox_dedupe_key(
        kind="github_workflow_dispatch",
        parts=("ephemeral-addon", request.repository, str(request.job_id)),
    )
    now = utc_now_timestamp()
    return OutboxDeliveryRecord(
        delivery_id=build_outbox_delivery_id(kind="github_workflow_dispatch", dedupe_key=key),
        kind="github_workflow_dispatch",
        aggregate_type="ephemeral_addon_job",
        aggregate_id=f"{request.repository}:{request.job_id}",
        dedupe_key=key,
        created_at=now,
        updated_at=now,
        next_attempt_at=now,
        payload={
            "repository": source_repository,
            "workflow_id": RUNNER_LANE_LIFECYCLE_WORKFLOW_PATH.rsplit("/", 1)[-1],
            "ref": source_ref.removeprefix("refs/heads/"),
            "credential_context": SERVICE_CONTEXT,
            "observe_timeout_seconds": 12,
            "expected_run_title": f"Ephemeral addon {request.job_id}",
            "inputs": {
                "operation": "ephemeral",
                "repository": request.repository,
                "lane_name": request.name,
                "audit_record_key": f"ephemeral-addon/{request.job_id}",
                "mutate": "true",
                "confirmation": "run ephemeral addon job",
                "run_id": str(request.run_id),
                "run_attempt": str(request.run_attempt),
                "job_id": str(request.job_id),
            },
        },
    )


class RunnerLaneServiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    operation: Literal["inventory", "active_runs", "registration_token", "retire", "addon_job"]
    runner_id: int | None = Field(default=None, gt=0)
    run_id: int | None = Field(default=None, gt=0)
    run_attempt: int | None = Field(default=None, gt=0)
    job_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_target(self) -> "RunnerLaneServiceRequest":
        self.repository = repository_full_name(self.repository)
        if (self.operation == "retire") != (self.runner_id is not None):
            raise ValueError("Only retirement requires an exact runner id.")
        job_fields = (self.run_id, self.run_attempt, self.job_id)
        if self.operation == "addon_job":
            if any(value is None for value in job_fields):
                raise ValueError("Addon job reads require exact run, attempt and job ids.")
        elif any(value is not None for value in job_fields):
            raise ValueError("Only addon job reads accept job identities.")
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
        elif request.operation == "addon_job":
            assert (
                request.run_id is not None
                and request.run_attempt is not None
                and request.job_id is not None
            )
            payload = read_addon_job(
                transport=GitHubBuildProvenanceTransport(token=token),
                request=AddonRunnerRequest(
                    repository=request.repository,
                    run_id=request.run_id,
                    run_attempt=request.run_attempt,
                    job_id=request.job_id,
                ),
                repository_id=token.repository_id,
            )
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
