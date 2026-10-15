from collections.abc import Callable
import time
from typing import Annotated

from fastapi import Depends, Query
from pydantic import BaseModel

from control_plane.health_alert_drill import (
    DRILL_ROUTE,
    HealthAlertDrillEvidence,
    HealthAlertDrillRequest,
    HealthAlertDrillScope,
    HealthAlertDrillWindowError,
    health_alert_drill,
)
from control_plane.http_routes.support import (
    LAUNCHPLANE_SERVICE_CONTEXT,
    ApiRouteRegistrar,
    ReadRouteDependencies,
    run_in_worker_thread,
)
from control_plane.service_auth import AuthorizationTarget, LaunchplaneIdentity
from control_plane.storage.postgres import PostgresRecordStore


class HealthAlertDrillResponse(BaseModel):
    trace_id: str
    drill: HealthAlertDrillEvidence


def register_health_alert_drill_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
) -> None:
    def require_access(
        identity: LaunchplaneIdentity, scope: HealthAlertDrillScope, trace: str, *, send: bool
    ) -> None:
        if not dependencies.authorization_allows(
            identity=identity,
            action="product_environment.read",
            product=scope.product,
            context=scope.context,
            target=AuthorizationTarget(scope="instance", instances=(scope.instance,)),
        ) or (
            send
            and not dependencies.authorization_allows(
                identity=identity,
                action="public_ingress_monitor.run_once",
                product=scope.product,
                context=LAUNCHPLANE_SERVICE_CONTEXT,
            )
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace,
                code="authorization_denied",
                message="Caller lacks existing scoped read or monitor execution access.",
            )

    async def execute(
        store: object,
        scope: HealthAlertDrillScope,
        trace: str,
        *,
        send: bool = False,
        window: int | None = None,
    ) -> HealthAlertDrillResponse:
        if not isinstance(store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="database_storage_required",
                message="Health alert drills require database evidence storage.",
            )
        try:
            evidence = await run_in_worker_thread(
                lambda: health_alert_drill(
                    store, scope=scope, now=time.time(), send=send, window=window
                )
            )
        except HealthAlertDrillWindowError as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace,
                code="invalid_drill_window",
                message="Drill evidence must name a current or past window.",
            ) from error
        except Exception as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="drill_evidence_unavailable",
                message="Drill evidence is unavailable; read the same window before retrying.",
            ) from error
        return HealthAlertDrillResponse(trace_id=trace, drill=evidence)

    async def read_drill(
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        product: Annotated[str, Query(min_length=1, max_length=200)],
        context: Annotated[str, Query(min_length=1, max_length=200)],
        instance: Annotated[str, Query(min_length=1, max_length=200)],
        check_name: Annotated[str, Query(min_length=1, max_length=200)],
        window: Annotated[int | None, Query(ge=0)] = None,
    ) -> HealthAlertDrillResponse:
        trace = dependencies.next_trace_id()
        try:
            scope = HealthAlertDrillScope(
                product=product, context=context, instance=instance, check_name=check_name
            )
        except ValueError as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace,
                code="invalid_drill_scope",
                message="Drill scope must name a product, lane and check.",
            ) from error
        require_access(identity, scope, trace, send=False)
        return await execute(record_store, scope, trace, window=window)

    async def send_drill(
        payload: HealthAlertDrillRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> HealthAlertDrillResponse:
        trace = dependencies.next_trace_id()
        scope = HealthAlertDrillScope.model_validate(payload.model_dump(exclude={"mode", "window"}))
        require_access(identity, scope, trace, send=payload.mode == "send")
        return await execute(
            record_store, scope, trace, send=payload.mode == "send", window=payload.window
        )

    errors = {code: {"model": dependencies.error_response_model} for code in (400, 401, 403, 503)}
    app.add_api_route(
        DRILL_ROUTE,
        read_drill,
        methods=["GET"],
        response_model=HealthAlertDrillResponse,
        operation_id="read_health_alert_drill",
        summary="Read effective Discord routing and a drill receipt",
        responses=errors,
    )
    app.add_api_route(
        DRILL_ROUTE,
        send_drill,
        methods=["POST"],
        response_model=HealthAlertDrillResponse,
        operation_id="run_health_alert_drill",
        summary="Dry-run or send a non-disruptive health alert drill",
        responses=errors,
    )
