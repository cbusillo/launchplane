from dataclasses import dataclass
from collections.abc import Callable
from typing import Annotated, Literal, cast

from fastapi import Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field

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
from control_plane.http_routes.mutation_support import idempotency_scope, request_fingerprint
from control_plane.repository_inventory import (
    RepositoryInventoryApplyEnvelope,
    RepositoryInventoryApplyResult,
    RepositoryInventoryConflictError,
    RepositoryInventoryReadModel,
    RepositoryInventorySequenceError,
    dry_run_repository_inventory,
    get_repository_inventory_read_model,
    require_repository_inventory_read_store,
)
from control_plane.service_auth import LaunchplaneIdentity, TerminalAgentIdentity
from control_plane.storage.postgres import DbOnlyMutationRequest, PostgresRecordStore

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
    replayed: bool | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    original_trace_id: str | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


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
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
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
                code="database_storage_required"
                if isinstance(error, TypeError)
                else "invalid_request",
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
        responses={
            status_code: {"model": dependencies.error_response_model}
            for status_code in (400, 401, 403, 503)
        },
    )


def register_repository_inventory_write_routes(
    app: ApiRouteRegistrar, *, dependencies: RepositoryInventoryWriteRouteDependencies
) -> None:
    async def apply_repository_inventory_route(
        request: Request,
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
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Terminal agents and unauthorized identities cannot write repository inventory.",
            )
        if envelope.mode == "apply":
            normalized_idempotency_key = idempotency_key.strip()
            if not normalized_idempotency_key:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="idempotency_key_required",
                    message="Repository inventory apply requires an Idempotency-Key header.",
                )
            if (
                not isinstance(record_store, PostgresRecordStore)
                or record_store.database_dialect_name != "postgresql"
            ):
                raise dependencies.http_error(
                    status_code=503,
                    trace_id=trace_id,
                    code="database_storage_required",
                    message=(
                        "Repository inventory apply requires PostgreSQL-backed Launchplane storage."
                    ),
                )

            result = RepositoryInventoryApplyResult(
                status="applied",
                mode="apply",
                repository_id=envelope.record.repository_id,
                inventory_revision=envelope.record.inventory_revision,
                record_id=envelope.record.record_id,
                inventory_digest=envelope.record.inventory_digest,
                supersedes_record_id=envelope.record.supersedes_record_id,
                applied_at=envelope.record.recorded_at,
            )
            response = RepositoryInventoryApplyResponse(trace_id=trace_id, result=result)
            raw_payload = await request.json()
            try:
                write_result = record_store.compare_and_write_repository_inventory_record(
                    record=envelope.record,
                    expected_current_record_id=envelope.expected_current_record_id,
                    mutation=DbOnlyMutationRequest(
                        scope=idempotency_scope(identity),
                        route_path=REPOSITORY_INVENTORY_APPLY_ROUTE,
                        idempotency_key=normalized_idempotency_key,
                        request_fingerprint=request_fingerprint(
                            cast(dict[str, object], raw_payload)
                        ),
                        lease_owner=trace_id,
                        response_status_code=200,
                        response_trace_id=trace_id,
                        response_payload=response.model_dump(mode="json"),
                    ),
                )
            except RepositoryInventoryConflictError as error:
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="repository_inventory_conflict",
                    message=str(error),
                ) from error
            except RepositoryInventorySequenceError as error:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="invalid_sequence",
                    message=str(error),
                ) from error
            except ValueError as error:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="invalid_request",
                    message=str(error),
                ) from error

            if write_result.status == "idempotency_conflict":
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="idempotency_key_reused",
                    message=(
                        "Idempotency-Key was already used for a different Launchplane request "
                        "payload on this route."
                    ),
                )
            if write_result.status == "replayed" and write_result.idempotency_record is not None:
                payload = dict(write_result.idempotency_record.response_payload)
                payload["replayed"] = True
                payload["original_trace_id"] = write_result.idempotency_record.response_trace_id
                return RepositoryInventoryApplyResponse.model_validate(payload)
            if write_result.status == "reservation_in_progress":
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_in_progress",
                    message="Repository inventory apply is already in progress.",
                )
            if write_result.status == "reconciliation_required":
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_reconciliation_required",
                    message="Repository inventory apply requires reconciliation before retry.",
                )
            return response

        try:
            result = dry_run_repository_inventory(
                store=require_repository_inventory_read_store(record_store),
                record=envelope.record,
                expected_current_record_id=envelope.expected_current_record_id,
            )
        except RepositoryInventoryConflictError as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="repository_inventory_conflict",
                message=str(error),
            ) from error
        except RepositoryInventorySequenceError as error:
            raise dependencies.http_error(
                status_code=400, trace_id=trace_id, code="invalid_sequence", message=str(error)
            ) from error
        except (TypeError, ValueError) as error:
            raise dependencies.http_error(
                status_code=503 if isinstance(error, TypeError) else 400,
                trace_id=trace_id,
                code="database_storage_required"
                if isinstance(error, TypeError)
                else "invalid_request",
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
        responses={
            status_code: {"model": dependencies.error_response_model}
            for status_code in (400, 401, 403, 409, 503)
        },
    )
