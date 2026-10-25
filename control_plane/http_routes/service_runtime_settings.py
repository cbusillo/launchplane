from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Header, Response
from starlette.concurrency import run_in_threadpool

from control_plane.http_routes.mutation_support import idempotency_scope
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.service_auth import AuthorizationTarget, LaunchplaneIdentity
from control_plane.service_runtime_settings import (
    SERVICE_CONTEXT,
    SERVICE_RUNTIME_SETTINGS_APPLY_ACTION,
    SERVICE_RUNTIME_SETTINGS_PLAN_ACTION,
    SERVICE_RUNTIME_SETTINGS_ROUTE,
    ServiceRuntimeSettingRequest,
    ServiceRuntimeSettingResponse,
    apply_service_runtime_setting,
)
from control_plane.storage.postgres import PostgresRecordStore


def register_service_runtime_settings_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
) -> None:
    async def set_service_runtime_setting(
        body: ServiceRuntimeSettingRequest,
        response: Response,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> dict[str, object]:
        trace_id = dependencies.next_trace_id()
        response.headers["Cache-Control"] = "no-store"
        if not dependencies.authorization_allows(
            identity=identity,
            action=SERVICE_RUNTIME_SETTINGS_APPLY_ACTION
            if body.mode == "apply"
            else SERVICE_RUNTIME_SETTINGS_PLAN_ACTION,
            product=SERVICE_CONTEXT,
            context=SERVICE_CONTEXT,
            target=AuthorizationTarget(scope="context"),
            inherit_agent_read_role=True,
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot change Launchplane service settings.",
            )
        if not isinstance(record_store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Service settings require shared database storage.",
            )
        try:
            return await run_in_threadpool(
                apply_service_runtime_setting,
                store=record_store,
                request=body,
                actor=idempotency_scope(identity),
                trace_id=trace_id,
                idempotency_key=idempotency_key.strip(),
            )
        except (ValueError, FileNotFoundError) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="service_runtime_setting_conflict",
                message="The service setting is unavailable, stale, or conflicts with its reviewed request.",
            ) from error

    app.add_api_route(
        SERVICE_RUNTIME_SETTINGS_ROUTE,
        set_service_runtime_setting,
        methods=["POST"],
        response_model=ServiceRuntimeSettingResponse,
        operation_id="set_launchplane_service_runtime_setting",
        summary="Review and set a declared non-secret Launchplane service setting",
        responses={
            code: {"model": dependencies.error_response_model} for code in (401, 403, 409, 503)
        },
    )
