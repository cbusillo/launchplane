"""Routes the signed-in policy administrator uses to release an Odoo prod lane.

A promotion can run for most of an hour and a rollback for many minutes. A browser
request that long times out through ingress and looks uncertain, and a retry would
start a second run. These routes check what the run checks up front, queue one
durable operation per lane for the Odoo stable-lane worker, and return at once; the
operator's release panel polls the read routes.

Only the person the active policy names as administrator may queue or cancel one:
no automated identity gets the right to change a production site this way. The
synchronous driver routes keep their workflow callers until those are deleted.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path as FilePath
from typing import Annotated, Literal, NoReturn, cast

import click
from fastapi import Depends, Header, Path, Query
from pydantic import BaseModel, ConfigDict

from control_plane.contracts.durable_operation_authorization import (
    DurableOperationAuthorization,
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
from control_plane.contracts.odoo_prod_rollback_operation import (
    ODOO_PROD_ROLLBACK_ACTION,
    OdooProdRollbackOperationPhase,
    OdooProdRollbackOperationRecord,
    OdooProdRollbackOperationStatus,
    OdooProdRollbackResult,
    build_odoo_prod_rollback_operation_id,
    odoo_prod_rollback_request_fingerprint,
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
from control_plane.odoo_prod_rollback_http import (
    OdooProdRollbackEnvelope,
    OdooProdRollbackProductMismatchError,
    OdooProdRollbackRouteDependencyError,
    resolve_odoo_prod_rollback_product_route,
)
from control_plane.odoo_stable_lane import (
    ODOO_STABLE_LANE_BLOCKING_STATUSES,
    OdooStableLaneOperationConflictError,
)
from control_plane.service_auth import AuthorizationTarget, GitHubHumanIdentity, LaunchplaneIdentity
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.odoo_prod_promotion_run import (
    OdooProdPromotionRunStore,
    admit_odoo_prod_promotion_run,
)
from control_plane.workflows.odoo_prod_rollback import (
    OdooProdRollbackTargetMissingError,
    resolve_odoo_prod_rollback_target,
)
from control_plane.workflows.ship import utc_now_timestamp


ODOO_PROD_PROMOTION_OPERATIONS_ROUTE = "/v1/odoo-prod-promotions"
ODOO_PROD_PROMOTION_OPERATION_ROUTE = "/v1/odoo-prod-promotions/operations/{operation_id}"
ODOO_PROD_ROLLBACK_OPERATIONS_ROUTE = "/v1/odoo-prod-rollbacks"
ODOO_PROD_ROLLBACK_OPERATION_ROUTE = "/v1/odoo-prod-rollbacks/operations/{operation_id}"


@dataclass(frozen=True, slots=True)
class OdooProdReleaseOperationRouteDependencies:
    common: ReadRouteDependencies
    read_mutation_identity: Callable[..., LaunchplaneIdentity]
    cancel_pending_operation: Callable[..., object]
    control_plane_root: FilePath


class _OperationViewBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str
    product: str
    context: str
    instance: str
    attempt: int
    created_at: str
    started_at: str
    updated_at: str
    finished_at: str
    error_code: str
    error_message: str


class OdooProdPromotionOperationView(_OperationViewBase):
    request_id: str
    status: OdooProdPromotionOperationStatus
    phase: OdooProdPromotionOperationPhase
    result: OdooProdPromotionRunResult | None = None


class OdooProdPromotionOperationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    trace_id: str
    operation: OdooProdPromotionOperationView


class OdooProdRollbackOperationView(_OperationViewBase):
    reason: str
    target_artifact_id: str
    target_deployment_record_id: str
    status: OdooProdRollbackOperationStatus
    phase: OdooProdRollbackOperationPhase
    result: OdooProdRollbackResult | None = None


class OdooProdRollbackOperationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    trace_id: str
    operation: OdooProdRollbackOperationView


def _common_view_fields(operation: object) -> dict[str, object]:
    return {
        field_name: getattr(operation, field_name)
        for field_name in (*_OperationViewBase.model_fields, "status", "phase", "result")
    }


def _promotion_response(
    operation: OdooProdPromotionOperationRecord, trace_id: str
) -> OdooProdPromotionOperationResponse:
    return OdooProdPromotionOperationResponse(
        trace_id=trace_id,
        operation=OdooProdPromotionOperationView.model_validate(
            {**_common_view_fields(operation), "request_id": operation.request.request_id}
        ),
    )


def _rollback_response(
    operation: OdooProdRollbackOperationRecord, trace_id: str
) -> OdooProdRollbackOperationResponse:
    return OdooProdRollbackOperationResponse(
        trace_id=trace_id,
        operation=OdooProdRollbackOperationView.model_validate(
            {
                **_common_view_fields(operation),
                "reason": operation.request.reason,
                "target_artifact_id": operation.target.artifact_id,
                "target_deployment_record_id": operation.target.deployment_record_id,
            }
        ),
    )


def register_odoo_prod_release_operation_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: OdooProdReleaseOperationRouteDependencies,
) -> None:
    common = dependencies.common

    def require_operation_store(record_store: object, trace_id: str) -> PostgresRecordStore:
        if not isinstance(record_store, PostgresRecordStore):
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Odoo prod release operations require database storage.",
            )
        return record_store

    def require_policy_administrator(
        identity: LaunchplaneIdentity, store: PostgresRecordStore, trace_id: str
    ) -> None:
        try:
            policy = read_active_authz_policy_record(store).policy
        except (LookupError, TypeError) as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="authorization_provenance_unavailable",
                message="The active authorization policy is unavailable.",
            ) from error
        if not isinstance(identity, GitHubHumanIdentity) or not policy.names_administrator(
            identity
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Only the signed-in policy administrator can queue or cancel an Odoo "
                    "prod release."
                ),
            )

    def capture_administrator_authorization(
        *,
        identity: LaunchplaneIdentity,
        store: PostgresRecordStore,
        action: str,
        product: str,
        context: str,
        authorized_at: str,
        trace_id: str,
    ) -> DurableOperationAuthorization:
        try:
            authorization = capture_durable_operation_authorization(
                identity=identity,
                action=action,
                product=product,
                context=context,
                instances=("prod",),
                policy_record=read_active_authz_policy_record(store),
                authorized_at=authorized_at,
            )
        except (DurableOperationAuthorizationCaptureError, LookupError, TypeError) as error:
            authorization = None
            cause: Exception | None = error
        else:
            cause = None
        if authorization is None or authorization.grant != "policy_administrator":
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="authorization_provenance_unavailable",
                message="Durable administrator authorization is unavailable.",
            ) from cause
        return authorization

    def require_idempotency_key(idempotency_key: str, trace_id: str) -> str:
        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="missing_idempotency_key",
                message="An Odoo prod release requires Idempotency-Key.",
            )
        return normalized_key

    def require_wait(wait: bool, trace_id: str) -> None:
        if not wait:
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="A queued Odoo prod release waits for its deploy; wait must be true.",
            )

    def raise_conflict(code: str, message: str, trace_id: str) -> NoReturn:
        raise common.http_error(status_code=409, trace_id=trace_id, code=code, message=message)

    def require_same_request(fingerprint: str, expected: str, trace_id: str) -> None:
        if fingerprint != expected:
            raise_conflict(
                "idempotency_key_reused",
                "Idempotency-Key was already used for a different request.",
                trace_id,
            )

    def raise_active(kind: str, operation_id: str, status: str, trace_id: str) -> NoReturn:
        raise_conflict(
            f"{kind}_already_active",
            f"Odoo prod {kind} {operation_id} is already {status.replace('_', ' ')} on this lane.",
            trace_id,
        )

    def raise_lane_busy(error: OdooStableLaneOperationConflictError, trace_id: str) -> NoReturn:
        raise common.http_error(
            status_code=409,
            trace_id=trace_id,
            code="lane_busy",
            message=(
                "Another Odoo operation is active on this prod lane "
                f"({error.owner.operation_kind} {error.owner.operation_id})."
            ),
        ) from error

    def bad_route(trace_id: str, error: Exception) -> NoReturn:
        if isinstance(
            error, (OdooProdPromotionRouteDependencyError, OdooProdRollbackRouteDependencyError)
        ):
            raise common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="The product has no Odoo prod lane.",
            ) from error
        if isinstance(
            error, (OdooProdPromotionProductMismatchError, OdooProdRollbackProductMismatchError)
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the Odoo driver.",
            ) from error
        raise common.http_error(
            status_code=400,
            trace_id=trace_id,
            code="invalid_request",
            message="Request could not be completed.",
        ) from error

    def enqueue_promotion(
        request: OdooProdPromotionRunEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_mutation_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", max_length=256)] = "",
    ) -> OdooProdPromotionOperationResponse:
        trace_id = common.next_trace_id()
        store = require_operation_store(record_store, trace_id)
        require_policy_administrator(identity, store, trace_id)
        run = request.run
        try:
            product = resolve_odoo_prod_promotion_product_route(
                record_store=store,
                product=request.product,
                context=run.context,
                instances=(run.from_instance, run.to_instance),
            ).product
        except ValueError as error:
            bad_route(trace_id, error)
        normalized_key = require_idempotency_key(idempotency_key, trace_id)
        require_wait(run.wait, trace_id)
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
            existing = store.read_odoo_prod_promotion_operation_record(operation_id)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            require_same_request(fingerprint, existing.request_fingerprint, trace_id)
            return _promotion_response(existing, trace_id)
        for active in store.list_odoo_prod_promotion_operation_records(
            product=product,
            context_name=run_request.context,
            instance_name="prod",
            statuses=ODOO_STABLE_LANE_BLOCKING_STATUSES,
            limit=1,
        ):
            raise_active("promotion", active.operation_id, active.status, trace_id)
        admission = admit_odoo_prod_promotion_run(
            control_plane_root=dependencies.control_plane_root,
            record_store=cast(OdooProdPromotionRunStore, store),
            request=run_request,
        )
        if admission.blocked_reason:
            raise_conflict("promotion_not_ready", admission.blocked_reason, trace_id)
        created_at = utc_now_timestamp()
        operation = OdooProdPromotionOperationRecord(
            operation_id=operation_id,
            product=product,
            context=run_request.context,
            instance="prod",
            idempotency_key=normalized_key,
            idempotency_scope=scope,
            request_fingerprint=fingerprint,
            request=run_request,
            authorization=capture_administrator_authorization(
                identity=identity,
                store=store,
                action=ODOO_PROD_PROMOTION_RUN_ACTION,
                product=product,
                context=run_request.context,
                authorized_at=created_at,
                trace_id=trace_id,
            ),
            created_at=created_at,
            updated_at=created_at,
            runner_trace_id=trace_id,
        )
        try:
            persisted, created = (
                store.create_odoo_prod_promotion_operation_record_if_no_active_lane(operation)
            )
        except OdooStableLaneOperationConflictError as error:
            raise_lane_busy(error, trace_id)
        if not created:
            if persisted.operation_id != operation_id:
                raise_active("promotion", persisted.operation_id, persisted.status, trace_id)
            require_same_request(fingerprint, persisted.request_fingerprint, trace_id)
        return _promotion_response(persisted, trace_id)

    def enqueue_rollback(
        request: OdooProdRollbackEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_mutation_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", max_length=256)] = "",
    ) -> OdooProdRollbackOperationResponse:
        trace_id = common.next_trace_id()
        store = require_operation_store(record_store, trace_id)
        require_policy_administrator(identity, store, trace_id)
        rollback = request.rollback
        try:
            product = resolve_odoo_prod_rollback_product_route(
                record_store=store,
                product=request.product,
                context=rollback.context,
                instance=rollback.instance,
            ).product
        except ValueError as error:
            bad_route(trace_id, error)
        normalized_key = require_idempotency_key(idempotency_key, trace_id)
        require_wait(rollback.wait, trace_id)
        fingerprint = odoo_prod_rollback_request_fingerprint(product=product, request=rollback)
        scope = idempotency_scope(identity)
        operation_id = build_odoo_prod_rollback_operation_id(
            product=product,
            context=rollback.context,
            idempotency_key=normalized_key,
            idempotency_scope=scope,
        )
        try:
            existing = store.read_odoo_prod_rollback_operation_record(operation_id)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            # A retry returns the operation and its recorded target; nothing re-resolves.
            require_same_request(fingerprint, existing.request_fingerprint, trace_id)
            return _rollback_response(existing, trace_id)
        for active in store.list_odoo_prod_rollback_operation_records(
            product=product,
            context_name=rollback.context,
            instance_name="prod",
            statuses=ODOO_STABLE_LANE_BLOCKING_STATUSES,
            limit=1,
        ):
            raise_active("rollback", active.operation_id, active.status, trace_id)
        try:
            target = resolve_odoo_prod_rollback_target(record_store=store, request=rollback)
        except OdooProdRollbackTargetMissingError as error:
            raise_conflict("rollback_target_missing", error.message, trace_id)
        except click.ClickException as error:
            raise_conflict("rollback_not_ready", error.message, trace_id)
        created_at = utc_now_timestamp()
        operation = OdooProdRollbackOperationRecord(
            operation_id=operation_id,
            product=product,
            context=rollback.context,
            instance="prod",
            idempotency_key=normalized_key,
            idempotency_scope=scope,
            request_fingerprint=fingerprint,
            request=rollback,
            target=target,
            authorization=capture_administrator_authorization(
                identity=identity,
                store=store,
                action=ODOO_PROD_ROLLBACK_ACTION,
                product=product,
                context=rollback.context,
                authorized_at=created_at,
                trace_id=trace_id,
            ),
            created_at=created_at,
            updated_at=created_at,
            runner_trace_id=trace_id,
        )
        try:
            persisted, created = store.create_odoo_prod_rollback_operation_record_if_no_active_lane(
                operation
            )
        except OdooStableLaneOperationConflictError as error:
            raise_lane_busy(error, trace_id)
        if not created:
            if persisted.operation_id != operation_id:
                raise_active("rollback", persisted.operation_id, persisted.status, trace_id)
            require_same_request(fingerprint, persisted.request_fingerprint, trace_id)
        return _rollback_response(persisted, trace_id)

    def scoped_operation(
        *,
        read: Callable[[PostgresRecordStore, str], OdooProdPromotionOperationRecord]
        | Callable[[PostgresRecordStore, str], OdooProdRollbackOperationRecord],
        action: str,
        operation_id: str,
        product: str,
        context: str,
        identity: LaunchplaneIdentity,
        record_store: object,
        trace_id: str,
    ) -> OdooProdPromotionOperationRecord | OdooProdRollbackOperationRecord:
        scope = (product.strip(), context.strip().lower())
        if not common.authorization_allows(
            identity=identity,
            action=action,
            product=scope[0],
            context=scope[1],
            target=AuthorizationTarget(scope="instance", instances=("prod",)),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot read this Odoo prod release operation.",
            )
        store = require_operation_store(record_store, trace_id)
        try:
            operation = read(store, operation_id)
            if (operation.product, operation.context) != scope:
                raise FileNotFoundError(operation_id)
        except FileNotFoundError as error:
            raise common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Odoo prod release operation was not found.",
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
            read=lambda store, key: store.read_odoo_prod_promotion_operation_record(key),
            action=ODOO_PROD_PROMOTION_RUN_ACTION,
            operation_id=operation_id,
            product=product,
            context=context,
            identity=identity,
            record_store=record_store,
            trace_id=trace_id,
        )
        return _promotion_response(cast(OdooProdPromotionOperationRecord, operation), trace_id)

    def read_rollback_operation(
        operation_id: Annotated[str, Path(min_length=1, max_length=256)],
        product: Annotated[str, Query(min_length=1)],
        context: Annotated[str, Query(min_length=1)],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> OdooProdRollbackOperationResponse:
        trace_id = common.next_trace_id()
        operation = scoped_operation(
            read=lambda store, key: store.read_odoo_prod_rollback_operation_record(key),
            action=ODOO_PROD_ROLLBACK_ACTION,
            operation_id=operation_id,
            product=product,
            context=context,
            identity=identity,
            record_store=record_store,
            trace_id=trace_id,
        )
        return _rollback_response(cast(OdooProdRollbackOperationRecord, operation), trace_id)

    def cancel_promotion_operation(
        operation_id: Annotated[str, Path(min_length=1, max_length=256)],
        cancellation_request: DurableOperationCancellationRequest,
        product: Annotated[str, Query(min_length=1)],
        context: Annotated[str, Query(min_length=1)],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_mutation_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> OdooProdPromotionOperationResponse:
        trace_id = common.next_trace_id()
        require_policy_administrator(
            identity, require_operation_store(record_store, trace_id), trace_id
        )
        scoped_operation(
            read=lambda store, key: store.read_odoo_prod_promotion_operation_record(key),
            action=ODOO_PROD_PROMOTION_RUN_ACTION,
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
        return _promotion_response(cast(OdooProdPromotionOperationRecord, operation), trace_id)

    def cancel_rollback_operation(
        operation_id: Annotated[str, Path(min_length=1, max_length=256)],
        cancellation_request: DurableOperationCancellationRequest,
        product: Annotated[str, Query(min_length=1)],
        context: Annotated[str, Query(min_length=1)],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_mutation_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> OdooProdRollbackOperationResponse:
        trace_id = common.next_trace_id()
        require_policy_administrator(
            identity, require_operation_store(record_store, trace_id), trace_id
        )
        scoped_operation(
            read=lambda store, key: store.read_odoo_prod_rollback_operation_record(key),
            action=ODOO_PROD_ROLLBACK_ACTION,
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
            action=ODOO_PROD_ROLLBACK_ACTION,
            read_method_name="read_odoo_prod_rollback_operation_record",
            cancel_method_name="cancel_pending_odoo_prod_rollback_operation_record",
            cancellation_request=cancellation_request,
        )
        return _rollback_response(cast(OdooProdRollbackOperationRecord, operation), trace_id)

    routes: tuple[tuple[str, Callable[..., object], str, str, type[BaseModel]], ...] = (
        (
            ODOO_PROD_PROMOTION_OPERATIONS_ROUTE,
            enqueue_promotion,
            "POST",
            "enqueue_odoo_prod_promotion",
            OdooProdPromotionOperationResponse,
        ),
        (
            ODOO_PROD_PROMOTION_OPERATION_ROUTE,
            read_promotion_operation,
            "GET",
            "read_odoo_prod_promotion_operation",
            OdooProdPromotionOperationResponse,
        ),
        (
            ODOO_PROD_PROMOTION_OPERATION_ROUTE + "/cancel",
            cancel_promotion_operation,
            "POST",
            "cancel_odoo_prod_promotion_operation",
            OdooProdPromotionOperationResponse,
        ),
        (
            ODOO_PROD_ROLLBACK_OPERATIONS_ROUTE,
            enqueue_rollback,
            "POST",
            "enqueue_odoo_prod_rollback",
            OdooProdRollbackOperationResponse,
        ),
        (
            ODOO_PROD_ROLLBACK_OPERATION_ROUTE,
            read_rollback_operation,
            "GET",
            "read_odoo_prod_rollback_operation",
            OdooProdRollbackOperationResponse,
        ),
        (
            ODOO_PROD_ROLLBACK_OPERATION_ROUTE + "/cancel",
            cancel_rollback_operation,
            "POST",
            "cancel_odoo_prod_rollback_operation",
            OdooProdRollbackOperationResponse,
        ),
    )
    for path, handler, method, route_operation_id, response_model in routes:
        app.add_api_route(
            path,
            handler,
            methods=[method],
            response_model=response_model,
            operation_id=route_operation_id,
            responses={
                status: {"model": common.error_response_model}
                for status in (400, 401, 403, 404, 409, 503)
            },
        )
