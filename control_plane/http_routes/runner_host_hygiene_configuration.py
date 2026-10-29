from typing import Annotated, Protocol, cast

from fastapi import Depends
import click
import jwt

from control_plane import secrets
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.runner_host_hygiene_configuration import (
    SERVICE_CONTEXT,
    RunnerHostHygieneDispatch,
    RunnerHostHygieneDispatchIntent,
    build_runner_host_hygiene_dispatch,
    load_runner_host_hygiene_configuration,
    require_current_hygiene_configuration,
)
from control_plane.runner_host_hygiene_metadata import (
    RunnerHostHygieneMetadata,
    RunnerHostHygieneMetadataRequest,
    read_runner_host_hygiene_metadata,
)
from control_plane.runtime_environments import RuntimeEnvironmentRecordStore
from control_plane.service_auth import GitHubActionsIdentity, LaunchplaneIdentity

DISPATCH_ROUTE = "/v1/service/runner-host-hygiene/dispatch"
METADATA_ROUTE = "/v1/service/runner-host-hygiene/metadata"


class HygieneStore(RuntimeEnvironmentRecordStore, secrets.SecretReadStore, Protocol):
    pass


def register_runner_host_hygiene_configuration_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
) -> None:
    def executor_identity(identity: LaunchplaneIdentity) -> GitHubActionsIdentity:
        if not isinstance(identity, GitHubActionsIdentity) or not dependencies.authorization_allows(
            identity=identity,
            action="runner_host_hygiene_audit.write",
            product=SERVICE_CONTEXT,
            context=SERVICE_CONTEXT,
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=dependencies.next_trace_id(),
                code="authorization_denied",
                message="Only the authorized hygiene workflow may read executor inputs.",
            )
        return identity

    def current_run_id(identity: GitHubActionsIdentity) -> int:
        value = identity.raw_claims.get("run_id", "")
        if not isinstance(value, str) or not value.isdecimal() or int(value) <= 0:
            raise ValueError("Authenticated hygiene run identity is required.")
        return int(value)

    def read_dispatch(
        intent: RunnerHostHygieneDispatchIntent,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> RunnerHostHygieneDispatch:
        workflow = executor_identity(identity)
        try:
            run_id = current_run_id(workflow)
            configuration, digest = load_runner_host_hygiene_configuration(
                cast(HygieneStore, record_store)
            )
            if workflow.repository.casefold() != configuration.repository_scope.casefold():
                raise ValueError("The scheduler repository does not match the configuration.")
            return build_runner_host_hygiene_dispatch(
                configuration=configuration,
                configuration_sha256=digest,
                intent=intent,
                current_run_id=run_id,
            )
        except (LookupError, ValueError, TypeError) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=dependencies.next_trace_id(),
                code="hygiene_configuration_unavailable",
                message="Hygiene dispatch requires matching service configuration and run identity.",
            ) from error

    def read_metadata(
        request: RunnerHostHygieneMetadataRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> RunnerHostHygieneMetadata:
        workflow = executor_identity(identity)
        try:
            configuration = require_current_hygiene_configuration(
                cast(HygieneStore, record_store),
                request.configuration_sha256,
            )
            if workflow.repository.casefold() != configuration.repository_scope.casefold():
                raise ValueError("The scheduler repository does not match the configuration.")
            return read_runner_host_hygiene_metadata(
                configuration=configuration,
                store=cast(HygieneStore, record_store),
                request=request,
                current_run_id=current_run_id(workflow),
            )
        except (
            LookupError,
            OSError,
            ValueError,
            TypeError,
            click.ClickException,
            jwt.PyJWTError,
        ) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=dependencies.next_trace_id(),
                code="hygiene_metadata_unavailable",
                message="Fresh, scoped hygiene metadata is unavailable.",
            ) from error

    app.add_api_route(
        DISPATCH_ROUTE,
        read_dispatch,
        methods=["POST"],
        response_model=RunnerHostHygieneDispatch,
        operation_id="read_runner_host_hygiene_dispatch",
    )
    app.add_api_route(
        METADATA_ROUTE,
        read_metadata,
        methods=["POST"],
        response_model=RunnerHostHygieneMetadata,
        operation_id="read_runner_host_hygiene_metadata",
    )
