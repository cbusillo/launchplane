from dataclasses import dataclass
from pathlib import Path

from control_plane.client_release import (
    CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
    ClientReleaseNotReady,
    _covering_decision,
    _prod_context,
    _require_current_release,
    _step_key,
    client_release_grant,
    client_release_grant_allows,
    client_release_step_operation_id,
    client_release_steps,
    read_client_release_step_views,
)
from control_plane.client_release_backup_continuation import (
    BackupContinuationRefused,
    BackupContinuationResult,
    BackupContinuationReview,
)
from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256
from control_plane.contracts.odoo_prod_promotion_operation import (
    DEFAULT_ODOO_STABLE_WORKER_MAX_ATTEMPTS,
    ODOO_PROD_PROMOTION_RUN_ACTION,
    OdooProdPromotionOperationRecord,
    OdooProdPromotionRunRequest,
    odoo_prod_promotion_request_fingerprint,
)
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementRequest,
)
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.release_review import ReleaseReviewDecisionRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.odoo_business_hours import require_release_window
from control_plane.odoo_release_recovery import (
    odoo_release_wrote_production,
    require_odoo_release_recovery_pin,
)
from control_plane.service_deploy_drain import read_status
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.odoo_prod_promotion_run import admit_odoo_prod_promotion_run
from control_plane.workflows.odoo_prod_backup_gate import BACKUP_GATE_SOURCE
from control_plane.workflows.odoo_stable_target_replacement import (
    build_odoo_stable_target_replacement_plan,
    OdooStableTargetReplacementPlan,
)
from control_plane.workflows.production_promotion_backup import (
    ODOO_PROMOTION_BACKUP_ACTION,
    promotion_is_temporary_admission_refusal,
    require_production_promotion_backup,
)
from control_plane.workflows.ship import utc_now_timestamp


@dataclass(frozen=True)
class OdooContinuationEvidence:
    operation: OdooProdPromotionOperationRecord
    promotion: PromotionRecord
    digest: str


def require_pre_effect_original(
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
) -> tuple[OdooProdPromotionOperationRecord, PromotionRecord]:
    steps = client_release_steps(decision.release_start)
    views = read_client_release_step_views(store=store, profile=profile, decision=decision)
    index = next((i for i, view in enumerate(views) if view.status != "pass"), len(views))
    if (
        decision.decision != "accepted"
        or profile.driver_id != "odoo"
        or index >= len(steps)
        or steps[index].kind != "promote"
        or index == 0
        or len(views) != len(steps)
        or any(view.status != "not_started" for view in views[index + 1 :])
    ):
        raise BackupContinuationRefused("odoo_original_not_continuable")
    step = steps[index]
    operation = store.read_odoo_prod_promotion_operation_record(
        client_release_step_operation_id(profile=profile, decision=decision, step=step)
    )
    backup = store.read_verireel_prod_backup_gate_operation_record(views[index - 1].operation_id)
    expected_request = OdooProdPromotionRunRequest(
        product=profile.product,
        context=_prod_context(profile),
        request_id=f"client-release-{step.name}-{decision.record_id}",
        infrastructure_backup_record_id=backup.backup_record_id,
        expected_artifact_id=decision.checklist.candidate.artifact_id,
    )
    result = operation.result
    if (
        operation.status != "fail"
        or operation.phase != "failed"
        or not operation.finished_at
        or operation.error_code != "promotion_fail"
        or operation.cancellation is not None
        or operation.request != expected_request
        or operation.request_fingerprint
        != odoo_prod_promotion_request_fingerprint(expected_request)
        or operation.idempotency_scope != CLIENT_RELEASE_IDEMPOTENCY_SCOPE
        or operation.idempotency_key != _step_key(decision, step)
        or operation.authorization
        != client_release_grant(
            decision=decision,
            action=ODOO_PROD_PROMOTION_RUN_ACTION,
            context=operation.context,
            authorized_at=operation.authorization.authorized_at,
        )
        or not client_release_grant_allows(store, operation.authorization)
        or operation.attempt >= DEFAULT_ODOO_STABLE_WORKER_MAX_ATTEMPTS
        or result is None
        or result.run_status != "fail"
        or result.input_status != "ready"
        or result.backup_status != "pass"
        or result.promotion_status != "fail"
        or result.deployment_status != "fail"
        or result.post_deploy_status != "skipped"
        or result.destination_health_status != "skipped"
        or result.deployment_record_id
        or not result.promotion_record_id
        or result.request_id != operation.request.request_id
        or result.artifact_id != decision.checklist.candidate.artifact_id
        or result.source_git_ref != decision.checklist.candidate.source_commit
        or result.infrastructure_backup_record_id != backup.backup_record_id
        or odoo_release_wrote_production(operation)
        or not operation.checkpoints
        or operation.checkpoints[0].phase != "created"
        or operation.checkpoints[-1].phase != "promotion_started"
        or not {"validated", "logical_backup_started", "logical_backup_completed"}.issubset(
            checkpoint.phase for checkpoint in operation.checkpoints
        )
    ):
        raise BackupContinuationRefused("odoo_original_effects_not_proven_absent")
    promotion = store.read_promotion_record(result.promotion_record_id)
    logical = store.read_backup_gate_record(result.backup_record_id)
    evidence = promotion.backup_gate.evidence
    if (
        not promotion_is_temporary_admission_refusal(promotion)
        or evidence.get("infrastructure_provider_effects_status") != "not_started"
        or (promotion.context, promotion.from_instance, promotion.to_instance)
        != (operation.context, operation.request.from_instance, operation.instance)
        or promotion.artifact_identity.artifact_id != result.artifact_id
        or promotion.backup_record_id != result.backup_record_id
        or evidence.get("infrastructure_backup_record_id") != backup.backup_record_id
        or evidence.get("infrastructure_backup_operation_id") != backup.operation_id
        or logical.source != BACKUP_GATE_SOURCE
        or logical.status != "pass"
        or not logical.required
        or (logical.context, logical.instance) != (operation.context, operation.instance)
        or any(evidence.get(key) != value for key, value in logical.evidence.items())
    ):
        raise BackupContinuationRefused("odoo_original_receipt_mismatch")
    require_odoo_release_recovery_pin(store, operation)
    return operation, promotion


def qualify_odoo_continuation(
    store: PostgresRecordStore,
    root: Path,
    product: str,
    decision_record_id: str,
    review: BackupContinuationReview,
    actor: str,
) -> OdooContinuationEvidence:
    covered = _covering_decision(store, product, decision_record_id=decision_record_id)
    if covered is None:
        raise BackupContinuationRefused("release_acceptance_no_longer_current")
    profile, decision = covered
    if read_status(store).get("admission_paused"):
        raise BackupContinuationRefused("service_deploy_draining")
    _require_current_release(store, root, profile, decision)
    require_release_window(store=store, profile=profile, decision=decision, control_plane_root=root)
    operation, promotion = require_pre_effect_original(store, profile, decision)
    assert operation.result is not None
    backup = require_production_promotion_backup(
        record_store=store,
        product=product,
        context=operation.context,
        instance="prod",
        promotion_action=ODOO_PROMOTION_BACKUP_ACTION,
        backup_record_id=operation.request.infrastructure_backup_record_id,
    )
    admission = admit_odoo_prod_promotion_run(
        control_plane_root=root,
        record_store=store,
        request=operation.request,
    )
    if (
        admission.blocked_reason
        or admission.inputs_result.artifact_id != operation.result.artifact_id
    ):
        raise ClientReleaseNotReady("promotion_not_ready")
    plan, baseline = qualified_current_odoo_target(store, root, operation, decision)
    digest = nonvalidating_compact_sorted_json_sha256(
        {
            "operation": operation.model_dump(mode="json"),
            "promotion": promotion.model_dump(mode="json"),
            "profile": profile.model_dump(mode="json"),
            "decision": decision.model_dump(mode="json"),
            "baseline": baseline.model_dump(mode="json"),
            "plan": plan.model_dump(mode="json"),
            "backup": {
                key: value
                for key, value in backup.evidence.items()
                if key != "admission_checked_at"
            },
            "actor": actor,
            "reason": review.reason,
        }
    )
    return OdooContinuationEvidence(operation, promotion, digest)


def qualified_current_odoo_target(
    store: PostgresRecordStore,
    root: Path,
    operation: OdooProdPromotionOperationRecord,
    decision: ReleaseReviewDecisionRecord,
) -> tuple[OdooStableTargetReplacementPlan, DeploymentRecord]:
    plan = build_odoo_stable_target_replacement_plan(
        control_plane_root=root,
        record_store=store,
        request=OdooStableTargetReplacementRequest(
            product=operation.product,
            instance="prod",
            artifact_id=decision.checklist.candidate.artifact_id,
            source_git_ref=decision.checklist.candidate.source_commit,
            allow_empty_data=True,
            expected_current_artifact_id=decision.checklist.production.artifact_id,
        ),
    )
    target = plan.current_target
    baseline = store.read_deployment_record(
        operation.checkpoints[0].evidence["recovery_deployment_record_id"]
    )
    deployed = baseline.deployed_target
    if (
        plan.plan_status != "ready"
        or plan.strategy != "recreate-in-place"
        or (plan.product, plan.context, plan.instance)
        != (operation.product, operation.context, "prod")
        or plan.expected_artifact_id != operation.request.expected_artifact_id
        or plan.expected_source_git_ref != decision.checklist.candidate.source_commit
        or target is None
        or deployed is None
        or deployed.provider_id != "dokploy"
        or (target.target_id, target.target_type)
        != (deployed.target_id, deployed.provider_target_type)
        or not target.runtime_identity_present
        or target.runtime_identity_deployment_record_id != baseline.record_id
    ):
        raise BackupContinuationRefused("odoo_current_target_or_plan_changed")
    return plan, baseline


def require_continued_odoo_target(
    store: PostgresRecordStore,
    root: Path,
    operation: OdooProdPromotionOperationRecord,
) -> None:
    if operation.previous_attempt is None or odoo_release_wrote_production(operation):
        return
    decision = store.read_release_review_decision_record(
        product=operation.product,
        record_id=operation.authorization.release_decision_record_id,
    )
    profile = store.read_product_profile_record(operation.product)
    _require_current_release(store, root, profile, decision)
    qualified_current_odoo_target(store, root, operation, decision)


def continuation_replayed(
    operation: OdooProdPromotionOperationRecord,
    review: BackupContinuationReview,
    actor: str,
) -> bool:
    if not review.apply or not review.expected_digest:
        return False
    while True:
        previous = operation.previous_attempt
        if previous is None:
            return False
        if operation.continuation_digest == review.expected_digest:
            if (operation.continuation_actor, operation.continuation_reason) != (
                actor,
                review.reason,
            ):
                raise BackupContinuationRefused("backup_continuation_replay_conflict")
            return True
        operation = previous


def continue_release_odoo_promotion(
    *,
    store: PostgresRecordStore,
    root: Path,
    product: str,
    decision_record_id: str,
    review: BackupContinuationReview,
    actor: str,
    trace_id: str,
) -> BackupContinuationResult:
    for operation in store.list_odoo_prod_promotion_operation_records(product=product):
        if (
            operation.authorization.release_decision_record_id == decision_record_id
            and continuation_replayed(operation, review, actor)
        ):
            return BackupContinuationResult(
                trace_id=trace_id,
                decision_record_id=decision_record_id,
                operation_id=operation.operation_id,
                review_digest=review.expected_digest,
                status="replayed",
            )
    evidence = qualify_odoo_continuation(store, root, product, decision_record_id, review, actor)
    if review.apply:
        if review.expected_digest != evidence.digest:
            raise BackupContinuationRefused("backup_continuation_review_changed")
        now = utc_now_timestamp()
        operation = evidence.operation
        resumed = operation.model_copy(
            update={
                "previous_attempt": operation,
                "previous_promotion": evidence.promotion,
                "continuation_digest": evidence.digest,
                "continuation_actor": actor,
                "continuation_reason": review.reason,
                "continued_at": now,
                "status": "pending",
                "phase": "created",
                "updated_at": now,
                "started_at": "",
                "finished_at": "",
                "lease_owner": "",
                "lease_expires_at": "",
                "heartbeat_at": "",
                "result": None,
                "error_code": "",
                "error_message": "",
                "checkpoints": (operation.checkpoints[0],),
            }
        )
        resumed = OdooProdPromotionOperationRecord.model_validate(resumed.model_dump(mode="json"))
        if not store.continue_pre_effect_odoo_promotion_operation(
            expected=evidence.operation,
            resumed=resumed,
            root=root,
            review=review,
            actor=actor,
        ):
            current = store.read_odoo_prod_promotion_operation_record(operation.operation_id)
            if not continuation_replayed(current, review, actor):
                raise BackupContinuationRefused("backup_continuation_review_changed")
            return BackupContinuationResult(
                trace_id=trace_id,
                decision_record_id=decision_record_id,
                operation_id=operation.operation_id,
                review_digest=evidence.digest,
                status="replayed",
            )
    return BackupContinuationResult(
        trace_id=trace_id,
        decision_record_id=decision_record_id,
        operation_id=evidence.operation.operation_id,
        review_digest=evidence.digest,
        status="queued" if review.apply else "ready",
    )
