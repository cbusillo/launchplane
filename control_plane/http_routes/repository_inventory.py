from dataclasses import dataclass
from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import Depends, Header, Query
from pydantic import BaseModel, ConfigDict

from control_plane.contracts.repository_inventory import (
    REPOSITORY_INVENTORY_READ_ACTION,
    REPOSITORY_INVENTORY_WRITE_ACTION,
)
from control_plane.http_routes.support import (
    LAUNCHPLANE_SERVICE_CONTEXT,
    ApiRouteRegistrar,
    AuthorizationAllows,
    HttpErrorFactory,
    ReadRouteDependencies,
)
from control_plane.repository_inventory import (
    RepositoryInventoryApplyEnvelope,
    RepositoryInventoryApplyResult,
    RepositoryInventoryConflictError,
    RepositoryInventoryReadModel,
    RepositoryInventorySequenceError,
    apply_repository_inventory,
    get_repository_inventory_read_model,
    require_repository_inventory_read_store,
)
from control_plane.service_auth import LaunchplaneIdentity, TerminalAgentIdentity
from control_plane.storage.postgres import PostgresRecordStore

REPOSITORY_INVENTORY_READ_ROUTE = "/v1/repository-inventory"
REPOSITORY_INVENTORY_APPLY_ROUTE = "/v1/repository-inventory/apply"


@dataclass(frozen=True, slots=True)
class RepositoryInventoryWriteRouteDependencies:
    read_write_identity: Callable[..., LaunchplaneIdentity]
    get_record_store: Callable[[], object]
    next_trace_id: Callable[[], str]
    authorization_allows: AuthorizationAllows
    http_error: HttpErrorFactory
    error_response_model: type[BaseModel]


class RepositoryInventoryReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    trace_id: str
    read_model: RepositoryInventoryReadModel


class RepositoryInventoryApplyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    trace_id: str
    result: RepositoryInventoryApplyResult


def register_repository_inventory_read_routes(
    app: ApiRouteRegistrar, *, dependencies: ReadRouteDependencies
) -> None:
    def read_repository_inventory(
        repository_id: Annotated[str, Query(...)],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> RepositoryInventoryReadResponse:
        trace_id = dependencies.next_trace_id()
        if not dependencies.authorization_allows(
            identity=identity,
            action=REPOSITORY_INVENTORY_READ_ACTION,
            product=LAUNCHPLANE_SERVICE_CONTEXT,
            context=LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise dependencies.http_error(
                status_code=403, trace_id=trace_id, code="authorization_denied",
                message="Identity cannot read repository inventory records.",
            )
        try:
            read_model = get_repository_inventory_read_model(
                repository_id=repository_id,
                store=require_repository_inventory_read_store(record_store),
            )
        except (TypeError, ValueError) as error:
            raise dependencies.http_error(
                status_code=503 if isinstance(error, TypeError) else 400,
                trace_id=trace_id,
                code="database_storage_required" if isinstance(error, TypeError) else "invalid_request",
                message=str(error),
            ) from error
        return RepositoryInventoryReadResponse(trace_id=trace_id, read_model=read_model)

    app.add_api_route(
        REPOSITORY_INVENTORY_READ_ROUTE,
        read_repository_inventory,
        methods=["GET"],
        response_model=RepositoryInventoryReadResponse,
        operation_id="read_repository_inventory",
        summary="Read the current inert repository inventory record",
    )


def register_repository_inventory_write_routes(
    app: ApiRouteRegistrar, *, dependencies: RepositoryInventoryWriteRouteDependencies
) -> None:
    def apply_repository_inventory_route(
        envelope: RepositoryInventoryApplyEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> RepositoryInventoryApplyResponse:
        trace_id = dependencies.next_trace_id()
        if isinstance(identity, TerminalAgentIdentity) or not dependencies.authorization_allows(
            identity=identity,
            action=REPOSITORY_INVENTORY_WRITE_ACTION,
            product=LAUNCHPLANE_SERVICE_CONTEXT,
            context=LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise dependencies.http_error(
                status_code=403, trace_id=trace_id, code="authorization_denied",
                message="Terminal agents and unauthorized identities cannot write repository inventory.",
            )
        if envelope.mode == "apply" and (not isinstance(record_store, PostgresRecordStore) or not idempotency_key.strip()):
            raise dependencies.http_error(
                status_code=503 if not isinstance(record_store, PostgresRecordStore) else 400,
                trace_id=trace_id,
                code="database_storage_required" if not isinstance(record_store, PostgresRecordStore) else "idempotency_key_required",
                message="Repository inventory apply requires PostgreSQL and an Idempotency-Key.",
            )
        try:
            result = apply_repository_inventory(
                store=require_repository_inventory_read_store(record_store),
                record=envelope.record,
                expected_current_record_id=envelope.expected_current_record_id,
                mode=envelope.mode,
            )
        except (RepositoryInventoryConflictError, RepositoryInventorySequenceError) as error:
            raise dependencies.http_error(
                status_code=409, trace_id=trace_id, code="repository_inventory_conflict", message=str(error)
            ) from error
        except (TypeError, ValueError) as error:
            raise dependencies.http_error(
                status_code=503 if isinstance(error, TypeError) else 400,
                trace_id=trace_id,
                code="database_storage_required" if isinstance(error, TypeError) else "invalid_request",
                message=str(error),
            ) from error
        return RepositoryInventoryApplyResponse(trace_id=trace_id, result=result)

    app.add_api_route(
        REPOSITORY_INVENTORY_APPLY_ROUTE,
        apply_repository_inventory_route,
        methods=["POST"],
        response_model=RepositoryInventoryApplyResponse,
        operation_id="apply_repository_inventory",
        summary="Apply or dry-run an inert repository inventory record",
    )
