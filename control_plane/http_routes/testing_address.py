from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, NoReturn

import click
from fastapi import Depends, Header, Path as PathParameter

from control_plane.http_routes.mutation_support import (
    AcceptedEvidenceResponse,
    accepted_evidence_response,
    request_fingerprint,
    idempotency_scope,
)
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    ReadRouteDependencies,
    run_in_worker_thread,
)
from control_plane.service_auth import LaunchplaneIdentity, director_agent_identity
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_address import (
    ROUTE,
    TestingAddressRequest,
    TestingAddressAdapter,
    address_plan_result,
)
from control_plane.testing_address_qualification import (
    plan_testing_address,
    TestingAddressRecipientUnverified,
)
from control_plane.testing_address_storage import (
    read_address_plan,
    save_address_plan,
    settled_testing_address,
)
from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied


def register_testing_address_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
    root: Path,
    run_provider_mutation: Callable[..., Awaitable[AcceptedEvidenceResponse]],
) -> None:
    async def amend(
        product: Annotated[str, PathParameter(min_length=1, pattern=r"^\S+$")],
        body: TestingAddressRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()

        def refuse(
            status: int, code: str, message: str, *, cause: BaseException | None = None
        ) -> NoReturn:
            raise dependencies.http_error(
                status_code=status, trace_id=trace, code=code, message=message
            ) from cause

        if not director_agent_identity(identity):
            refuse(
                403,
                "authorization_denied",
                "Testing address amendment requires the Director agent.",
            )
        if not isinstance(record_store, PostgresRecordStore):
            refuse(
                503,
                "database_storage_required",
                "Testing address amendment requires database records.",
            )
        assert isinstance(record_store, PostgresRecordStore)
        database_store = record_store
        if body.mode == "apply" and database_store.database_dialect_name != "postgresql":
            refuse(
                503, "postgresql_required", "Testing address amendment requires PostgreSQL fencing."
            )
        if body.mode != "dry-run" and not body.reviewed_plan_sha256:
            refuse(400, "review_required", "Keep the reviewed testing address plan.")
        if body.mode == "apply" and not idempotency_key.strip():
            refuse(
                400, "idempotency_key_required", "Keep the original testing address operation key."
            )

        def prepare() -> dict[str, object]:
            if body.operation == "cancel":
                refuse(
                    400,
                    "original_review_required",
                    "Cancel uses the original reviewed address plan and key.",
                )
            draft_plan = plan_testing_address(
                root,
                database_store,
                product=product,
                context=body.context,
                retirement_operation_id=body.retirement_operation_id,
                reservation_operation_id=body.reservation_operation_id,
                hostname=body.hostname,
                reason=body.reason,
            )
            save_address_plan(database_store, draft_plan)
            return address_plan_result(draft_plan, applied=False)

        try:
            if body.mode == "dry-run":
                return accepted_evidence_response(
                    trace_id=trace, records={}, result=await run_in_worker_thread(prepare)
                )
            plan = await run_in_worker_thread(
                lambda: read_address_plan(database_store, body.reviewed_plan_sha256)
            )
            if (
                plan.product,
                plan.context,
                plan.retirement_operation_id,
                plan.route_after.host,
            ) != (product, body.context, body.retirement_operation_id, body.hostname) or {
                item.operation_id for item in plan.reservations
            } != {body.reservation_operation_id}:
                raise ValueError("The reviewed address belongs to another request.")
            if body.reason != plan.reason:
                raise ValueError("The reviewed address reason belongs to another request.")
            fingerprint = request_fingerprint(
                {"product": product, **body.model_dump(mode="json", exclude={"mode", "operation"})}
            )
            if body.operation == "cancel":
                original = await run_in_worker_thread(
                    lambda: database_store.read_idempotency_record(
                        scope=idempotency_scope(identity),
                        route_path=ROUTE.format(product=product),
                        idempotency_key=idempotency_key.strip(),
                    )
                )
                if not idempotency_key.strip() or original is None:
                    refuse(
                        400,
                        "original_key_required",
                        "Cancel requires the original address request and key.",
                    )
                if original.request_fingerprint != fingerprint:
                    refuse(
                        409,
                        "original_request_required",
                        "Cancel must preserve the original address request.",
                    )
                if (
                    await run_in_worker_thread(
                        lambda: settled_testing_address(database_store, plan)
                    )
                    is not None
                ):
                    refuse(
                        409,
                        "address_already_published",
                        "Canonical metadata already committed; retrieve the original amend result.",
                    )
                if body.mode == "readback" and (
                    original.state != "completed"
                    or original.response_payload.get("result", {}).get("cancelled") is not True
                ):
                    refuse(
                        409,
                        "address_cancel_pending",
                        "The original address cancellation is not complete.",
                    )
            adapter = TestingAddressAdapter(
                root, database_store, plan, trace, cancel=body.operation == "cancel"
            )
            if body.mode == "readback":
                result = await run_in_worker_thread(
                    adapter.cancel_readback if body.operation == "cancel" else adapter.readback
                )
                return accepted_evidence_response(trace_id=trace, records={}, result=result)
            return await run_provider_mutation(
                record_store=database_store,
                identity=identity,
                route_path=ROUTE.format(product=product),
                idempotency_key=idempotency_key.strip(),
                request_fingerprint=fingerprint,
                trace_id=trace,
                adapter=adapter,
                original_intent={"testing_address_plan": plan.model_dump(mode="json")},
                in_progress_message="Testing address amendment is running under its original key.",
                reconcile_message="Testing address outcome needs observation with the original plan and key.",
            )
        except TestingAddressRecipientUnverified as error:
            refuse(409, "testing_address_recipient_unverified", str(error), cause=error)
        except NativeSourceAuthorizationDenied as error:
            refuse(
                403,
                "authorization_denied",
                "The existing provider identity denied testing address observation. Preserve the original request, plan and key.",
                cause=error,
            )
        except (
            ValueError,
            FileNotFoundError,
            RuntimeError,
            OSError,
            click.ClickException,
        ) as error:
            refuse(
                409,
                "testing_address_unverified",
                "The testing address is unverified. Preserve the original plan and key; same-key cancellation can restore an unpublished route without clearing retained operation fences.",
                cause=error,
            )

    app.add_api_route(
        ROUTE,
        amend,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="amend_native_testing_address",
        summary="Amend a native testing lane's canonical address while preserving its original hostname and copy",
        responses={
            code: {"model": dependencies.error_response_model} for code in (400, 401, 403, 409, 503)
        },
    )
