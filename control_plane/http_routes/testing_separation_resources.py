"""Create independent testing storage without releasing a retained interlock."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Header

from control_plane.http_routes.mutation_support import (
    AcceptedEvidenceResponse,
    accepted_evidence_response,
    request_fingerprint,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.service_auth import LaunchplaneIdentity, director_agent_identity
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_separation_resources import (
    ROUTE,
    SeparationResourcesAdapter,
    SeparationResourcesRefused,
    SeparationResourcesRequest,
)


def register_testing_separation_resource_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
    actor: Callable[[LaunchplaneIdentity], str],
    root: Path,
    run_provider_mutation: Callable[..., Awaitable[AcceptedEvidenceResponse]],
) -> None:
    async def prepare(
        product: str,
        body: SeparationResourcesRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()
        if not director_agent_identity(identity):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace,
                code="authorization_denied",
                message="Independent testing preparation requires the Director agent.",
            )
        if not isinstance(record_store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="database_storage_required",
                message="Independent preparation requires database storage.",
            )
        adapter = SeparationResourcesAdapter(
            root, record_store, product, body, actor(identity), trace
        )
        try:
            if body.mode == "dry-run":
                return accepted_evidence_response(trace_id=trace, records={}, result=adapter.plan())
            if record_store.database_dialect_name != "postgresql":
                raise dependencies.http_error(
                    status_code=503,
                    trace_id=trace,
                    code="postgresql_required",
                    message="Independent preparation requires PostgreSQL execution guards.",
                )
            if not idempotency_key.strip() or not body.reviewed_plan_sha256:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace,
                    code="review_required",
                    message="Apply requires saved review evidence and its original key.",
                )
            return await run_provider_mutation(
                record_store=record_store,
                identity=identity,
                route_path=ROUTE.replace("{product}", product),
                idempotency_key=idempotency_key.strip(),
                request_fingerprint=request_fingerprint(
                    {"product": product, **body.model_dump(mode="json")}
                ),
                trace_id=trace,
                adapter=adapter,
                in_progress_message="Independent testing storage preparation is in progress.",
                reconcile_message="Preparation outcome is unknown; retain its original key and both fences.",
            )
        except (ValueError, FileNotFoundError, SeparationResourcesRefused) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="testing_resources_refused",
                message="Retained testing preparation inputs changed or remain unproved.",
            ) from error

    app.add_api_route(
        ROUTE,
        prepare,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="prepare_independent_testing_storage",
        summary="Prepare independent empty testing storage while preserving the retained lane",
        responses={
            code: {"model": dependencies.error_response_model}
            for code in (400, 401, 403, 404, 409, 503)
        },
    )
