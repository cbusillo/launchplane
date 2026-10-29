"""Governed edge publication for an existing independent Odoo testing generation."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, NoReturn

import click
from fastapi import Depends, Header, Path as PathParameter

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
from control_plane.testing_public_ingress import (
    ROUTE,
    TestingIngressPrerequisiteError,
    TestingPublicIngressAdapter,
    TestingPublicIngressRequest,
)
from control_plane.workflows.ingress_provider import build_npmplus_ingress_client_from_env


def register_testing_public_ingress_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
    root: Path,
    run_provider_mutation: Callable[..., Awaitable[AcceptedEvidenceResponse]],
) -> None:
    async def publish(
        product: Annotated[str, PathParameter(min_length=1, pattern=r"^\S+$")],
        body: TestingPublicIngressRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()

        def refuse(status: int, code: str, message: str) -> NoReturn:
            raise dependencies.http_error(
                status_code=status, trace_id=trace, code=code, message=message
            )

        # The same existing Director-agent boundary as fresh testing preparation.
        # This is not an extension of generic instance-scoped ingress authority.
        if not director_agent_identity(identity):
            refuse(
                403, "authorization_denied", "Testing public ingress requires the Director agent."
            )
        if not isinstance(record_store, PostgresRecordStore):
            refuse(503, "database_storage_required", "Testing ingress requires database records.")
        if body.mode != "dry-run" and record_store.database_dialect_name != "postgresql":
            refuse(503, "postgresql_required", "Testing ingress requires PostgreSQL fencing.")
        if body.mode != "dry-run" and not body.reviewed_plan_sha256:
            refuse(400, "review_required", "Keep the reviewed testing ingress plan.")
        if body.mode == "apply" and not idempotency_key.strip():
            refuse(400, "idempotency_key_required", "Keep the original testing ingress key.")
        try:
            adapter = TestingPublicIngressAdapter(
                root, record_store, product, body, trace, build_npmplus_ingress_client_from_env()
            )
            if body.mode == "apply":
                return await run_provider_mutation(
                    record_store=record_store,
                    identity=identity,
                    route_path=ROUTE.format(product=product),
                    idempotency_key=idempotency_key.strip(),
                    request_fingerprint=request_fingerprint(
                        {"product": product, **body.model_dump(mode="json")}
                    ),
                    trace_id=trace,
                    adapter=adapter,
                    in_progress_message="Testing edge publication is running under its original key.",
                    reconcile_message="Testing edge outcome is unknown; retain the original plan and key.",
                )
            result = await run_in_worker_thread(
                adapter.plan if body.mode == "dry-run" else adapter.readback
            )
            return accepted_evidence_response(trace_id=trace, records={}, result=result)
        except TestingIngressPrerequisiteError as error:
            refuse(
                409,
                error.code,
                "Testing edge prerequisite is unverified; preserve the original plan and key.",
            )
        except (ValueError, FileNotFoundError, RuntimeError, OSError, click.ClickException):
            refuse(
                409,
                "testing_public_ingress_unverified",
                "Testing public ingress is unverified; preserve the current generation, reviewed plan and original key.",
            )

    app.add_api_route(
        ROUTE,
        publish,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="prepare_testing_public_ingress",
        summary="Publish one independent testing hostname at its existing public edge",
        responses={
            code: {"model": dependencies.error_response_model} for code in (400, 401, 403, 409, 503)
        },
    )
