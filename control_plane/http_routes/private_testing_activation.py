"""Service-owned reviewed private testing Compose source installation/recovery."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import click
from fastapi import Depends, Header, Path as PathParameter
from control_plane.build_provenance import BuildProvenanceError
from control_plane.github_app_identity import GitHubAppIdentityError

from control_plane.http_routes.mutation_support import (
    AcceptedEvidenceResponse,
    accepted_evidence_response,
    idempotency_scope,
    request_fingerprint,
)
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    ReadRouteDependencies,
    run_in_worker_thread,
)
from control_plane.private_testing_activation import (
    ROUTE,
    PrivateTestingActivationAdapter,
    PrivateTestingActivationRequest,
)
from control_plane.private_testing_transport import PRODUCT_PATTERN
from control_plane.provider_operations import (
    DurableProviderOperationResult,
    run_durable_provider_operation,
)
from control_plane.service_auth import (
    AuthorizationTarget,
    LaunchplaneIdentity,
    director_agent_identity,
)
from control_plane.storage.postgres import PostgresRecordStore


def register_private_testing_activation_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
    root: Path,
    run_provider_operation_http: Callable[..., Awaitable[AcceptedEvidenceResponse]],
) -> None:
    async def activate(
        product: Annotated[str, PathParameter(min_length=1, pattern=PRODUCT_PATTERN)],
        body: PrivateTestingActivationRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()
        if (body.mode == "apply" and not director_agent_identity(identity)) or (
            body.mode != "apply"
            and not dependencies.authorization_allows(
                identity=identity,
                action="product_config.plan",
                product=product,
                context=body.context,
                target=AuthorizationTarget(scope="instance", instances=(body.instance,)),
            )
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace,
                code="authorization_denied",
                message="Private testing source operation is not authorized for this caller and lane.",
            )
        if not isinstance(store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="database_storage_required",
                message="Service-owned source custody is required.",
            )
        if body.mode != "dry-run" and (
            not idempotency_key.strip() or not body.expected_plan_sha256
        ):
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace,
                code="review_required",
                message="Use the reviewed digest and original operation key.",
            )
        scope = idempotency_scope(identity)
        try:
            adapter = await run_in_worker_thread(
                lambda: PrivateTestingActivationAdapter.prepare(
                    root,
                    store,
                    product,
                    body,
                    scope,
                    trace,
                    idempotency_key.strip() if body.mode != "dry-run" else "",
                )
            )
            if body.mode == "dry-run":
                return accepted_evidence_response(
                    trace_id=trace, records={}, result=adapter.result(matches=False)
                )
            if body.mode == "readback":

                def readback() -> tuple[str, dict[str, object]]:
                    row = store.read_idempotency_record(
                        scope=scope,
                        route_path=ROUTE.format(product=product),
                        idempotency_key=idempotency_key.strip(),
                    )
                    if row is None:
                        raise ValueError("Original source operation is unavailable.")
                    return row.record_id, adapter.readback()

                record_id, result = await run_in_worker_thread(readback)
                return accepted_evidence_response(
                    trace_id=trace, records={"operation": record_id}, result=result
                )
            if store.database_dialect_name != "postgresql":
                raise dependencies.http_error(
                    status_code=503,
                    trace_id=trace,
                    code="postgresql_required",
                    message="Provider execution requires PostgreSQL fences.",
                )

            def operation(record_store: PostgresRecordStore) -> DurableProviderOperationResult:
                return run_durable_provider_operation(
                    store=record_store,
                    scope=scope,
                    route_path=ROUTE.format(product=product),
                    idempotency_key=idempotency_key.strip(),
                    request_fingerprint=request_fingerprint(
                        {
                            "product": product,
                            "request": adapter.intent["request"],
                            "plan": adapter.intent["plan_sha256"],
                        }
                    ),
                    lease_owner=trace,
                    response_trace_id=trace,
                    adapter=adapter,
                    original_intent=adapter.intent,
                )

            return await run_provider_operation_http(
                record_store=store,
                route_path=ROUTE.format(product=product),
                trace_id=trace,
                operation=operation,
                in_progress_message="Private testing source is running under its original key.",
                reconcile_message="Source outcome is uncertain; preserve the original key and inspect original readback.",
            )
        except (
            GitHubAppIdentityError,
            click.ClickException,
            OSError,
            BuildProvenanceError,
            RuntimeError,
        ) as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="private_testing_activation_provider_unavailable",
                message="Provider/source evidence is unavailable; preserve any original operation key.",
            ) from error
        except ValueError as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="private_testing_activation_unqualified",
                message="Testing source, isolation, custody review or original operation is unqualified.",
            ) from error

    app.add_api_route(
        ROUTE,
        activate,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        operation_id="activate_private_testing_transport_source",
        summary="Install/remove reviewed private testing source; deploy with the existing artifact driver",
        responses={
            code: {"model": dependencies.error_response_model} for code in (400, 401, 403, 409, 503)
        },
    )
