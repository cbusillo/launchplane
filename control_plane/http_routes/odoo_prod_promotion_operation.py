"""Signed-in operator routes that queue an Odoo prod promotion and read its progress.

The synchronous ``/v1/drivers/odoo/prod-promotion-run`` route can run for most of an
hour. A browser request that long times out through ingress and looks uncertain, and a
retry would start a second run. These routes check the same preconditions up front,
queue one durable operation per lane for the stable-lane worker, and return at once.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path as FilePath
from typing import Annotated, Literal, NoReturn, cast

from fastapi import Depends, Header, Path, Query
from pydantic import BaseModel, ConfigDict

from control_plane.contracts.durable_operation_authorization import (
    DurableOperationCancellationRequest,
)
from control_plane.contracts.odoo_prod_promotion_operation import (
    ODOO_PROD_PROMOTION_RUN_ACTION,
    OdooProdPromotionOperationPhase,
    OdooProdPromotionOperationRecord,
    OdooProdPromotionOperationStatus,
    OdooProdPromotionRunResult,
    build_odoo_prod_promotion_operation_id,
    odoo_prod_promotion_request_fingerprint,
)
from control_plane.durable_operation_authorization import (
    DurableOperationAuthorizationCaptureError,
    capture_durable_operation_authorization,
    read_active_authz_policy_record,
)
from control_plane.http_routes.mutation_support import idempotency_scope
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.odoo_prod_promotion_http import (
    OdooProdPromotionProductMismatchError,
    OdooProdPromotionRouteDependencyError,
    OdooProdPromotionRunEnvelope,
    resolve_odoo_prod_promotion_product_route,
)
from control_plane.odoo_stable_lane import (
    ODOO_STABLE_LANE_BLOCKING_STATUSES,
    OdooStableLaneOperationConflictError,
)
from control_plane.service_auth import AuthorizationTarget, LaunchplaneIdentity
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.odoo_prod_promotion_run import (
    OdooProdPromotionRunStore,
    admit_odoo_prod_promotion_run,
)
from control_plane.workflows.ship import utc_now_timestamp


ODOO_PROD_PROMOTION_OPERATIONS_ROUTE = "/v1/odoo-prod-promotions"
ODOO_PROD_PROMOTION_OPERATION_ROUTE = "/v1/odoo-prod-promotions/operations/{operation_id}"


@dataclass(frozen=True, slots=True)
class OdooProdPromotionOperationRouteDependencies:
    common: ReadRouteDependencies
    read_mutation_identity: Callable[..., LaunchplaneIdentity]
    cancel_pending_operation: Callable[..., OdooProdPromotionOperationRecord]
    control_plane_root: FilePath


class OdooProdPromotionOperationView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str
    product: str
    context: str
    instance: str
    request_id: str
    status: OdooProdPromotionOperationStatus
    phase: OdooProdPromotionOperationPhase
    attempt: int
    created_at: str
    started_at: str
    updated_at: str
    finished_at: str
    error_code: str
    error_message: str
    result: OdooProdPromotionRunResult | None = None


class OdooProdPromotionOperationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    trace_id: str
    operation: OdooProdPromotionOperationView


def _response(
    operation: OdooProdPromotionOperationRecord, trace_id: str
) -> OdooProdPromotionOperationResponse:
    return OdooProdPromotionOperationResponse(
        trace_id=trace_id,
        operation=OdooProdPromotionOperationView(
            operation_id=operation.operation_id,
            product=operation.product,
            context=operation.context,
            instance=operation.instance,
            request_id=operation.request.request_id,
            status=operation.status,
            phase=operation.phase,
            attempt=operation.attempt,
            created_at=operation.created_at,
            started_at=operation.started_at,
            updated_at=operation.updated_at,
            finished_at=operation.finished_at,
            error_code=operation.error_code,
            error_message=operation.error_message,
            result=operation.result,
        ),
    )


def register_odoo_prod_promotion_operation_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: OdooProdPromotionOperationRouteDependencies,
) -> None:
    common = dependencies.common

    def require_operation_store(record_store: object, trace_id: str) -> PostgresRecordStore:
        if not isinstance(record_store, PostgresRecordStore):
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Odoo prod promotion operations require database storage.",
            )
        return record_store

    def require_execute_allowed(
        *,
        identity: LaunchplaneIdentity,
        product: str,
        context: str,
        instances: tuple[str, ...],
        trace_id: str,
    ) -> None:
        if not common.authorization_allows(
            identity=identity,
            action=ODOO_PROD_PROMOTION_RUN_ACTION,
            product=product,
            context=context,
            target=AuthorizationTarget(scope="instance", instances=instances),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot run an Odoo prod promotion for this product/context.",
            )

    def replay_or_conflict(
        operation: OdooProdPromotionOperationRecord,
        *,
        fingerprint: str,
        trace_id: str,
    ) -> OdooProdPromotionOperationResponse:
        if operation.request_fingerprint != fingerprint:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="idempotency_key_reused",
                message="Idempotency-Key was already used for a different promotion request.",
            )
        return _response(operation, trace_id)

    def raise_active_promotion(
        operation: OdooProdPromotionOperationRecord, trace_id: str
    ) -> NoReturn:
        raise common.http_error(
            status_code=409,
            trace_id=trace_id,
            code="promotion_already_active",
            message=(
                f"Odoo prod promotion {operation.operation_id} is already "
                f"{operation.status.replace('_', ' ')} on this lane."
            ),
        )

    def enqueue_promotion(
        request: OdooProdPromotionRunEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_mutation_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", max_length=256)] = "",
    ) -> OdooProdPromotionOperationResponse:
        trace_id = common.next_trace_id()
        run = request.run
        try:
            product_profile = resolve_odoo_prod_promotion_product_route(
                record_store=record_store,
                product=request.product,
                context=run.context,
                instances=(run.from_instance, run.to_instance),
            )
        except OdooProdPromotionRouteDependencyError as error:
            raise common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="The product has no Odoo prod promotion lane.",
            ) from error
        except OdooProdPromotionProductMismatchError as error:
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the Odoo driver.",
            ) from error
        except ValueError as error:
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        product = product_profile.product
        require_execute_allowed(
            identity=identity,
            product=product,
            context=run.context,
            instances=(run.from_instance, run.to_instance),
            trace_id=trace_id,
        )
        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="missing_idempotency_key",
                message="Odoo prod promotion requires Idempotency-Key.",
            )
        if not run.wait:
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="A queued Odoo prod promotion waits for its deploy; wait must be true.",
            )
        operation_store = require_operation_store(record_store, trace_id)
        run_request = run.model_copy(update={"product": product})
        fingerprint = odoo_prod_promotion_request_fingerprint(run_request)
        scope = idempotency_scope(identity)
        operation_id = build_odoo_prod_promotion_operation_id(
            product=product,
            context=run_request.context,
            idempotency_key=normalized_key,
            idempotency_scope=scope,
        )
        try:
            existing = operation_store.read_odoo_prod_promotion_operation_record(operation_id)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            return replay_or_conflict(existing, fingerprint=fingerprint, trace_id=trace_id)
        active_promotions = operation_store.list_odoo_prod_promotion_operation_records(
            product=product,
            context_name=run_request.context,
            instance_name="prod",
            statuses=ODOO_STABLE_LANE_BLOCKING_STATUSES,
            limit=1,
        )
        if active_promotions:
            raise_active_promotion(active_promotions[0], trace_id)

        admission = admit_odoo_prod_promotion_run(
            control_plane_root=dependencies.control_plane_root,
            record_store=cast(OdooProdPromotionRunStore, operation_store),
            request=run_request,
        )
        if admission.blocked_reason:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="promotion_not_ready",
                message=admission.blocked_reason,
            )
        created_at = utc_now_timestamp()
        try:
            authorization = capture_durable_operation_authorization(
                identity=identity,
                action=ODOO_PROD_PROMOTION_RUN_ACTION,
                product=product,
                context=run_request.context,
                instances=(run_request.to_instance,),
                policy_record=read_active_authz_policy_record(operation_store),
                authorized_at=created_at,
            )
        except (DurableOperationAuthorizationCaptureError, LookupError, TypeError) as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="authorization_provenance_unavailable",
                message="Durable promotion authorization is unavailable.",
            ) from error
        operation = OdooProdPromotionOperationRecord(
            operation_id=operation_id,
            product=product,
            context=run_request.context,
            instance="prod",
            idempotency_key=normalized_key,
            idempotency_scope=scope,
            request_fingerprint=fingerprint,
            request=run_request,
            authorization=authorization,
            created_at=created_at,
            updated_at=created_at,
            runner_trace_id=trace_id,
        )
        try:
            persisted, created = (
                operation_store.create_odoo_prod_promotion_operation_record_if_no_active_lane(
                    operation
                )
            )
        except OdooStableLaneOperationConflictError as error:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="lane_busy",
                message=(
                    "Another Odoo operation is active on this prod lane "
                    f"({error.owner.operation_kind} {error.owner.operation_id})."
                ),
            ) from error
        if created:
            return _response(persisted, trace_id)
        if persisted.operation_id == operation_id:
            return replay_or_conflict(persisted, fingerprint=fingerprint, trace_id=trace_id)
        raise_active_promotion(persisted, trace_id)

    def scoped_operation(
        *,
        operation_id: str,
        product: str,
        context: str,
        identity: LaunchplaneIdentity,
        record_store: object,
        trace_id: str,
    ) -> OdooProdPromotionOperationRecord:
        scope = (product.strip(), context.strip().lower())
        require_execute_allowed(
            identity=identity,
            product=scope[0],
            context=scope[1],
            instances=("prod",),
            trace_id=trace_id,
        )
        operation_store = require_operation_store(record_store, trace_id)
        try:
            operation = operation_store.read_odoo_prod_promotion_operation_record(operation_id)
            if (operation.product, operation.context) != scope:
                raise FileNotFoundError(operation_id)
        except FileNotFoundError as error:
            raise common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Odoo prod promotion operation was not found.",
            ) from error
        return operation

    def read_promotion_operation(
        operation_id: Annotated[str, Path(min_length=1, max_length=256)],
        product: Annotated[str, Query(min_length=1)],
        context: Annotated[str, Query(min_length=1)],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> OdooProdPromotionOperationResponse:
        trace_id = common.next_trace_id()
        operation = scoped_operation(
            operation_id=operation_id,
            product=product,
            context=context,
            identity=identity,
            record_store=record_store,
            trace_id=trace_id,
        )
        return _response(operation, trace_id)

    def cancel_promotion_operation(
        operation_id: Annotated[str, Path(min_length=1, max_length=256)],
        cancellation_request: DurableOperationCancellationRequest,
        product: Annotated[str, Query(min_length=1)],
        context: Annotated[str, Query(min_length=1)],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_mutation_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> OdooProdPromotionOperationResponse:
        trace_id = common.next_trace_id()
        scoped_operation(
            operation_id=operation_id,
            product=product,
            context=context,
            identity=identity,
            record_store=record_store,
            trace_id=trace_id,
        )
        operation = dependencies.cancel_pending_operation(
            trace_id=trace_id,
            record_store=record_store,
            identity=identity,
            operation_id=operation_id,
            action=ODOO_PROD_PROMOTION_RUN_ACTION,
            read_method_name="read_odoo_prod_promotion_operation_record",
            cancel_method_name="cancel_pending_odoo_prod_promotion_operation_record",
            cancellation_request=cancellation_request,
        )
        return _response(operation, trace_id)

    for path, handler, method, route_operation_id in (
        (
            ODOO_PROD_PROMOTION_OPERATIONS_ROUTE,
            enqueue_promotion,
            "POST",
            "enqueue_odoo_prod_promotion",
        ),
        (
            ODOO_PROD_PROMOTION_OPERATION_ROUTE,
            read_promotion_operation,
            "GET",
            "read_odoo_prod_promotion_operation",
        ),
        (
            ODOO_PROD_PROMOTION_OPERATION_ROUTE + "/cancel",
            cancel_promotion_operation,
            "POST",
            "cancel_odoo_prod_promotion_operation",
        ),
    ):
        app.add_api_route(
            path,
            handler,
            methods=[method],
            response_model=OdooProdPromotionOperationResponse,
            operation_id=route_operation_id,
            responses={
                status: {"model": common.error_response_model}
                for status in (400, 401, 403, 404, 409, 503)
            },
        )
