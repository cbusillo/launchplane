"""Director-agent continuation of the exact fresh Odoo testing generation."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, NoReturn

import click
from fastapi import Depends, Header, Path as PathParameter

from control_plane.durable_operation_authorization import DurableOperationAuthorizationDeniedError
from control_plane.http_routes.mutation_support import (
    AcceptedEvidenceResponse,
    accepted_evidence_response,
    request_fingerprint,
)
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    ReadRouteDependencies,
    run_in_worker_thread,
)
from control_plane.service_auth import LaunchplaneIdentity, director_agent_identity
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_replacement_bootstrap import (
    ROUTE,
    TestingReplacementBootstrapAdapter,
    TestingReplacementBootstrapRequest,
    TestingBootstrapPrerequisiteError,
    bootstrap_plan,
    bootstrap_plan_payload,
    bootstrap_wire_payload,
)


def register_testing_replacement_bootstrap_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
    root: Path,
    run_provider_mutation: Callable[..., Awaitable[AcceptedEvidenceResponse]],
) -> None:
    async def bootstrap(
        product: Annotated[str, PathParameter(min_length=1, pattern=r"^\S+$")],
        body: TestingReplacementBootstrapRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()

        def refuse(status: int, code: str, message: str) -> NoReturn:
            raise dependencies.http_error(
                status_code=status, trace_id=trace, code=code, message=message
            )

        if not director_agent_identity(identity):
            refuse(403, "authorization_denied", "Testing bootstrap requires the Director agent.")
        if not isinstance(record_store, PostgresRecordStore):
            refuse(503, "database_storage_required", "Testing bootstrap requires database records.")
        assert isinstance(record_store, PostgresRecordStore)
        if body.mode != "dry-run" and record_store.database_dialect_name != "postgresql":
            refuse(
                503, "postgresql_required", "Testing bootstrap needs PostgreSQL concurrency guards."
            )
        adapter = TestingReplacementBootstrapAdapter(root, record_store, product, body, trace)
        try:
            if body.mode.startswith("route-repair-"):
                from control_plane.testing_bootstrap_route_repair import TestingBootstrapRouteRepair

                repair = TestingBootstrapRouteRepair(adapter)
                if body.mode == "route-repair-dry-run":
                    result = await run_in_worker_thread(repair.plan)
                elif body.mode == "route-repair-readback":
                    result = await run_in_worker_thread(repair.readback)
                else:
                    if not idempotency_key.strip() or not body.reviewed_plan_sha256:
                        refuse(
                            400,
                            "review_required",
                            "Certificate repair requires its reviewed plan and separate original repair key.",
                        )
                    return await run_provider_mutation(
                        record_store=record_store,
                        identity=identity,
                        route_path=ROUTE.format(product=product),
                        idempotency_key=idempotency_key.strip(),
                        request_fingerprint=request_fingerprint(
                            {"product": product, **bootstrap_wire_payload(body)}
                        ),
                        trace_id=trace,
                        adapter=repair,
                        in_progress_message="Original testing certificate repair is running under its repair key.",
                        reconcile_message="Certificate repair has an unknown outcome; preserve its repair key and original bootstrap custody.",
                    )
            elif body.mode == "dry-run":
                try:
                    result = await run_in_worker_thread(
                        lambda: bootstrap_plan_payload(bootstrap_plan(record_store, product, body))
                    )
                except TestingBootstrapPrerequisiteError as error:
                    refuse(409, error.code, str(error))
            elif body.mode == "readback":
                result = await run_in_worker_thread(adapter.readback)
            else:
                if not idempotency_key.strip() or not body.reviewed_plan_sha256:
                    refuse(
                        400,
                        "review_required",
                        "Apply requires the reviewed bootstrap plan and original key.",
                    )
                return await run_provider_mutation(
                    record_store=record_store,
                    identity=identity,
                    route_path=ROUTE.format(product=product),
                    idempotency_key=idempotency_key.strip(),
                    request_fingerprint=request_fingerprint(
                        {"product": product, **bootstrap_wire_payload(body)}
                    ),
                    trace_id=trace,
                    adapter=adapter,
                    in_progress_message="Fresh testing bootstrap is running under its original key.",
                    reconcile_message="Bootstrap has an unknown outcome; inspect the retained generation with its original key.",
                )
        except DurableOperationAuthorizationDeniedError:
            refuse(
                403,
                "authorization_denied",
                (
                    "The provider refused the testing usage read. Preserve the trace, repair key and original bootstrap custody."
                    if body.mode.startswith("route-repair-")
                    else "The provider refused the Odoo testing usage read. Preserve the trace and original bootstrap key."
                ),
            )
        except (ValueError, FileNotFoundError, RuntimeError, OSError, click.ClickException):
            refuse(
                409,
                "testing_bootstrap_unverified",
                (
                    "Testing certificate repair is unverified. Inspect its repair key and preserve the original bootstrap generation and key."
                    if body.mode.startswith("route-repair-")
                    else "Fresh testing bootstrap is unverified. Inspect its original key and exact generation."
                ),
            )
        return accepted_evidence_response(
            trace_id=trace, records={"operation": body.operation_id}, result=result
        )

    app.add_api_route(
        ROUTE,
        bootstrap,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="prepare_fresh_testing_bootstrap",
        summary="Prepare the exact fresh Odoo testing restore generation",
        responses={
            code: {"model": dependencies.error_response_model}
            for code in (400, 401, 403, 404, 409, 503)
        },
    )
