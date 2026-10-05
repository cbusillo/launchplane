"""Scoped admin recovery with separate inspection and reviewed apply."""

import asyncio
from collections.abc import Callable
from typing import Annotated
from fastapi import Depends, Query

from control_plane.contracts.generic_web_deploy_recovery import (
    build_generic_web_deploy_recovery_digest,
)
from control_plane.contracts.generic_web_promotion_recovery import (
    PromotionRecoveryReview,
    PromotionRecoveryApply,
    PromotionRecoveryPlan,
    PromotionRecoveryApplied,
)
from control_plane.generic_web_promotion_recovery import PromotionInspection, inspect_promotion
from control_plane.http_routes.generic_web import GenericWebWriteRouteDependencies
from control_plane.http_routes.support import ApiRouteRegistrar
from control_plane.service_auth import (
    AuthorizationTarget,
    GitHubHumanIdentity,
    LaunchplaneIdentity,
    LocalAdminIdentity,
    LocalOperatorIdentity,
)
from control_plane.storage.postgres import PostgresRecordStore

RECOVERY_ROUTE = "/v1/admin/generic-web/promotion-recovery/{product}/{decision_record_id}"


def register_promotion_recovery_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: GenericWebWriteRouteDependencies,
    read_identity: Callable[..., LaunchplaneIdentity],
) -> None:
    def authorized_store(
        product: str, identity: LaunchplaneIdentity, record_store: object, *, apply: bool
    ) -> PostgresRecordStore:
        trace = dependencies.next_trace_id()
        if not isinstance(record_store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="database_storage_required",
                message="Promotion recovery requires database storage.",
            )
        admin = isinstance(identity, LocalAdminIdentity | LocalOperatorIdentity) or (
            isinstance(identity, GitHubHumanIdentity) and identity.role == "admin"
        )
        if not admin:
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace,
                code="authorization_denied",
                message="Promotion recovery requires a scoped admin.",
            )
        try:
            profile = record_store.read_product_profile_record(product)
            lane = next(item for item in profile.lanes if item.instance == "prod")
        except (FileNotFoundError, StopIteration) as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace,
                code="not_found",
                message="Promotion recovery was not found.",
            ) from error
        if not dependencies.authorization_allows(
            identity=identity,
            action="generic_web_prod_promotion.execute" if apply else "product_environment.read",
            product=product,
            context=lane.context,
            target=AuthorizationTarget(scope="instance", instances=("prod",)),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace,
                code="authorization_denied",
                message="Identity cannot recover this product's promotion.",
            )
        return record_store

    async def inspect(
        store: PostgresRecordStore,
        product: str,
        decision_record_id: str,
        attempt: int,
        *,
        inspect_provider: bool = True,
    ) -> PromotionInspection:
        try:
            return await asyncio.to_thread(
                inspect_promotion,
                store=store,
                root=dependencies.control_plane_root,
                product=product,
                decision_record_id=decision_record_id,
                attempt=attempt,
                inspect_provider=inspect_provider,
            )
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=dependencies.next_trace_id(),
                code="not_found",
                message="Promotion recovery was not found.",
            ) from error
        except ValueError as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=dependencies.next_trace_id(),
                code="recovery_evidence_conflict",
                message="Promotion recovery evidence is not authoritative.",
            ) from error

    async def read_promotion_recovery(
        product: str,
        decision_record_id: str,
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        attempt: Annotated[int, Query(ge=1, le=2)] = 1,
    ) -> PromotionRecoveryPlan:
        store = authorized_store(product, identity, record_store, apply=False)
        return (
            await inspect(store, product, decision_record_id, attempt, inspect_provider=False)
        ).plan(product, "")

    async def dry_run_promotion_recovery(
        product: str,
        decision_record_id: str,
        review: PromotionRecoveryReview,
        identity: Annotated[
            LaunchplaneIdentity, Depends(dependencies.read_browser_mutation_identity)
        ],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        attempt: Annotated[int, Query(ge=1, le=2)] = 1,
    ) -> PromotionRecoveryPlan:
        store = authorized_store(product, identity, record_store, apply=False)
        return (await inspect(store, product, decision_record_id, attempt)).plan(
            product, review.reason
        )

    async def apply_promotion_recovery(
        product: str,
        decision_record_id: str,
        review: PromotionRecoveryApply,
        identity: Annotated[
            LaunchplaneIdentity, Depends(dependencies.read_browser_mutation_identity)
        ],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        attempt: Annotated[int, Query(ge=1, le=2)] = 1,
    ) -> PromotionRecoveryApplied:
        store = authorized_store(product, identity, record_store, apply=True)
        inspection = await inspect(store, product, decision_record_id, attempt)
        plan = inspection.plan(product, review.reason)
        trace = dependencies.next_trace_id()
        reason_digest = build_generic_web_deploy_recovery_digest({"reason": review.reason})
        recorded_recovery = inspection.reservation.response_payload.get("recovery", {})
        replay = (
            isinstance(recorded_recovery, dict)
            and (
                recorded_recovery.get("recovery_digest") == review.expected_recovery_digest
                and recorded_recovery.get("reason_digest") == reason_digest
            )
            and inspection.reservation.state == "completed"
        )
        if review.recovery_reference != plan.recovery_reference or (
            review.expected_recovery_digest != plan.recovery_digest and not replay
        ):
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="stale_recovery_digest",
                message="Reviewed promotion recovery evidence changed.",
            )
        if inspection.reservation.state == "completed":
            return PromotionRecoveryApplied(**plan.model_dump())
        if inspection.action not in {"adopt_promotion", "adopt_rollback"}:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="recovery_not_actionable",
                message="The promotion outcome remains held.",
            )
        authorized_store(product, identity, store, apply=True)
        adoption = await asyncio.to_thread(
            store.adopt_reconciled_mutation,
            reservation=inspection.reservation,
            response_status_code=202,
            response_trace_id=trace,
            response_payload={
                "status": "accepted",
                "trace_id": trace,
                "records": {},
                "result": inspection.result,
                "recovery": {
                    "recovery_digest": plan.recovery_digest,
                    "reason_digest": reason_digest,
                    "action": inspection.action,
                },
            },
            expected_promotion_evidence=inspection.evidence,
            promotion_recovery_inventory=inspection.inventory,
            promotion_recovery_record=inspection.promotion,
            promotion_recovery_deployment=inspection.deployment,
        )
        if adoption.status != "adopted" or adoption.record is None:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="reservation_changed",
                message="Promotion evidence changed before adoption.",
            )
        return PromotionRecoveryApplied(**{**plan.model_dump(), "reservation_state": "completed"})

    for suffix, endpoint, method, model in (
        ("", read_promotion_recovery, "GET", PromotionRecoveryPlan),
        ("/dry-run", dry_run_promotion_recovery, "POST", PromotionRecoveryPlan),
        ("/apply", apply_promotion_recovery, "POST", PromotionRecoveryApplied),
    ):
        app.add_api_route(
            RECOVERY_ROUTE + suffix,
            endpoint,
            methods=[method],
            status_code=202 if suffix == "/apply" else 200,
            response_model=model,
            operation_id=endpoint.__name__,
            responses={
                code: {"model": dependencies.error_response_model}
                for code in (401, 403, 404, 409, 503)
            },
        )
