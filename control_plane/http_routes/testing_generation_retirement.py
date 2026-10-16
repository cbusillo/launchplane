"""Reviewed, database-only retirement of incomplete fresh testing custody."""

from collections.abc import Callable
from typing import Annotated, NoReturn

from fastapi import Depends, Header, Path

from control_plane.http_routes.mutation_support import (
    AcceptedEvidenceResponse,
    accepted_evidence_response,
    idempotency_scope,
    replay_idempotent_response,
    request_fingerprint,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.service_auth import LaunchplaneIdentity, director_agent_identity
from control_plane.storage.postgres import DbOnlyMutationRequest, PostgresRecordStore
from control_plane.testing_generation_retirement import (
    ROUTE,
    TestingGenerationRetirementRequest,
    retirement_snapshot,
)


def register_testing_generation_retirement_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
) -> None:
    def retire(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        body: TestingGenerationRetirementRequest,
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
                "Testing generation retirement requires the Director agent.",
            )
        if not isinstance(record_store, PostgresRecordStore):
            refuse(
                503, "database_storage_required", "Testing retirement requires database custody."
            )
        assert isinstance(record_store, PostgresRecordStore)
        if body.mode == "apply" and record_store.database_dialect_name != "postgresql":
            refuse(
                503,
                "postgresql_required",
                "Testing retirement needs PostgreSQL concurrency guards.",
            )
        if body.mode == "apply" and (not idempotency_key.strip() or not body.reviewed_plan_sha256):
            refuse(
                400, "review_required", "Apply requires the reviewed plan and original stable key."
            )
        try:
            retained = record_store.read_retained_testing_generation(body.operation_id)
            if retained is not None:
                if (retained.plan.product, retained.plan.context, retained.reason) != (
                    product,
                    body.context,
                    body.reason,
                ):
                    raise ValueError("The retained generation belongs to another request.")
                plan = retained.plan
                if body.mode == "dry-run":
                    raise ValueError("The generation is already retained; use readback.")
            else:
                if body.mode == "readback":
                    raise ValueError("No retirement receipt is available.")
                with record_store.testing_replacement_execution_lock(body.operation_id) as check:
                    if check is None:
                        raise ValueError("The original executor cannot be excluded.")
                    plan = retirement_snapshot(record_store, product, body).plan
                    check()
            if body.mode in {"dry-run", "readback"}:
                return accepted_evidence_response(
                    trace_id=trace,
                    records={"operation": body.operation_id},
                    result=plan.model_dump(mode="json"),
                )
            if body.reviewed_plan_sha256 != plan.plan_sha256:
                raise ValueError("The reviewed retirement changed.")
            response = accepted_evidence_response(
                trace_id=trace,
                records={"operation": body.operation_id},
                result=plan.model_copy(update={"applied": True}).model_dump(mode="json"),
            )
            status, receipt = record_store.retire_testing_generation(
                product=product,
                request=body,
                mutation=DbOnlyMutationRequest(
                    scope=idempotency_scope(identity),
                    route_path=ROUTE,
                    idempotency_key=idempotency_key.strip(),
                    request_fingerprint=request_fingerprint(
                        {"product": product, **body.model_dump(mode="json")}
                    ),
                    lease_owner=trace,
                    response_status_code=202,
                    response_trace_id=trace,
                    response_payload=response.model_dump(mode="json", exclude_none=True),
                    lease_seconds=300,
                ),
            )
            if status == "replayed":
                return replay_idempotent_response(
                    trace_id=trace, stored_record=receipt, route_path=ROUTE
                )
            if status != "written":
                refuse(
                    409,
                    "retirement_key_conflict",
                    "Keep the original retirement request and key; inspect its receipt.",
                )
            return response
        except (ValueError, FileNotFoundError, RuntimeError):
            refuse(
                409,
                "testing_retirement_unverified",
                "Testing retirement is unverified. Preserve the original operation and evidence; no provider action is permitted.",
            )

    app.add_api_route(
        ROUTE,
        retire,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="retire_incomplete_testing_generation",
        summary="Retain an incomplete testing generation without releasing its operation",
        responses={
            code: {"model": dependencies.error_response_model} for code in (400, 401, 403, 409, 503)
        },
    )
