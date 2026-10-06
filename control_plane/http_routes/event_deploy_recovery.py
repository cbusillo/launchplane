"""Read-only acquisition of one service-owned held event deploy reference."""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends

from control_plane.contracts.generic_web_deploy_recovery import (
    GenericWebDeployRecoveryReferenceResponse,
)
from control_plane.generic_web_deploy_recovery_http import (
    GenericWebDeployRecoveryDependencies,
    authorized_event_deploy_coordinates,
)
from control_plane.http_routes.generic_web import GenericWebWriteRouteDependencies
from control_plane.http_routes.support import ApiRouteRegistrar
from control_plane.service_auth import LaunchplaneIdentity


def register_event_deploy_recovery_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: GenericWebWriteRouteDependencies,
    read_identity: Callable[..., LaunchplaneIdentity],
) -> None:
    recovery_dependencies = GenericWebDeployRecoveryDependencies(
        read_write_identity=dependencies.read_write_identity,
        get_record_store=dependencies.get_record_store,
        next_trace_id=dependencies.next_trace_id,
        authorization_allows=dependencies.authorization_allows,
        http_error=dependencies.http_error,
        control_plane_root=dependencies.control_plane_root,
        idempotency_request_fingerprint=dependencies.idempotency_request_fingerprint,
    )

    def read_event_deploy_recovery_reference(
        product: str,
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> GenericWebDeployRecoveryReferenceResponse:
        coordinates = authorized_event_deploy_coordinates(
            product=product,
            identity=identity,
            record_store=record_store,
            dependencies=recovery_dependencies,
            trace_id=dependencies.next_trace_id(),
            apply=False,
        )
        return GenericWebDeployRecoveryReferenceResponse(
            product=product,
            context=coordinates.context,
            recovery_reference=coordinates.reference,
            reservation_state="running"
            if coordinates.reservation.state == "running"
            else "reconcile_required",
            reservation_attempt=coordinates.reservation.attempt,
        )

    app.add_api_route(
        "/v1/admin/generic-web/deploy-recovery/{product}/testing",
        read_event_deploy_recovery_reference,
        methods=["GET"],
        response_model=GenericWebDeployRecoveryReferenceResponse,
        operation_id="read_event_deploy_recovery_reference",
        responses={
            code: {"model": dependencies.error_response_model} for code in (401, 403, 404, 409, 503)
        },
    )
