"""Same-identity activation of a qualified independent generic-web testing recipient."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import click
from fastapi import Depends, Header, Path as PathParameter

from control_plane.contracts.testing_activation import (
    TestingActivationRequest,
    TestingActivationResult,
    TestingActivationRefusal,
)
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
from control_plane.service_auth import LaunchplaneIdentity, director_agent_identity
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_activation import ROUTE, TestingActivationAdapter, plan


def register_testing_activation_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
    actor: Callable[[LaunchplaneIdentity], str],
    root: Path,
    run_provider_mutation: Callable[..., Awaitable[AcceptedEvidenceResponse]],
) -> None:
    async def activate(
        product: Annotated[str, PathParameter(min_length=1, pattern=r"^\S+$")],
        body: TestingActivationRequest,
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
                message="Testing activation requires the Director agent.",
            )
        if not isinstance(record_store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="database_storage_required",
                message="Testing activation requires service-owned records.",
            )
        scope = idempotency_scope(identity)
        adapter = TestingActivationAdapter(
            root, record_store, product, body, actor(identity), scope, trace
        )
        try:
            if body.mode == "receipt":
                row = record_store.read_idempotency_record(
                    scope=scope,
                    route_path=ROUTE.format(product=product),
                    idempotency_key=idempotency_key.strip(),
                )
                if row is None or row.state != "completed":
                    raise ValueError("The original activation has no completed receipt.")
                result = TestingActivationResult.model_validate(row.response_payload.get("result"))
                if (result.product, result.context) != (product, body.context):
                    raise ValueError("The receipt belongs to another lane.")
                return accepted_evidence_response(
                    trace_id=trace,
                    records={"activation": row.record_id},
                    result=result.model_dump(mode="json"),
                )
            if body.mode == "dry-run":
                result_payload = await run_in_worker_thread(lambda: plan(adapter.current()))
            elif body.mode == "readback":

                def readback() -> dict[str, object]:
                    record = adapter.current()
                    if not record.published:
                        raise ValueError("Activation has no qualified published recipient.")
                    adapter.require_unchanged(record)
                    adapter.verify_provider(record)
                    return adapter.result(record)

                result_payload = await run_in_worker_thread(readback)
            else:
                if record_store.database_dialect_name != "postgresql":
                    raise dependencies.http_error(
                        status_code=503,
                        trace_id=trace,
                        code="postgresql_required",
                        message="Testing activation requires PostgreSQL execution guards.",
                    )
                if not idempotency_key.strip() or not body.reviewed_plan_sha256:
                    raise dependencies.http_error(
                        status_code=400,
                        trace_id=trace,
                        code="review_required",
                        message="Apply requires the original key and reviewed plan.",
                    )
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
                    in_progress_message="Testing activation is running under its original key.",
                    reconcile_message="Testing activation outcome is unknown; preserve original placement, holds and fences.",
                )
        except TestingActivationRefusal as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code=error.public_code,
                message="Testing activation qualification refused; preserve the original resources and interlock.",
            ) from error
        except (
            ValueError,
            FileNotFoundError,
            RuntimeError,
            OSError,
            click.ClickException,
        ) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="testing_activation_dependency_unverified_" + type(error).__name__.lower(),
                message="Testing activation inputs, recipient or isolation proof remain unverified.",
            ) from error
        return accepted_evidence_response(trace_id=trace, records={}, result=result_payload)

    app.add_api_route(
        ROUTE,
        activate,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="activate_independent_generic_web_testing",
        summary="Activate a qualified independent testing recipient while retaining historical effects",
        responses={
            code: {"model": dependencies.error_response_model} for code in (400, 401, 403, 409, 503)
        },
    )
