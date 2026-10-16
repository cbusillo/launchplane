"""Service-owned stored-setting review, apply and value-free readback."""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Header, Response
from pydantic import BaseModel, ConfigDict

from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.runtime_setting_removal import ROUTE, RuntimeSettingRemovalRequest
from control_plane.service_auth import LaunchplaneIdentity
from control_plane.storage.postgres import PostgresRecordStore


class RuntimeSettingRemovalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = "ok"
    trace_id: str
    result: dict[str, object]


def register_runtime_setting_removal_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
) -> None:
    def removal(
        body: RuntimeSettingRemovalRequest,
        response: Response,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> RuntimeSettingRemovalResponse:
        trace = dependencies.next_trace_id()
        response.headers["Cache-Control"] = "no-store"
        if not isinstance(record_store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="database_storage_required",
                message="Removal requires shared database custody.",
            )
        try:
            result = record_store.runtime_setting_removal(
                request=body,
                identity=identity,
                trace_id=trace,
                idempotency_key=idempotency_key,
            )
        except PermissionError:
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace,
                code="authorization_denied",
                message="Identity cannot remove or review these settings.",
            ) from None
        except (ValueError, FileNotFoundError):
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="runtime_setting_removal_unverified",
                message="Refresh the exact review or read the original removal receipt; no retry under a new key.",
            ) from None
        return RuntimeSettingRemovalResponse(trace_id=trace, result=result)

    app.add_api_route(
        ROUTE,
        removal,
        methods=["POST"],
        response_model=RuntimeSettingRemovalResponse,
        operation_id="runtime_setting_removal",
        summary="Review, remove and read back exact stored plain-setting keys",
        responses={
            code: {"model": dependencies.error_response_model} for code in (401, 403, 409, 503)
        },
    )
