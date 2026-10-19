"""Review and re-arm one exhausted testing artifact without a direct deploy."""

from collections.abc import Callable
from typing import Annotated, NoReturn

import click
from fastapi import Depends, Header, Path
from sqlalchemy.exc import SQLAlchemyError

from control_plane.build_provenance import BuildProvenanceError
from control_plane.contracts.product_reconcile import TestingRetryRearm
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
from control_plane.service_auth import AuthorizationTarget, LaunchplaneIdentity
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
    def retry(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        body: TestingRetryRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", max_length=256)] = "",
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
        if not dependencies.authorization_allows(
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
                    "data_source_mode": "existing",
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
