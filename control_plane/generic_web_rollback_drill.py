"""The shared generic-web rollback, fenced as a Client release drill step."""

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import click

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.workflows.inventory import build_environment_inventory
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.contracts.generic_web_rollback import (
    GenericWebRollbackPlanRequest,
    build_generic_web_rollback_plan,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.release_review import ReleaseReviewDecisionRecord
from control_plane.generic_web_promotion_http import (
    GenericWebProdPromotionEnvelope,
    GENERIC_WEB_PROD_PROMOTION_ROUTE,
)
from control_plane.generic_web_promotion_provider_adapter import (
    GenericWebProdPromotionProviderMutationAdapter,
    require_generic_web_promotion_target,
)
from control_plane.generic_web_rollback_http import GENERIC_WEB_ROLLBACK_ROUTE
from control_plane.provider_operations import (
    ProviderEvidenceLease,
    ProviderMutationOutcome,
    ProviderMutationUnknownError,
    ProviderOperationLease,
    provider_operation_response_payload,
    provider_operation_title,
    run_durable_provider_operation,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_deploy_provider import GenericWebResolvedDeployTarget
from control_plane.workflows.generic_web_promotion import (
    GenericWebProdPromotionRequest,
    _RollbackTarget,
    _roll_back_production,
)
from control_plane.workflows.ship import utc_now_timestamp


def run_generic_web_rollback_drill(
    *,
    store: PostgresRecordStore,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
    deployment: DeploymentRecord,
    scope: str,
    idempotency_key: str,
    validate_checkpoint: Callable[[], None],
) -> str:
    lane = next(lane for lane in profile.lanes if lane.instance == "prod")
    request = GenericWebRollbackPlanRequest(
        product=profile.product, rollback_deployment_record_id=deployment.record_id
    )
    plan = build_generic_web_rollback_plan(record_store=store, request=request)
    if plan.planned_deploy is None or plan.status != "ready":
        raise click.ClickException("The accepted rollback target is not ready.")
    planned = plan.planned_deploy
    trace_id = f"client-release-{decision.record_id}"
    previous_inventory, _promotion = rollback_drill_previous_inventory(
        store=store, decision=decision, deployment=deployment, scope=scope
    )

    class RollbackDrillAdapter(GenericWebProdPromotionProviderMutationAdapter):
        def resolve_deploy_target(self) -> GenericWebResolvedDeployTarget:
            if self._resolved_deploy_target is None:
                self._resolved_deploy_target = self._deploy_provider.resolve_deploy_target(
                    control_plane_root=control_plane_root,
                    request_artifact_id=planned.artifact_id,
                    request_source_git_ref=planned.source_git_ref,
                    request_timeout_seconds=planned.timeout_seconds,
                    request_no_cache=planned.no_cache,
                    record_store=store,
                    profile=profile,
                    lane=lane,
                    normalized_artifact_id=planned.artifact_id,
                    request_deploy_reference=planned.deploy_reference,
                    fallback_target_name=f"{profile.product}-{lane.instance}",
                )
            return self._resolved_deploy_target

        def apply(
            self, provider_operation_key: str, lease: ProviderOperationLease
        ) -> ProviderMutationOutcome:
            effects_started = False

            def checkpoint(phase: str) -> None:
                nonlocal effects_started
                # This is forward drill work; recovery inside a failed promotion
                # keeps its separate rule allowing rollback after revocation.
                validate_checkpoint()
                require_generic_web_promotion_target(
                    record_store=store,
                    lane=lane,
                    resolved_deploy_target=self.resolve_deploy_target(),
                )
                lease.checkpoint_effect(phase)
                effects_started = True

            try:
                self._validate_before_effect(self.resolve_deploy_target())
                current = build_generic_web_rollback_plan(record_store=store, request=request)
                if current.status != "ready" or current.planned_deploy != planned:
                    raise click.ClickException("The accepted rollback target changed.")
                require_generic_web_promotion_target(
                    record_store=store,
                    lane=lane,
                    resolved_deploy_target=self.resolve_deploy_target(),
                )
                if not isinstance(lease, ProviderEvidenceLease):
                    raise ValueError("Rollback drill requires an evidence-bound provider lease.")
                with store.provider_evidence_guard(lease.evidence_reservation):
                    outcome = _roll_back_production(
                        control_plane_root=control_plane_root,
                        record_store=store,
                        profile=profile,
                        lane=lane,
                        request=self._promotion_request.promotion,
                        rollback_target=_RollbackTarget(
                            deployment_record_id=deployment.record_id,
                            planned_deploy=planned,
                            previous_inventory=previous_inventory,
                        ),
                        production_changed=True,
                        deploy_provider=self._deploy_provider,
                        provider_operation_title=provider_operation_title(provider_operation_key),
                        deployment_record_id=self._deployment_record_id(provider_operation_key),
                        provider_effect_checkpoint=checkpoint,
                        resolved_deploy_target=self.resolve_deploy_target(),
                    )
                    if outcome.error is not None:
                        raise outcome.error
                result: dict[str, object] = {
                    "rollback_status": outcome.evidence.status,
                    "deployment_record_id": outcome.evidence.deployment_record_id,
                    "rollback_target_deployment_record_id": deployment.record_id,
                    "rollback_health_status": outcome.health.status,
                    "error_message": outcome.error_message,
                }
            except (FileNotFoundError, ValueError, click.ClickException) as error:
                if effects_started:
                    raise ProviderMutationUnknownError(str(error)) from error
                result = {
                    "rollback_status": "fail",
                    "error_code": "rollback_not_ready",
                    "error_message": str(error),
                }
            return ProviderMutationOutcome(
                response_status_code=202,
                response_payload=provider_operation_response_payload(
                    trace_id=trace_id, records={}, result=result
                ),
                durable=True,
                provider_effect_performed=effects_started,
            )

    adapter = RollbackDrillAdapter(
        control_plane_root=control_plane_root,
        record_store=store,
        promotion_request=GenericWebProdPromotionEnvelope(
            product=profile.product,
            promotion=GenericWebProdPromotionRequest(
                product=profile.product,
                artifact_id=planned.artifact_id,
                source_git_ref=planned.source_git_ref,
            ),
        ),
        profile=profile,
        lane=lane,
        trace_id=trace_id,
        validate_before_effect=lambda _target: validate_checkpoint(),
    )
    reservation_result = run_durable_provider_operation(
        store=store,
        scope=scope,
        route_path=GENERIC_WEB_ROLLBACK_ROUTE,
        idempotency_key=idempotency_key,
        request_fingerprint=hashlib.sha256(
            (request.model_dump_json() + planned.model_dump_json()).encode()
        ).hexdigest(),
        lease_owner=f"{trace_id}-{datetime.now(UTC).isoformat()}",
        response_trace_id=trace_id,
        adapter=adapter,
    )
    return reservation_result.record.record_id if reservation_result.record is not None else ""


def rollback_drill_previous_inventory(
    *,
    store: PostgresRecordStore,
    decision: ReleaseReviewDecisionRecord,
    deployment: DeploymentRecord,
    scope: str,
) -> tuple[EnvironmentInventory, PromotionRecord | None]:
    """Keep the baseline lineage pinned by the first promotion, when it matches."""
    inventory = build_environment_inventory(
        deployment_record=deployment, updated_at=utc_now_timestamp()
    )
    reservation = store.read_idempotency_record(
        scope=scope,
        route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
        idempotency_key=f"{decision.record_id}:promote-1",
    )
    if reservation is not None:
        promotion_id = reservation.response_payload.get("result", {}).get("promotion_record_id")
        if isinstance(promotion_id, str) and promotion_id:
            promotion = store.read_promotion_record(promotion_id)
            if promotion.rollback.target_deployment_record_id == deployment.record_id:
                return inventory.model_copy(
                    update={
                        "promotion_record_id": promotion.rollback.target_promotion_record_id,
                        "promoted_from_instance": promotion.rollback.target_promoted_from_instance,
                    }
                ), promotion
    return inventory, None
