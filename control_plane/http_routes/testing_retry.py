"""Review and re-arm one exhausted testing artifact without a direct deploy."""

from collections.abc import Callable
from typing import Annotated, NoReturn

import click
from fastapi import Depends, Header, Path, Query
from sqlalchemy.exc import SQLAlchemyError

from control_plane.build_provenance import BuildProvenanceError
from control_plane.contracts.testing_retry import TestingRetryRearm
from control_plane.http_routes.mutation_support import (
    AcceptedEvidenceResponse,
    accepted_evidence_response,
    idempotency_scope,
    replay_idempotent_response,
    request_fingerprint,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.odoo_target_replacement_apply_http import (
    resolve_odoo_target_replacement_apply_lane,
)
from control_plane.product_reconcile import ProductReconcileError
from control_plane.service_auth import (
    AuthorizationTarget,
    LaunchplaneIdentity,
    director_agent_identity,
)
from control_plane.storage.postgres import DbOnlyMutationRequest, PostgresRecordStore
from control_plane.testing_retry import (
    TESTING_RETRY_ROUTE,
    TestingRetryRefused,
    TestingRetryRequest,
    build_testing_retry_plan,
)
from control_plane.workflows.ship import utc_now_timestamp


def register_testing_retry_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
) -> None:
    def perform_retry(
        product: str,
        body: TestingRetryRequest,
        identity: LaunchplaneIdentity,
        record_store: object,
        idempotency_key: str,
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()

        def refusal(code: str, status: int = 409) -> NoReturn:
            raise dependencies.http_error(
                status_code=status,
                trace_id=trace,
                code=code,
                message="Testing retry was refused. Preserve the original key and review lane evidence.",
            )

        if not isinstance(record_store, PostgresRecordStore):
            refusal("database_storage_required", 503)
        assert isinstance(record_store, PostgresRecordStore)
        store = record_store
        try:
            lane = resolve_odoo_target_replacement_apply_lane(
                record_store=store, product=product, instance="testing"
            )
        except (ValueError, click.ClickException):
            refusal("testing_retry_lane_unavailable", 404)
        # This route can only wake the testing reconciler. Keep standing
        # permission here rather than widening the mixed production/testing action.
        if not director_agent_identity(identity) and not dependencies.authorization_allows(
            identity=identity,
            action="odoo_target_replacement_apply.execute",
            product=product,
            context=lane.context,
            target=AuthorizationTarget(scope="instance", instances=("testing",)),
        ):
            refusal("authorization_denied", 403)
        key = idempotency_key.strip()
        scope = idempotency_scope(identity)
        fingerprint = request_fingerprint({"product": product, **body.model_dump(mode="json")})

        def replay_before_preflight() -> AcceptedEvidenceResponse | None:
            preflight = store.prepare_db_only_mutation(
                scope=scope,
                route_path=TESTING_RETRY_ROUTE,
                idempotency_key=key,
                request_fingerprint=fingerprint,
            )
            if preflight.status == "replayed":
                assert preflight.record is not None
                return replay_idempotent_response(trace_id=trace, stored_record=preflight.record)
            if preflight.status == "conflict":
                refusal("idempotency_key_reused")
            if preflight.status not in {"missing", "released"}:
                refusal("testing_retry_busy")
            return None

        if body.mode == "apply":
            if not key:
                refusal("idempotency_key_required", 400)
            replay = replay_before_preflight()
            if replay is not None:
                return replay
        try:
            plan = build_testing_retry_plan(store, product, body)
            if body.mode == "apply" and body.reviewed_plan_sha256 != plan.plan_sha256:
                raise TestingRetryRefused("testing_retry_stale")
            response = accepted_evidence_response(
                trace_id=trace,
                records={"product_reconcile_target": plan.request.target_key},
                result={
                    "product": product,
                    "context": plan.context,
                    "instance": "testing",
                    "artifact_id": body.artifact_id,
                    "source_git_ref": body.source_git_ref,
                    "after_operation_id": plan.after_operation_id,
                    "plan_sha256": plan.plan_sha256,
                    "reconcile_requested": body.mode == "apply",
                    "data_source_mode": plan.data_source_mode,
                    "generation_binding_sha256": plan.generation_binding_sha256,
                },
            )
            if body.mode == "dry-run":
                return response
            status, receipt = store.commit_testing_retry_rearm(
                expected=plan.request,
                rearm=TestingRetryRearm(
                    artifact_id=body.artifact_id,
                    context=plan.context,
                    after_operation_id=plan.after_operation_id,
                    lane_binding_sha256=plan.lane_binding_sha256,
                    recorded_at=utc_now_timestamp(),
                    recorded_by=scope,
                    reason=body.reason,
                ),
                mutation=DbOnlyMutationRequest(
                    scope=scope,
                    route_path=TESTING_RETRY_ROUTE,
                    idempotency_key=key,
                    request_fingerprint=fingerprint,
                    lease_owner=trace,
                    response_status_code=202,
                    response_trace_id=trace,
                    response_payload=response.model_dump(mode="json", exclude_none=True),
                ),
                generation_binding_sha256=plan.generation_binding_sha256,
            )
            if status == "replayed":
                return replay_idempotent_response(trace_id=trace, stored_record=receipt)
            if status == "idempotency_conflict":
                refusal("idempotency_key_reused")
            if status != "written":
                refusal("testing_retry_busy")
            return response
        except TestingRetryRefused as error:
            # A concurrent same-key request may have committed after our first lookup.
            if body.mode == "apply":
                replay = replay_before_preflight()
                if replay is not None:
                    return replay
            refusal(error.code)
        except (
            FileNotFoundError,
            ValueError,
            ProductReconcileError,
            BuildProvenanceError,
            click.ClickException,
        ):
            refusal("testing_retry_evidence_unavailable")
        except SQLAlchemyError:
            refusal("testing_retry_storage_unavailable", 503)

    def retry(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        body: TestingRetryRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", max_length=256)] = "",
    ) -> AcceptedEvidenceResponse:
        try:
            return perform_retry(product, body, identity, record_store, idempotency_key)
        except SQLAlchemyError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=dependencies.next_trace_id(),
                code="testing_retry_storage_unavailable",
                message="Testing retry storage is unavailable.",
            ) from error

    def review(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        artifact_id: Annotated[str, Query(min_length=1)],
        source_git_ref: Annotated[str, Query(pattern=r"^[a-f0-9]{40}$")],
        reason: Annotated[str, Query(min_length=1, max_length=1000)],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()
        try:
            if not isinstance(record_store, PostgresRecordStore):
                raise dependencies.http_error(
                    status_code=503,
                    trace_id=trace,
                    code="database_storage_required",
                    message="Testing retry requires database storage.",
                )
            lane = resolve_odoo_target_replacement_apply_lane(
                record_store=record_store, product=product, instance="testing"
            )
            if not dependencies.authorization_allows(
                identity=identity,
                action="product_environment.read",
                product=product,
                context=lane.context,
                target=AuthorizationTarget(scope="instance", instances=("testing",)),
            ):
                raise dependencies.http_error(
                    status_code=403,
                    trace_id=trace,
                    code="authorization_denied",
                    message="Caller cannot review this testing lane.",
                )
            body = TestingRetryRequest(
                artifact_id=artifact_id, source_git_ref=source_git_ref, reason=reason
            )
            plan = build_testing_retry_plan(record_store, product, body)
            return accepted_evidence_response(
                trace_id=trace,
                records={},
                result={
                    "product": product,
                    "context": plan.context,
                    "instance": "testing",
                    "artifact_id": body.artifact_id,
                    "source_git_ref": body.source_git_ref,
                    "after_operation_id": plan.after_operation_id,
                    "plan_sha256": plan.plan_sha256,
                    "reconcile_requested": False,
                    "data_source_mode": plan.data_source_mode,
                    "generation_binding_sha256": plan.generation_binding_sha256,
                },
            )
        except TestingRetryRefused as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code=error.code,
                message="Testing retry is not ready; review the lane evidence.",
            ) from error
        except SQLAlchemyError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="testing_retry_storage_unavailable",
                message="Testing retry storage is unavailable.",
            ) from error
        except (
            FileNotFoundError,
            ValueError,
            ProductReconcileError,
            BuildProvenanceError,
            click.ClickException,
        ) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="testing_retry_evidence_unavailable",
                message="Testing retry evidence is unavailable.",
            ) from error

    app.add_api_route(
        TESTING_RETRY_ROUTE,
        review,
        methods=["GET"],
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="review_testing_retry",
        summary="Review an exhausted testing artifact without write authority",
        responses={
            code: {"model": dependencies.error_response_model} for code in (401, 403, 409, 503)
        },
    )
    app.add_api_route(
        TESTING_RETRY_ROUTE,
        retry,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="rearm_testing_retry",
        summary="Review or re-arm an exhausted testing artifact",
        responses={
            code: {"model": dependencies.error_response_model}
            for code in (400, 401, 403, 404, 409, 503)
        },
    )
