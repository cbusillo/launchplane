from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Header, Path, Query
from pydantic import BaseModel, ConfigDict

from control_plane.contracts.production_backup_gate import (
    PRODUCTION_BACKUP_GATE_EXECUTE_ACTION,
    ProductionBackupGateRequest,
)
from control_plane.contracts.verireel_prod_backup_gate_operation import (
    VeriReelProdBackupGateOperationRecord,
)
from control_plane.durable_operation_authorization import (
    DurableOperationAuthorizationCaptureError,
    capture_durable_operation_authorization,
    read_active_authz_policy_record,
)
from control_plane.http_routes.mutation_support import idempotency_scope
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.service_auth import AuthorizationTarget, LaunchplaneIdentity
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.production_backup_gate import enqueue_production_backup_gate
from control_plane.workflows.ship import utc_now_timestamp


PRODUCTION_BACKUP_GATE_ROUTE = "/v1/production-backup-gates"
PRODUCTION_BACKUP_GATE_OPERATION_ROUTE = "/v1/production-backup-gates/operations/{operation_id}"


@dataclass(frozen=True, slots=True)
class ProductionBackupGateRouteDependencies:
    common: ReadRouteDependencies
    read_write_identity: Callable[..., LaunchplaneIdentity]


class ProductionBackupGateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    trace_id: str
    operation_id: str
    operation_status: str
    backup_record_id: str
    evidence: dict[str, str]
    error_code: str


def _response(
    operation: VeriReelProdBackupGateOperationRecord, trace_id: str
) -> ProductionBackupGateResponse:
    return ProductionBackupGateResponse(
        trace_id=trace_id,
        operation_id=operation.operation_id,
        operation_status=operation.status,
        backup_record_id=operation.backup_record_id,
        evidence=operation.result.evidence if operation.result else {},
        error_code=operation.error_code or ("backup_failed" if operation.status == "fail" else ""),
    )


def register_production_backup_gate_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ProductionBackupGateRouteDependencies,
) -> None:
    common = dependencies.common

    def enqueue_backup(
        request: ProductionBackupGateRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", max_length=256)] = "",
    ) -> ProductionBackupGateResponse:
        trace_id = common.next_trace_id()
        if not common.authorization_allows(
            identity=identity,
            action=PRODUCTION_BACKUP_GATE_EXECUTE_ACTION,
            product=request.product,
            context=request.context,
            target=AuthorizationTarget(scope="instance", instances=(request.instance,)),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot execute this production backup gate.",
            )
        if not idempotency_key.strip():
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="missing_idempotency_key",
                message="Production backup requires Idempotency-Key.",
            )
        if not isinstance(
            record_store, PostgresRecordStore
        ) or record_store.database_url.startswith("sqlite"):
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Production backup execution requires PostgreSQL.",
            )
        try:
            authorization = capture_durable_operation_authorization(
                identity=identity,
                action=PRODUCTION_BACKUP_GATE_EXECUTE_ACTION,
                product=request.product,
                context=request.context,
                instances=(request.instance,),
                policy_record=read_active_authz_policy_record(record_store),
                authorized_at=utc_now_timestamp(),
            )
            operation = enqueue_production_backup_gate(
                record_store=record_store,
                request=request,
                authorization=authorization,
                operation_key=f"{idempotency_scope(identity)}|{idempotency_key.strip()}",
            )
        except DurableOperationAuthorizationCaptureError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="authorization_provenance_unavailable",
                message="Durable backup authorization is unavailable.",
            ) from error
        except (FileNotFoundError, ValueError) as error:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="production_backup_conflict",
                message=str(error),
            ) from error
        return _response(operation, trace_id)

    def read_backup_operation(
        operation_id: Annotated[str, Path(min_length=1, max_length=256)],
        product: Annotated[str, Query(min_length=1)],
        context: Annotated[str, Query(min_length=1)],
        instance: Annotated[str, Query(min_length=1)],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> ProductionBackupGateResponse:
        trace_id = common.next_trace_id()
        scope = (product.strip().lower(), context.strip().lower(), instance.strip().lower())
        if not common.authorization_allows(
            identity=identity,
            action="production_backup_authority.read",
            product=scope[0],
            context=scope[1],
            target=AuthorizationTarget(scope="instance", instances=(scope[2],)),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot read this production backup gate.",
            )
        if not isinstance(record_store, PostgresRecordStore):
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Production backup operations require database storage.",
            )
        try:
            operation = record_store.read_verireel_prod_backup_gate_operation_record(operation_id)
            if (
                operation.binding is None
                or (operation.product, operation.context, operation.instance) != scope
            ):
                raise FileNotFoundError(operation_id)
        except FileNotFoundError as error:
            raise common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Production backup operation was not found.",
            ) from error
        return _response(operation, trace_id)

    for path, handler, method, operation_id in (
        (PRODUCTION_BACKUP_GATE_ROUTE, enqueue_backup, "POST", "enqueue_production_backup_gate"),
        (
            PRODUCTION_BACKUP_GATE_OPERATION_ROUTE,
            read_backup_operation,
            "GET",
            "read_production_backup_gate_operation",
        ),
    ):
        app.add_api_route(
            path,
            handler,
            methods=[method],
            response_model=ProductionBackupGateResponse,
            operation_id=operation_id,
            responses={
                status: {"model": common.error_response_model}
                for status in (400, 401, 403, 404, 409, 503)
            },
        )
