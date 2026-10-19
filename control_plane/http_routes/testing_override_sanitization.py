"""Director-agent record-only continuation for fresh testing overrides."""

from collections.abc import Callable
from typing import Annotated, NoReturn

import click
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
from control_plane.testing_override_sanitization import (
    ROUTE,
    TestingOverrideSanitizationRequest,
    read_sanitization_audit,
    sanitization_readback,
    sanitization_snapshot,
)


def register_testing_override_sanitization_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
) -> None:
    def sanitize(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        body: TestingOverrideSanitizationRequest,
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
                "Testing override sanitization requires the Director agent.",
            )
        if not isinstance(record_store, PostgresRecordStore):
            refuse(503, "database_storage_required", "Sanitization requires database records.")
        if body.mode == "apply" and record_store.database_dialect_name != "postgresql":
            refuse(
                503, "postgresql_required", "Sanitization requires PostgreSQL concurrency guards."
            )
        if body.mode != "dry-run" and not body.reviewed_plan_sha256:
            refuse(400, "review_required", "Keep the reviewed sanitization plan.")
        if body.mode == "apply" and not idempotency_key.strip():
            refuse(400, "idempotency_key_required", "Keep the original sanitization key.")
        try:
            if body.mode == "readback":
                plan = sanitization_readback(record_store, product, body)
            else:
                audit = (
                    read_sanitization_audit(record_store, product, body)
                    if body.mode == "apply"
                    else None
                )
                plan = (
                    audit.plan
                    if audit is not None
                    else sanitization_snapshot(record_store, product, body).plan
                )
            if body.mode == "apply" and plan.plan_sha256 != body.reviewed_plan_sha256:
                raise ValueError("The reviewed sanitization changed.")
            response = accepted_evidence_response(
                trace_id=trace,
                records={"operation": body.operation_id},
                result=plan.model_copy(update={"applied": body.mode != "dry-run"}).model_dump(
                    mode="json"
                ),
            )
            if body.mode != "apply":
                return response
            status, receipt = record_store.sanitize_testing_overrides(
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
                ),
            )
            if status == "replayed":
                return replay_idempotent_response(
                    trace_id=trace, stored_record=receipt, route_path=ROUTE
                )
            if status != "written":
                refuse(
                    409,
                    "sanitization_key_conflict",
                    "Keep the original sanitization request and key.",
                )
            return response
        except (ValueError, FileNotFoundError, RuntimeError, click.ClickException):
            refuse(
                409,
                "testing_override_sanitization_unverified",
                "Testing override sanitization is unverified; preserve its generation, review and original key.",
            )

    app.add_api_route(
        ROUTE,
        sanitize,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="sanitize_testing_generation_overrides",
        summary="Sanitize stored overrides on the current unfinished testing generation",
        responses={
            code: {"model": dependencies.error_response_model} for code in (400, 401, 403, 409, 503)
        },
    )
