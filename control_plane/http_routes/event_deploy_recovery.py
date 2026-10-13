"""Read-only acquisition of one service-owned held event deploy reference."""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.exc import SQLAlchemyError

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
from control_plane.service_auth import AuthorizationTarget
from control_plane.contracts.generic_web_testing_exclusion import (
    GENERIC_WEB_TESTING_EXCLUSION_ROUTE,
    GenericWebTestingExclusionResponse,
)
from control_plane.generic_web_testing_exclusion import (
    inspect_generic_web_testing_exclusion,
    TestingExclusionRefused,
)
from control_plane.generic_web_deploy_http import (
    GenericWebDeployProductMismatchError,
    GenericWebDeployRouteDependencyError,
)
from control_plane.product_config import ProductConfigError
from control_plane.storage.postgres import PostgresRecordStore


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

    def read_testing_exclusion(
        product: Annotated[str, Query(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")],
        context: Annotated[str, Query(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        instance: str = "testing",
    ) -> GenericWebTestingExclusionResponse:
        trace_id = dependencies.next_trace_id()
        product, context, instance = product.strip(), context.strip(), instance.strip()
        if not dependencies.authorization_allows(
            identity=identity,
            action="product_environment.read",
            product=product,
            context=context,
            target=AuthorizationTarget(scope="instance", instances=(instance,)),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot inspect this testing lane.",
            )
        if not isinstance(record_store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Testing exclusion requires database storage.",
            )
        try:
            return inspect_generic_web_testing_exclusion(
                store=record_store,
                root=dependencies.control_plane_root,
                product=product,
                context=context,
                instance=instance,
                trace_id=trace_id,
            )
        except TestingExclusionRefused as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code=error.code,
                message="Testing exclusion scope could not be proved.",
            ) from error
        except (ProductConfigError, GenericWebDeployProductMismatchError) as error:
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="testing_exclusion_lane_not_owned",
                message="Product does not exclusively own this generic-web testing lane.",
            ) from error
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="testing_exclusion_binding_missing",
                message="Testing exclusion requires complete tracked target records.",
            ) from error
        except (SQLAlchemyError, GenericWebDeployRouteDependencyError) as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="testing_exclusion_storage_unavailable",
                message="Testing exclusion storage could not be inspected.",
            ) from error
        except ValueError as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="testing_exclusion_evidence_unavailable",
                message="Testing exclusion evidence could not be proved.",
            ) from error

    app.add_api_route(
        GENERIC_WEB_TESTING_EXCLUSION_ROUTE,
        read_testing_exclusion,
        methods=["GET"],
        response_model=GenericWebTestingExclusionResponse,
        operation_id="read_generic_web_testing_exclusion",
        responses={
            code: {"model": dependencies.error_response_model} for code in (401, 403, 409, 503)
        },
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
