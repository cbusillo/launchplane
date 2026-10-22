"""Reviewed continuation of public verification; no deploy or restore authority."""

from collections.abc import Callable
from pathlib import Path as FilePath
from typing import Annotated, NoReturn

import click
from fastapi import Depends, Header, Path
from sqlalchemy.exc import SQLAlchemyError

from control_plane.durable_operation_authorization import DurableOperationAuthorizationDeniedError
from control_plane.dokploy.native_copy_stream import NativeProgramFailed

from control_plane.contracts.canonical_json import canonical_json_sha256
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
from control_plane.testing_verification import (
    ROUTE,
    TestingVerification,
    TestingVerificationRequest,
)


def register_testing_verification_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
    root: FilePath,
) -> None:
    def verify(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        body: TestingVerificationRequest,
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
            refuse(403, "authorization_denied", "Testing verification requires the Director agent.")
        if not isinstance(record_store, PostgresRecordStore):
            refuse(
                503, "database_storage_required", "Testing verification requires database records."
            )
        if body.mode == "apply" and record_store.database_dialect_name != "postgresql":
            refuse(503, "postgresql_required", "Testing verification requires PostgreSQL fencing.")
        if body.mode != "dry-run" and not body.reviewed_plan_sha256:
            refuse(400, "review_required", "Retain the original reviewed verification plan.")
        if body.mode == "apply" and not idempotency_key.strip():
            refuse(400, "idempotency_key_required", "Keep one original verification key.")
        fingerprint = request_fingerprint({"product": product, **body.model_dump(mode="json")})
        scope = idempotency_scope(identity)
        if body.mode == "apply":
            prior = record_store.prepare_db_only_mutation(
                scope=scope,
                route_path=ROUTE,
                idempotency_key=idempotency_key.strip(),
                request_fingerprint=fingerprint,
            )
            if prior.status == "replayed":
                assert prior.record is not None
                return replay_idempotent_response(
                    trace_id=trace, stored_record=prior.record, route_path=ROUTE
                )
            if prior.status not in {"missing", "released"}:
                refuse(
                    409,
                    "verification_key_conflict",
                    "Inspect the original verification key and receipt.",
                )
        verification = TestingVerification(root, record_store, product, body)
        try:
            with record_store.testing_replacement_execution_lock(body.operation_id) as check:
                if check is None:
                    refuse(
                        409,
                        "testing_verification_in_progress",
                        "The original generation is active. Preserve its plan and key; observe completion before same-key recovery.",
                    )
                with record_store.testing_verification_lane_guard(
                    product=product, context=body.context
                ) as check_lane:

                    def checkpoint() -> None:
                        try:
                            check()
                            check_lane()
                        except SQLAlchemyError as error:
                            raise RuntimeError(
                                "Testing verification lost execution exclusion."
                            ) from error

                    if body.mode == "readback":
                        result = verification.readback()
                        checkpoint()
                    else:
                        expected = verification.snapshot()
                        plan = verification.plan()
                        if (
                            body.mode == "apply"
                            and plan["plan_sha256"] != body.reviewed_plan_sha256
                        ):
                            raise ValueError("The reviewed verification authority changed.")
                        completed = verification.verify()
                        checkpoint()
                        if canonical_json_sha256(verification.snapshot()) != plan["plan_sha256"]:
                            raise ValueError("Testing authority changed during verification.")
                        result = (
                            {**plan, "public_health_verified": True}
                            if body.mode == "dry-run"
                            else verification.receipt(body.reviewed_plan_sha256, applied=True)
                        )
                        if body.mode == "apply":
                            response = accepted_evidence_response(
                                trace_id=trace,
                                records={"deployment": body.deployment_record_id},
                                result=result,
                            )
                            status, receipt = record_store.publish_testing_verification(
                                verification=verification,
                                expected=expected,
                                completed=completed,
                                mutation=DbOnlyMutationRequest(
                                    scope=scope,
                                    route_path=ROUTE,
                                    idempotency_key=idempotency_key.strip(),
                                    request_fingerprint=fingerprint,
                                    lease_owner=trace,
                                    response_status_code=202,
                                    response_trace_id=trace,
                                    response_payload=response.model_dump(
                                        mode="json", exclude_none=True
                                    ),
                                ),
                                checkpoint=checkpoint,
                            )
                            if status == "replayed":
                                return replay_idempotent_response(
                                    trace_id=trace, stored_record=receipt, route_path=ROUTE
                                )
                            if status != "written":
                                refuse(
                                    409,
                                    "verification_key_conflict",
                                    "Inspect the original verification key and receipt.",
                                )
                            return response
            return accepted_evidence_response(trace_id=trace, records={}, result=result)
        except DurableOperationAuthorizationDeniedError:
            refuse(
                403,
                "authorization_denied",
                "The existing provider transport refused complete_testing_verification. Preserve the trace and original request; do not change identity or credentials.",
            )
        except NativeProgramFailed as error:
            refuse(
                409,
                error.code,
                "Testing copy predicate "
                + error.code
                + ". Preserve the original generation, deployment, plan and key.",
            )
        except (
            ValueError,
            FileNotFoundError,
            RuntimeError,
            OSError,
            click.ClickException,
        ):
            refuse(
                409,
                "testing_verification_unverified",
                "Testing verification is incomplete. Preserve the original generation, deployment, plan and key; never restore again to repair public verification.",
            )

    app.add_api_route(
        ROUTE,
        verify,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="complete_testing_verification",
        summary="Verify and publish the installed Odoo testing copy without another deployment",
        responses={
            code: {"model": dependencies.error_response_model} for code in (400, 401, 403, 409, 503)
        },
    )
