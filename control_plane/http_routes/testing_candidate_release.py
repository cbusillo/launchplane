import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Header
from pydantic import BaseModel

from control_plane.contracts.testing_candidate_release import (
    TESTING_CANDIDATE_RELEASE_ROUTE,
    TestingCandidateReleaseRequest,
    TestingCandidateReleaseResponse,
)
from control_plane.http_routes.support import ApiRouteRegistrar, HttpErrorFactory
from control_plane.provider_operations import run_durable_provider_operation
from control_plane.service_auth import (
    GitHubHumanIdentity,
    LaunchplaneIdentity,
    LocalAdminIdentity,
    director_agent_identity,
    launchplane_identity_actor,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied
from control_plane.testing_candidate_release import (
    RELEASE_PLAN_ERRORS,
    CandidateRelease,
    TestingCandidateReleaseAdapter,
    TestingCandidateReleaseRefused,
    build_candidate_release,
    release_from_receipt,
    release_response,
)


@dataclass(frozen=True)
class TestingCandidateReleaseDependencies:
    read_identity: Callable[..., LaunchplaneIdentity]
    get_record_store: Callable[[], object]
    next_trace_id: Callable[[], str]
    http_error: HttpErrorFactory
    control_plane_root: Path
    error_response_model: type[BaseModel]


def register_testing_candidate_release_route(
    app: ApiRouteRegistrar, *, dependencies: TestingCandidateReleaseDependencies
) -> None:
    async def testing_candidate_release(
        product: str,
        operation_id: str,
        payload: TestingCandidateReleaseRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> TestingCandidateReleaseResponse:
        trace_id = dependencies.next_trace_id()

        def refusal(code: str, message: str, status: int = 409) -> Exception:
            return dependencies.http_error(
                status_code=status, trace_id=trace_id, code=code, message=message
            )

        administrator = isinstance(identity, LocalAdminIdentity) or (
            isinstance(identity, GitHubHumanIdentity) and identity.role == "admin"
        )
        if not administrator and not director_agent_identity(identity):
            raise refusal(
                "authorization_denied", "Identity cannot release testing candidates.", 403
            )
        if not isinstance(record_store, PostgresRecordStore):
            raise refusal(
                "database_required", "Candidate release requires shared database storage.", 503
            )
        store: PostgresRecordStore = record_store
        key = idempotency_key.strip()
        mutating = payload.mode in {"apply", "readback"}
        if mutating and (not key or len(key) > 200):
            raise refusal(
                "idempotency_key_required",
                "Apply and readback require a bounded Idempotency-Key.",
                400,
            )
        normalized = payload.model_copy(update={"mode": "apply"})
        fingerprint = hashlib.sha256(
            "\n".join(
                (
                    launchplane_identity_actor(identity),
                    product,
                    operation_id,
                    normalized.model_dump_json(),
                )
            ).encode()
        ).hexdigest()
        scope = f"testing-candidate-release:{product}:{operation_id}"
        original = (
            store.read_idempotency_record(
                scope=scope, route_path=TESTING_CANDIDATE_RELEASE_ROUTE, idempotency_key=key
            )
            if mutating
            else None
        )
        if original is not None and original.request_fingerprint != fingerprint:
            raise refusal(
                "idempotency_key_reused", "The key belongs to a different release request."
            )
        if original is not None and original.state == "completed":
            return TestingCandidateReleaseResponse.model_validate(
                original.response_payload
            ).model_copy(update={"trace_id": trace_id, "replayed": True})
        if payload.mode == "readback" and original is None:
            raise refusal("release_receipt_unavailable", "No original release receipt exists.", 404)

        def plan() -> CandidateRelease:
            try:
                return build_candidate_release(
                    dependencies.control_plane_root, store, product, operation_id
                )
            except TestingCandidateReleaseRefused as error:
                raise refusal(error.code, str(error)) from error
            except NativeSourceAuthorizationDenied as error:
                raise refusal(
                    "authorization_denied", "The provider refused the candidate read.", 403
                ) from error
            except RELEASE_PLAN_ERRORS as error:
                raise refusal(
                    "candidate_release_unavailable",
                    "The candidate release could not be planned. Nothing was released.",
                ) from error

        def settle(release: CandidateRelease) -> TestingCandidateReleaseResponse:
            try:
                result = run_durable_provider_operation(
                    store=store,
                    scope=scope,
                    route_path=TESTING_CANDIDATE_RELEASE_ROUTE,
                    idempotency_key=key,
                    request_fingerprint=fingerprint,
                    lease_owner=trace_id,
                    response_trace_id=trace_id,
                    adapter=TestingCandidateReleaseAdapter(
                        store,
                        dependencies.control_plane_root,
                        release,
                        payload.reviewed_plan_sha256,
                        trace_id,
                    ),
                    lease_seconds=900,
                    allow_mutation=payload.mode == "apply",
                )
            except TestingCandidateReleaseRefused as error:
                raise refusal(error.code, str(error) + " Nothing was released.") from error
            except RELEASE_PLAN_ERRORS as error:
                raise refusal(
                    "candidate_release_unavailable",
                    "The candidate could not be checked again before release. Nothing was released.",
                ) from error
            if result.status in {"completed", "replayed", "adopted"}:
                return TestingCandidateReleaseResponse.model_validate(
                    result.response_payload
                ).model_copy(update={"trace_id": trace_id, "replayed": result.status == "replayed"})
            if result.status == "conflict":
                raise refusal(
                    "idempotency_key_reused", "The key belongs to a different release request."
                )
            raise refusal(
                "mutation_reconciliation_required",
                "The candidate release has not settled. Preserve the original request and key.",
            )

        def recorded() -> CandidateRelease:
            assert original is not None
            try:
                return release_from_receipt(original.reconciliation_key)
            except (ValueError, KeyError, TypeError) as error:
                raise refusal(
                    "mutation_reconciliation_required",
                    "The original release receipt is unreadable. Preserve the request and key.",
                ) from error

        def execute() -> TestingCandidateReleaseResponse:
            release = recorded() if original is not None else plan()
            if payload.mode == "dry-run":
                return release_response(release, trace_id, release.status)
            if payload.mode == "apply" and release.plan.digest() != payload.reviewed_plan_sha256:
                raise refusal(
                    "candidate_release_plan_changed", "The release plan changed. Review it again."
                )
            return settle(release)

        return await asyncio.shield(asyncio.create_task(asyncio.to_thread(execute)))

    app.add_api_route(
        TESTING_CANDIDATE_RELEASE_ROUTE,
        testing_candidate_release,
        methods=["POST"],
        response_model=TestingCandidateReleaseResponse,
        response_model_exclude_none=True,
        operation_id="release_failed_testing_candidate",
        summary="Inspect, release or read back a failed testing candidate's storage",
        responses={
            status: {"model": dependencies.error_response_model}
            for status in (400, 401, 403, 404, 409, 503)
        },
    )
