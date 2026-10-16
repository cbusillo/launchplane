"""Read-only private transport planning; no provider mutation route."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import click
from fastapi import Depends, Path as PathParameter

from control_plane.http_routes.mutation_support import (
    AcceptedEvidenceResponse,
    accepted_evidence_response,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.private_testing_transport import (
    ROUTE,
    PRODUCT_PATTERN,
    PrivateTestingTransportRequest,
    plan_private_testing_transport,
)
from control_plane.service_auth import AuthorizationTarget, LaunchplaneIdentity
from control_plane.storage.postgres import PostgresRecordStore


def register_private_testing_transport_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
    root: Path,
) -> None:
    def plan(
        product: Annotated[str, PathParameter(min_length=1, pattern=PRODUCT_PATTERN)],
        body: PrivateTestingTransportRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()
        if not dependencies.authorization_allows(
            identity=identity,
            action="product_config.plan",
            product=product,
            context=body.context,
            target=AuthorizationTarget(scope="instance", instances=(body.instance,)),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace,
                code="authorization_denied",
                message="Private transport planning is not authorized.",
            )
        if not isinstance(store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="database_storage_required",
                message="Service-owned target records are required.",
            )
        try:
            result = plan_private_testing_transport(store, root=root, product=product, request=body)
        except (ValueError, FileNotFoundError) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="private_testing_transport_unqualified",
                message="Private testing target, source, isolation or review is unqualified.",
            ) from error
        except (click.ClickException, OSError) as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="private_testing_transport_provider_unavailable",
                message="Provider evidence is unavailable; planning wrote nothing and can be retried.",
            ) from error
        return accepted_evidence_response(trace_id=trace, records={}, result=result)

    app.add_api_route(
        ROUTE,
        plan,
        methods=["POST"],
        response_model=AcceptedEvidenceResponse,
        operation_id="plan_private_testing_transport",
        summary="Plan or read back isolated private testing HTTPS/WSS configuration without changes",
        responses={
            code: {"model": dependencies.error_response_model} for code in (401, 403, 409, 503)
        },
    )
