from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Header, Path, Query
from pydantic import BaseModel, ConfigDict

from control_plane.contracts.production_backup_failures import (
    BACKUP_FAILED_CODE,
    backup_failure_description,
)
from control_plane.contracts.production_backup_gate import (
    PRODUCTION_BACKUP_GATE_EXECUTE_ACTION,
    ProductionBackupGateRequest,
)
from control_plane.contracts.durable_operation_authorization import (
    DurableOperationCancellationRequest,
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
from control_plane.operation_status_read import safe_operation_error_code
from control_plane.service_auth import AuthorizationTarget, LaunchplaneIdentity
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.production_backup_gate import enqueue_production_backup_gate
from control_plane.workflows.ship import utc_now_timestamp


PRODUCTION_BACKUP_GATE_ROUTE = "/v1/production-backup-gates"
PRODUCTION_BACKUP_GATE_OPERATION_ROUTE = "/v1/production-backup-gates/operations/{operation_id}"


@dataclass(frozen=True, slots=True)
class ProductionBackupGateRouteDependencies:
    common: ReadRouteDependencies
    read_mutation_identity: Callable[..., LaunchplaneIdentity]
    cancel_pending_operation: Callable[..., VeriReelProdBackupGateOperationRecord]


class ProductionBackupGateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    trace_id: str
    operation_id: str
    operation_status: str
    backup_record_id: str
    evidence: dict[str, str]
    error_code: str
    # Launchplane's fixed description of error_code; never provider text.
    error_description: str = ""


# Structured evidence the backup provider writes: record ids, digests, stage,
# snapshot and archive ids, times and statuses. Anything else, such as an older
# driver path's error_message, stays out of the response.
_BACKUP_EVIDENCE_KEYS = frozenset(
    {
        "provider",
        "product",
        "context",
        "instance",
        "promotion_action",
        "policy_record_id",
        "policy_revision",
        "policy_digest",
        "source_target_record_id",
        "source_target_digest",
        "destination_target_record_id",
        "destination_target_digest",
        "provider_stage",
        "requested_snapshot_name",
        "snapshot_name",
        "snapshot_started_at",
        "snapshot_finished_at",
        "independent_backup_started_at",
        "independent_backup_id",
        "independent_backup_finished_at",
        "capture_status",
        "retention_status",
        "retention_error_code",
    }
)


def _response(
    operation: VeriReelProdBackupGateOperationRecord, trace_id: str
) -> ProductionBackupGateResponse:
    evidence = operation.result.evidence if operation.result else operation.progress_evidence
    error_code = safe_operation_error_code(operation.error_code) or (
        BACKUP_FAILED_CODE if operation.status == "fail" else ""
    )
    return ProductionBackupGateResponse(
        trace_id=trace_id,
        operation_id=operation.operation_id,
        operation_status=operation.status,
        backup_record_id=operation.backup_record_id,
        evidence={key: value for key, value in evidence.items() if key in _BACKUP_EVIDENCE_KEYS},
        error_code=error_code,
        error_description=backup_failure_description(error_code),
    )


def register_production_backup_gate_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ProductionBackupGateRouteDependencies,
) -> None:
    common = dependencies.common

    def enqueue_backup(
        request: ProductionBackupGateRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_mutation_identity)],
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

    def scoped_operation(
        operation_id: str,
        product: str,
        context: str,
        instance: str,
        identity: LaunchplaneIdentity,
        record_store: object,
        trace_id: str,
        action: str,
        allow_unbound: bool = False,
    ) -> VeriReelProdBackupGateOperationRecord:
        """The operation in the requested scope.

        Only a read accepts ``allow_unbound``: an operation an older VeriReel
        driver path queued has no shared-gate binding, but the read still shows
        its structured status.
        """
        scope = (product.strip().lower(), context.strip().lower(), instance.strip().lower())
        if not common.authorization_allows(
            identity=identity,
            action=action,
            product=scope[0],
            context=scope[1],
            target=AuthorizationTarget(scope="instance", instances=(scope[2],)),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot access this production backup gate operation.",
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
            if (operation.binding is None and not allow_unbound) or (
                operation.product,
                operation.context,
                operation.instance,
            ) != scope:
                raise FileNotFoundError(operation_id)
        except FileNotFoundError as error:
            raise common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Production backup operation was not found.",
            ) from error
        return operation

    def read_backup_operation(
        operation_id: Annotated[str, Path(min_length=1, max_length=256)],
        product: Annotated[str, Query(min_length=1)],
        context: Annotated[str, Query(min_length=1)],
        instance: Annotated[str, Query(min_length=1)],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> ProductionBackupGateResponse:
        trace_id = common.next_trace_id()
        operation = scoped_operation(
            operation_id,
            product,
            context,
            instance,
            identity,
            record_store,
            trace_id,
            "production_backup_authority.read",
            allow_unbound=True,
        )
        return _response(operation, trace_id)

    def cancel_backup_operation(
        operation_id: Annotated[str, Path(min_length=1, max_length=256)],
        cancellation_request: DurableOperationCancellationRequest,
        product: Annotated[str, Query(min_length=1)],
        context: Annotated[str, Query(min_length=1)],
        instance: Annotated[str, Query(min_length=1)],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_mutation_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> ProductionBackupGateResponse:
        trace_id = common.next_trace_id()
        scoped_operation(
            operation_id,
            product,
            context,
            instance,
            identity,
            record_store,
            trace_id,
            PRODUCTION_BACKUP_GATE_EXECUTE_ACTION,
        )
        operation = dependencies.cancel_pending_operation(
            trace_id=trace_id,
            record_store=record_store,
            identity=identity,
            operation_id=operation_id,
            action=PRODUCTION_BACKUP_GATE_EXECUTE_ACTION,
            read_method_name="read_verireel_prod_backup_gate_operation_record",
            cancel_method_name="cancel_pending_verireel_prod_backup_gate_operation_record",
            cancellation_request=cancellation_request,
        )
        return _response(operation, trace_id)

    for path, handler, method, route_operation_id in (
        (PRODUCTION_BACKUP_GATE_ROUTE, enqueue_backup, "POST", "enqueue_production_backup_gate"),
        (
            PRODUCTION_BACKUP_GATE_OPERATION_ROUTE,
            read_backup_operation,
            "GET",
            "read_production_backup_gate_operation",
        ),
        (
            PRODUCTION_BACKUP_GATE_OPERATION_ROUTE + "/cancel",
            cancel_backup_operation,
            "POST",
            "cancel_production_backup_gate_operation",
        ),
    ):
        app.add_api_route(
            path,
            handler,
            methods=[method],
            response_model=ProductionBackupGateResponse,
            operation_id=route_operation_id,
            responses={
                status: {"model": common.error_response_model}
                for status in (400, 401, 403, 404, 409, 503)
            },
        )
