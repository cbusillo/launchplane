"""Inspect and adopt one existing Client release; never repeat a provider effect."""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import click
from pydantic import BaseModel

from control_plane.client_release import (
    CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
    ClientReleaseStep,
    client_release_step_operation_id,
    client_release_steps,
    client_release_promotion_request,
)
from control_plane.contracts.generic_web_deploy_recovery import (
    build_generic_web_deploy_recovery_digest,
)
from control_plane.contracts.generic_web_promotion_recovery import (
    PromotionRecoveryAction,
    PromotionRecoveryPlan,
)
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    parse_launchplane_mutation_timestamp,
)
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.promotion_record import PromotionRecord, promotion_failure
from control_plane.contracts.record_failures import record_failure_summary
from control_plane.generic_web_promotion_http import GENERIC_WEB_PROD_PROMOTION_ROUTE
from control_plane.generic_web_promotion_provider_adapter import (
    require_generic_web_promotion_target,
    generic_web_promotion_deployment_id,
)
from control_plane.provider_operations import build_provider_operation_key
from control_plane.release_review import checklist_digest
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebDeployRuntimeArtifactProvider,
    build_generic_web_provider_target_key,
    default_generic_web_deploy_provider,
    evaluate_generic_web_runtime_close_out,
    resolve_generic_web_provider_reconciliation_target,
)
from control_plane.workflows.generic_web_promotion import (
    _result_from_record,
    _verify_health_evidence_with_identity,
    _health_evidence_for_lane,
    _build_promotion_record,
    _mark_health_failed,
)
from control_plane.workflows.inventory import build_environment_inventory
from control_plane.workflows.ship import utc_now_timestamp


class PromotionRecoveryConflict(ValueError):
    """The stored release does not identify one authoritative promotion."""


@dataclass
class PromotionInspection:
    reservation: LaunchplaneIdempotencyRecord
    evidence: tuple[BaseModel, ...]
    action: PromotionRecoveryAction = "hold_unknown"
    provider_evidence: dict[str, object] = field(default_factory=dict)
    result: dict[str, object] = field(default_factory=dict)
    inventory: EnvironmentInventory | None = None
    promotion: PromotionRecord | None = None
    deployment: DeploymentRecord | None = None

    def plan(self, product: str, reason: str) -> PromotionRecoveryPlan:
        digest = build_generic_web_deploy_recovery_digest(
            {
                "reservation": self.reservation.model_dump(mode="json"),
                "records": [record.model_dump(mode="json") for record in self.evidence],
                "provider": self.provider_evidence,
                "action": self.action,
                "reason": reason,
            }
        )
        return PromotionRecoveryPlan(
            product=product,
            recovery_reference=self.reservation.record_id,
            reservation_state=self.reservation.state,
            reservation_attempt=self.reservation.attempt,
            checkpoint=self.reservation.provider_effect_phase,
            proposed_action=self.action,
            recovery_digest=digest,
        )


def inspect_promotion(
    *,
    store: PostgresRecordStore,
    root: Path,
    product: str,
    decision_record_id: str,
    attempt: int,
    inspect_provider: bool = True,
) -> PromotionInspection:
    profile = store.read_product_profile_record(product)
    lane = next((item for item in profile.lanes if item.instance == "prod"), None)
    decision = store.read_release_review_decision_record(
        product=product, record_id=decision_record_id
    )
    if lane is None or profile.driver_id != "generic-web":
        raise FileNotFoundError("Promotion release not found.")
    step = ClientReleaseStep("promote", attempt)
    if (
        decision.decision != "accepted"
        or step not in client_release_steps(decision.release_start)
        or checklist_digest(decision.checklist) != decision.checklist_digest
    ):
        raise PromotionRecoveryConflict("Promotion acceptance is not authoritative.")
    backup_id = client_release_step_operation_id(
        profile=profile, decision=decision, step=ClientReleaseStep("backup", attempt)
    )
    backup_operation = store.read_verireel_prod_backup_gate_operation_record(backup_id)
    promotion_request = client_release_promotion_request(
        profile, decision, backup_operation.backup_record_id
    )
    fingerprint = hashlib.sha256(promotion_request.model_dump_json().encode()).hexdigest()
    lookup = store.lookup_existing_mutation_reservation(
        route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
        idempotency_key=f"{decision.record_id}:{step.name}",
        request_fingerprint=fingerprint,
    )
    if lookup.status == "missing":
        raise FileNotFoundError("Promotion reservation not found.")
    reservation = lookup.record
    if (
        lookup.status != "found"
        or reservation is None
        or (
            reservation.scope != CLIENT_RELEASE_IDEMPOTENCY_SCOPE
            or reservation.record_id
            != client_release_step_operation_id(profile=profile, decision=decision, step=step)
        )
    ):
        raise PromotionRecoveryConflict("Promotion reservation identity conflicts.")
    inspection = PromotionInspection(reservation, (profile, decision, backup_operation))
    if reservation.state == "completed":
        inspection.action = "replay_completed"
        return inspection
    if reservation.state == "running":
        if parse_launchplane_mutation_timestamp(
            reservation.lease_expires_at, field_name="lease_expires_at"
        ) > parse_launchplane_mutation_timestamp(lookup.observed_at, field_name="observed_at"):
            inspection.action = "wait_for_active_lease"
            return inspection
    if not inspect_provider:
        return inspection

    # These reads deliberately do not materialize provider or deployment records.
    # A final promotion is evidence of finished checks; a pending check never
    # becomes success merely because the image was deployed.
    try:
        target = resolve_generic_web_provider_reconciliation_target(
            reconciliation_key=reservation.reconciliation_key,
            request_artifact_id=promotion_request.artifact_id,
            normalized_artifact_id=promotion_request.artifact_id,
            request_source_git_ref=promotion_request.source_git_ref,
            request_timeout_seconds=promotion_request.timeout_seconds,
            request_no_cache=promotion_request.no_cache,
            lane=lane,
        )
        if build_generic_web_provider_target_key(target) != reservation.provider_target_key:
            raise PromotionRecoveryConflict("Promotion target conflicts.")
        require_generic_web_promotion_target(
            record_store=store, lane=lane, resolved_deploy_target=target
        )
        operation_key = build_provider_operation_key(
            scope=reservation.scope,
            route_path=reservation.route_path,
            idempotency_key=reservation.idempotency_key,
            request_fingerprint=reservation.request_fingerprint,
            reconciliation_key=reservation.reconciliation_key,
        )
        deployment_id = generic_web_promotion_deployment_id(operation_key, lane)
        promotions = tuple(
            item
            for item in store.list_promotion_records(
                context_name=lane.context,
                from_instance_name="testing",
                to_instance_name="prod",
                recovery_backup_record_id=promotion_request.backup_record_id,
                recovery_deployment_record_id=deployment_id,
                limit=2,
            )
            if item.deployment_record_id == deployment_id
            or (
                not item.deployment_record_id
                and item.backup_record_id == promotion_request.backup_record_id
            )
        )
        if len(promotions) != 1:
            return inspection
        promotion = promotions[0]
        backup = store.read_backup_gate_record(promotion_request.backup_record_id)
        deployed = store.read_deployment_record(deployment_id)
        inventory = store.read_environment_inventory(
            context_name=lane.context, instance_name="prod"
        )
        recorded_target = store.read_provider_target_record(
            context_name=lane.context, instance_name="prod"
        )
        inspection.evidence += (recorded_target, backup, promotion, deployed, inventory)
        if (
            promotion.artifact_identity.artifact_id != promotion_request.artifact_id
            or promotion.backup_record_id != promotion_request.backup_record_id
            or promotion.backup_gate.status != "pass"
            or backup.status != "pass"
            or promotion.source_health.status not in {"pass", "skipped"}
            or deployed.source_git_ref != promotion_request.source_git_ref
            or deployed.artifact_identity is None
            or deployed.artifact_identity.artifact_id != promotion_request.artifact_id
            or deployed.deployed_target != target.deployed_target
        ):
            return inspection
        finishing_promotion = (
            promotion.deploy.status in {"pending", "pass"}
            and promotion.destination_health.status in {"pending", "pass"}
            and promotion.failure is None
            and not promotion.rollback.attempted
            and not reservation.provider_effect_phase.startswith("rollback_")
        )
        if promotion.rollback.status == "fail":
            return inspection
        interrupted_rollback = (
            not promotion.rollback.attempted
            and reservation.provider_effect_phase.startswith("rollback_")
            and bool(promotion.rollback.target_deployment_record_id)
        )
        finishing_rollback = interrupted_rollback or (
            promotion.rollback.attempted and promotion.rollback.status in {"pending", "pass"}
        )
        if finishing_promotion:
            effective = deployed
            action: PromotionRecoveryAction = "adopt_promotion"
        elif finishing_rollback:
            rollback_deployment_id = promotion.rollback.deployment_record_id
            if interrupted_rollback:
                rollback_deployment_id = deployment_id + "-rollback"
            if rollback_deployment_id != deployment_id + "-rollback":
                return inspection
            previous = store.read_deployment_record(promotion.rollback.target_deployment_record_id)
            effective = store.read_deployment_record(rollback_deployment_id)
            inspection.evidence += (previous, effective)
            if (
                previous.artifact_identity is None
                or effective.artifact_identity is None
                or previous.artifact_identity.artifact_id
                != decision.checklist.production.artifact_id
                or previous.source_git_ref != decision.checklist.production.source_commit
                or effective.artifact_identity != previous.artifact_identity
                or effective.source_git_ref != previous.source_git_ref
            ):
                return inspection
            action = "adopt_rollback"
        else:
            return inspection
        if (
            effective.deploy.status != "pass"
            or effective.post_deploy_update.status not in {"pass", "skipped"}
            or effective.deployed_target != target.deployed_target
            or effective.runtime_identity is None
            or effective.artifact_identity is None
            or effective.runtime_identity.deployment_record_id != effective.record_id
            or (effective.context, effective.instance) != (lane.context, lane.instance)
            or effective.runtime_identity.context != lane.context
            or effective.runtime_identity.instance != lane.instance
            or effective.runtime_identity.artifact_id != effective.artifact_identity.artifact_id
            or effective.runtime_identity.source_git_ref != effective.source_git_ref
        ):
            return inspection
        # Inventory can lag the final record after a crash, but must still name
        # this operation or the production version originally accepted.
        inventory_is_failed_candidate = (
            action == "adopt_rollback"
            and inventory.runtime_identity is not None
            and inventory.runtime_identity.deployment_record_id == deployed.record_id
            and inventory.runtime_identity.artifact_id == promotion_request.artifact_id
            and inventory.runtime_identity.source_git_ref == promotion_request.source_git_ref
        )
        if inventory.runtime_identity is None or (
            inventory.runtime_identity != effective.runtime_identity
            and not inventory_is_failed_candidate
            and (
                inventory.artifact_identity is None
                or inventory.artifact_identity.artifact_id
                != decision.checklist.production.artifact_id
                or inventory.source_git_ref != decision.checklist.production.source_commit
            )
        ):
            return inspection
        provider = default_generic_web_deploy_provider()
        if not isinstance(provider, GenericWebDeployRuntimeArtifactProvider):
            return inspection
        effective_target = target.model_copy(
            update={
                "ship_request": target.ship_request.model_copy(
                    update={
                        "artifact_id": effective.artifact_identity.artifact_id,
                        "source_git_ref": effective.source_git_ref,
                    }
                )
            }
        )
        runtime = provider.observe_runtime_artifact(
            control_plane_root=root, resolved_deploy_target=effective_target
        )
        evaluate_generic_web_runtime_close_out(
            observation=runtime,
            expected_artifact_reference=effective.artifact_identity.artifact_id,
            expected_deployment_record_id=effective.record_id,
        )
        checked = _verify_health_evidence_with_identity(
            _health_evidence_for_lane(
                lane=lane,
                request=promotion_request,
                health_path=profile.health_path,
                status="pending",
            ),
            expected_runtime_identity=effective.runtime_identity,
        )
        if checked.status != "pass" or checked.runtime_identity_status != "match":
            return inspection
        inspection.provider_evidence = {
            "runtime": runtime.model_dump(mode="json"),
            "health_status": checked.status,
            "runtime_identity_status": checked.runtime_identity_status,
            "observed_runtime_identity": checked.observed_runtime_identity.model_dump(mode="json")
            if checked.observed_runtime_identity is not None
            else None,
        }
        effective = effective.model_copy(
            update={"destination_health": checked, "verify_destination_health": True}
        )
        if finishing_promotion:
            promotion = _build_promotion_record(
                request=promotion_request,
                promotion_record_id=promotion.record_id,
                context=lane.context,
                source_health=promotion.source_health,
                backup_gate=promotion.backup_gate,
                destination_health=checked,
                deployment_record=deployed,
                deployment_status="pass",
                target_name=deployed.deploy.target_name,
                target_type=deployed.deploy.target_type,
                deployment_record_id=deployed.record_id,
            ).model_copy(update={"rollback": promotion.rollback})
            deployed = effective
        else:
            rollback = promotion.rollback.model_copy(
                update={
                    "attempted": True,
                    "status": "pass",
                    "detail": record_failure_summary("rollback_passed"),
                    "deployment_record_id": effective.record_id,
                    "finished_at": effective.deploy.finished_at,
                }
            )
            if interrupted_rollback:
                # A rollback checkpoint proves the admitted promotion failed. Its
                # exact deployment distinguishes deploy failure from health failure.
                failure_code = (
                    "destination_health_failed"
                    if deployed.deploy.status == "pass"
                    else "destination_deploy_failed"
                )
                promotion = _build_promotion_record(
                    request=promotion_request,
                    promotion_record_id=promotion.record_id,
                    context=lane.context,
                    source_health=promotion.source_health,
                    backup_gate=promotion.backup_gate,
                    destination_health=_mark_health_failed(promotion.destination_health),
                    deployment_record=deployed,
                    deployment_status="fail",
                    target_name=deployed.deploy.target_name,
                    target_type=deployed.deploy.target_type,
                    deployment_record_id=deployed.record_id,
                ).model_copy(update={"failure": promotion_failure(failure_code)})
            promotion = promotion.model_copy(
                update={"rollback": rollback, "rollback_health": checked}
            )
        inspection.promotion = promotion
        inspection.deployment = effective
        inspection.inventory = build_environment_inventory(
            deployment_record=effective,
            updated_at=utc_now_timestamp(),
            promotion_record_id=promotion.record_id
            if finishing_promotion
            else (promotion.rollback.target_promotion_record_id or inventory.promotion_record_id),
            promoted_from_instance="testing"
            if finishing_promotion
            else (
                promotion.rollback.target_promoted_from_instance or inventory.promoted_from_instance
            ),
        )
        inspection.result = _result_from_record(
            request=promotion_request,
            record=promotion,
            deployment_record=deployed,
            inventory_record_id=f"{lane.context}-prod",
            target_id=target.resolved_target.target_id,
            dry_run=False,
            error_message="",
        ).model_dump(mode="json")
        inspection.action = action
    except (FileNotFoundError, ValueError, click.ClickException):
        # Missing, conflicting or unobservable evidence never releases a fence.
        inspection.action = "hold_unknown"
    return inspection
