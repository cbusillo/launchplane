"""Production preparation keeps the existing setup authority and serving binding."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import click
from fastapi import Depends, Header, Path as PathParameter

from control_plane.contracts.production_recipient import (
    ProductionRecipientPlan,
    ProductionRecipientRequest,
)
from control_plane.http_routes.mutation_support import (
    AcceptedEvidenceResponse,
    accepted_evidence_response,
    idempotency_scope,
    request_fingerprint,
)
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    LAUNCHPLANE_SERVICE_CONTEXT,
    ReadRouteDependencies,
    run_in_worker_thread,
)
from control_plane.production_recipient import ROUTE, ProductionRecipientAdapter, custody, plan
from control_plane.service_auth import (
    AuthorizationTarget,
    LaunchplaneIdentity,
    TerminalAgentIdentity,
)
from control_plane.storage.postgres import PostgresRecordStore


def register_production_recipient_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
    actor: Callable[[LaunchplaneIdentity], str],
    root: Path,
    run_provider_mutation: Callable[..., Awaitable[AcceptedEvidenceResponse]],
) -> None:
    async def prepare(
        product: Annotated[str, PathParameter(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")],
        body: ProductionRecipientRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()
        apply = body.mode == "apply"
        allowed = dependencies.authorization_allows(
            identity=identity,
            action="dokploy_target.setup" if apply else "product_profile.read",
            product=product,
            context=LAUNCHPLANE_SERVICE_CONTEXT if apply else body.context,
            target=None if apply else AuthorizationTarget(scope="instance", instances=("prod",)),
        )
        # Match production target setup: terminal identities do not inherit
        # service-scoped creation authority from the disposable testing path.
        if not allowed or (apply and isinstance(identity, TerminalAgentIdentity)):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace,
                code="authorization_denied",
                message="Existing production target setup authority is required for preparation.",
            )
        if not isinstance(record_store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="database_storage_required",
                message="Service-owned production custody is required.",
            )
        scope = idempotency_scope(identity)
        adapter = ProductionRecipientAdapter(
            root,
            record_store,
            product,
            body,
            actor(identity),
            scope,
            trace,
            idempotency_key.strip(),
        )
        try:
            if body.mode == "receipt":
                if not idempotency_key.strip():
                    raise ValueError("The original preparation key is required.")
                row = record_store.read_idempotency_record(
                    scope=scope,
                    route_path=ROUTE.format(product=product),
                    idempotency_key=idempotency_key.strip(),
                )
                if row is None or row.state != "completed":
                    raise ValueError("No completed receipt exists for this original key.")
                result = ProductionRecipientPlan.model_validate(row.response_payload.get("result"))
                if (result.product, result.context) != (product, body.context):
                    raise ValueError("The receipt belongs to another lane.")
                return accepted_evidence_response(
                    trace_id=trace, records={}, result=result.model_dump(mode="json")
                )
            if apply:
                if record_store.database_dialect_name != "postgresql":
                    raise dependencies.http_error(
                        status_code=503,
                        trace_id=trace,
                        code="postgresql_required",
                        message="Production preparation requires cross-process execution and authority guards.",
                    )
                if not idempotency_key.strip() or not body.reviewed_plan_sha256:
                    raise dependencies.http_error(
                        status_code=400,
                        trace_id=trace,
                        code="review_required",
                        message="Apply requires the saved plan and original stable key.",
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
                    in_progress_message="Production preparation remains under its original key.",
                    reconcile_message="Retain both recipients and the original key; do not activate or delete anything.",
                )

            def read() -> dict[str, object]:
                record = adapter.current()
                adapter.require_unchanged(record)
                if body.mode == "readback":
                    if (
                        custody(record_store, product, body.context) is None
                        or not record.qualified_at
                    ):
                        raise ValueError("A completed qualified recipient is required.")
                    adapter.verify(record)
                    from control_plane.production_recipient_resources import inspect_resources

                    inspect_resources(
                        root,
                        record,
                        prepare=False,
                        checkpoint=lambda phase: adapter.require_unchanged(record),
                    )
                return plan(record)

            result_payload = await run_in_worker_thread(read)
            return accepted_evidence_response(trace_id=trace, records={}, result=result_payload)
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
                code="production_recipient_unverified",
                message="Preparation or readback refused; retain both recipients, the old binding and the original key.",
            ) from error

    app.add_api_route(
        ROUTE,
        prepare,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="prepare_independent_production_recipient",
        summary="Prepare a fresh private production recipient without changing the serving binding",
        responses={
            code: {"model": dependencies.error_response_model} for code in (400, 401, 403, 409, 503)
        },
    )
