"""Bounded Director-agent preparation of a new empty Odoo testing target."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, NoReturn
from uuid import uuid4
import click

from fastapi import Depends, Header, Path as PathParameter

from control_plane.durable_operation_authorization import DurableOperationAuthorizationDeniedError
from control_plane.http_routes.mutation_support import (
    AcceptedEvidenceResponse,
    accepted_evidence_response,
    idempotency_scope,
    request_fingerprint,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.http_routes.support import run_in_worker_thread
from control_plane.service_auth import (
    LaunchplaneIdentity,
    director_agent_identity,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_replacement_recovery import service_testing_operation
from control_plane.testing_replacement_resources import (
    ROUTE,
    TestingReplacementResourceAdapter,
    TestingReplacementResourcesRequest,
    TestingResourcePreparationRefused,
    resource_plan,
    resource_wire_payload,
)


def register_testing_replacement_resource_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
    root: Path,
    run_provider_mutation: Callable[..., Awaitable[AcceptedEvidenceResponse]],
) -> None:
    async def prepare(
        product: Annotated[str, PathParameter(min_length=1, pattern=r"^\S+$")],
        body: TestingReplacementResourcesRequest,
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
            refuse(
                403,
                "authorization_denied",
                "Testing resource preparation requires the Director agent.",
            )
        if not isinstance(record_store, PostgresRecordStore):
            refuse(
                503,
                "database_storage_required",
                "Testing resource preparation requires database storage.",
            )
        assert isinstance(record_store, PostgresRecordStore)
        if body.mode != "dry-run" and record_store.database_dialect_name != "postgresql":
            refuse(
                503,
                "postgresql_required",
                "Resource qualification needs PostgreSQL concurrency guards.",
            )
        if body.initial_generation:
            from control_plane.testing_replacement_bootstrap import (
                TestingBootstrapPrerequisiteError,
            )
            from control_plane.testing_initial_generation import (
                InitialTestingPrerequisiteCode,
                initial_preparation_id,
                require_initial_usage,
                require_initial_bootstrap_prerequisites,
            )

            checkpoint = InitialTestingPrerequisiteCode.PREPARATION_UNAVAILABLE
            try:
                initial_id = initial_preparation_id(
                    product, body.context, idempotency_scope(identity), idempotency_key
                )
                if body.operation_id and body.operation_id != initial_id:
                    raise ValueError("Use the original initial preparation key.")
                body = body.model_copy(update={"operation_id": initial_id})
                if body.mode == "dry-run":
                    await run_in_worker_thread(
                        lambda: record_store.prepare_initial_testing_generation(
                            product=product, request=body, prepared_by=idempotency_scope(identity)
                        )
                    )
                    checkpoint = InitialTestingPrerequisiteCode.COPY_PREREQUISITE_UNVERIFIED
                    await run_in_worker_thread(
                        lambda: require_initial_bootstrap_prerequisites(record_store, product, body)
                    )
                    checkpoint = InitialTestingPrerequisiteCode.USAGE_UNVERIFIED
                    await run_in_worker_thread(
                        lambda: require_initial_usage(root, record_store, product, body)
                    )
            except DurableOperationAuthorizationDeniedError:
                refuse(
                    403,
                    "authorization_denied",
                    "The provider refused the Odoo testing usage read. Preserve the trace and original request.",
                )
            except TestingBootstrapPrerequisiteError as error:
                refuse(409, error.code, str(error))
            except (ValueError, FileNotFoundError, RuntimeError, OSError, click.ClickException):
                refuse(
                    409,
                    checkpoint,
                    "Initial testing custody or current use could not be verified.",
                )
        if body.mode == "release":
            if not body.initial_generation or not body.reviewed_plan_sha256:
                refuse(
                    400,
                    "review_required",
                    "Release requires the original reviewed initial preparation and key.",
                )
            adapter = TestingReplacementResourceAdapter(root, record_store, product, body, trace)
            try:
                result = await run_in_worker_thread(
                    lambda: adapter.release(idempotency_scope(identity), idempotency_key.strip())
                )
            except DurableOperationAuthorizationDeniedError:
                refuse(
                    403,
                    "authorization_denied",
                    "The provider refused initial preparation release readback. Preserve the trace and original key.",
                )
            except (ValueError, FileNotFoundError, RuntimeError, OSError, click.ClickException):
                refuse(
                    409,
                    "testing_resources_unverified",
                    "Initial release could not prove absent effects and placement; retain the fence.",
                )
            return accepted_evidence_response(
                trace_id=trace, records={"operation": body.operation_id}, result=result
            )
        if body.mode == "apply":
            if not idempotency_key.strip() or not body.reviewed_plan_sha256:
                refuse(
                    400,
                    "review_required",
                    "Apply requires the reviewed resource plan and original key.",
                )
            adapter = TestingReplacementResourceAdapter(root, record_store, product, body, trace)
            try:
                return await run_provider_mutation(
                    record_store=record_store,
                    identity=identity,
                    route_path=ROUTE.replace("{product}", product),
                    idempotency_key=idempotency_key.strip(),
                    request_fingerprint=request_fingerprint(
                        {"product": product, **resource_wire_payload(body)}
                    ),
                    trace_id=trace,
                    adapter=adapter,
                    in_progress_message="Testing resource preparation is running with the original key.",
                    reconcile_message="The original preparation has an unknown outcome; do not create another target.",
                )
            except DurableOperationAuthorizationDeniedError:
                refuse(
                    403,
                    "authorization_denied",
                    "The provider refused the Odoo testing usage read. Preserve the trace and original key.",
                )
            except TestingResourcePreparationRefused:
                refuse(
                    409,
                    "testing_resources_refused",
                    "Preparation was refused before provider work. Re-read the lane and prepare a new dry run.",
                )
            except (ValueError, FileNotFoundError, click.ClickException, RuntimeError, OSError):
                refuse(
                    409,
                    "testing_resources_unverified",
                    "Resource preparation has no verified outcome. Inspect the original key and retained allocation.",
                )
        if body.mode == "readback":
            adapter = TestingReplacementResourceAdapter(root, record_store, product, body, trace)
            try:
                result = await run_in_worker_thread(adapter.readback)
            except DurableOperationAuthorizationDeniedError:
                refuse(
                    403,
                    "authorization_denied",
                    "The provider refused the Odoo testing usage read. Preserve the trace and original key.",
                )
            except (ValueError, FileNotFoundError, click.ClickException, RuntimeError, OSError):
                refuse(
                    409,
                    "testing_resources_unverified",
                    "The fresh replacement resource readback did not pass.",
                )
            return accepted_evidence_response(
                trace_id=trace, records={"operation": body.operation_id}, result=result
            )
        try:
            if not body.initial_generation:
                operation = record_store.read_odoo_stable_target_replacement_operation_record(
                    body.operation_id
                )
                profile = record_store.read_product_profile_record(product)
                if (
                    profile.driver_id != "odoo"
                    or not service_testing_operation(operation)
                    or operation.status != "reconciliation_required"
                    or (operation.product, operation.context, operation.instance)
                    != (product, body.context, "testing")
                    or not any(
                        (lane.context, lane.instance) == (body.context, "testing")
                        for lane in profile.lanes
                    )
                ):
                    raise ValueError(
                        "The requested held operation does not belong to this Odoo testing lane."
                    )
                if not record_store.prepare_disposable_testing_replacement(
                    operation, idempotency_scope(identity)
                ):
                    raise ValueError("The retained executor cannot be excluded.")
                preparation = record_store.read_disposable_resource_preparation(
                    operation.operation_id
                )
                if preparation is None:
                    raise ValueError("Retained target capture is unavailable.")
                if not preparation.resource_namespace:
                    record_store.compare_and_write_disposable_resource_preparation(
                        expected=preparation,
                        replacement=preparation.model_copy(
                            update={"resource_namespace": f"lp-testing-{uuid4().hex}"}
                        ),
                    )
            result = resource_plan(record_store, product=product, request=body)
        except FileNotFoundError:
            refuse(404, "not_found", "The requested testing resource record is unavailable.")
        except ValueError:
            refuse(
                409,
                "testing_resources_refused",
                "The owned testing lane or retained operation changed; inspect its current state.",
            )
        return accepted_evidence_response(
            trace_id=trace, records={"operation": body.operation_id}, result=result
        )

    app.add_api_route(
        ROUTE,
        prepare,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="prepare_testing_replacement_resources",
        summary="Prepare fresh independent Odoo testing replacement resources",
        responses={
            code: {"model": dependencies.error_response_model}
            for code in (400, 401, 403, 404, 409, 503)
        },
    )
