"""Recovery of a failed Odoo release, bound to its admitted acceptance."""

from __future__ import annotations

from control_plane.contracts.deployment_record import deployment_record_passed
from control_plane.contracts.odoo_prod_promotion_operation import OdooProdPromotionOperationRecord
from control_plane.contracts.odoo_prod_rollback_operation import (
    ODOO_PROD_ROLLBACK_ACTION,
    OdooProdRollbackCheckpoint,
    OdooProdRollbackOperationRecord,
    OdooProdRollbackRequest,
    OdooProdRollbackTarget,
    build_odoo_prod_rollback_operation_id,
    odoo_prod_rollback_request_fingerprint,
)
from control_plane.contracts.release_review import ReleaseReviewDecisionRecord
from control_plane.storage.postgres import PostgresRecordStore

RECOVERY_SOURCE_KEY = "recovery_promotion_operation_id"
PRODUCTION_WRITE_KEY = "production_write_started"


def pin_odoo_release_recovery_target(
    store: PostgresRecordStore, decision: ReleaseReviewDecisionRecord, context: str
) -> dict[str, str]:
    """Pin the checklist's passing production deployment before any deploy effect."""

    deployment = next(
        (
            record
            for record in store.list_deployment_records(context_name=context, instance_name="prod")
            if record.artifact_identity is not None
            and record.artifact_identity.artifact_id == decision.checklist.production.artifact_id
            and deployment_record_passed(record)
        ),
        None,
    )
    if deployment is None:
        raise ValueError("The accepted release has no passing production recovery target.")
    return {
        "recovery_artifact_id": decision.checklist.production.artifact_id,
        "recovery_deployment_record_id": deployment.record_id,
    }


def require_odoo_release_recovery_pin(
    store: PostgresRecordStore, operation: OdooProdPromotionOperationRecord
) -> None:
    decision = store.read_release_review_decision_record(
        product=operation.product, record_id=operation.authorization.release_decision_record_id
    )
    pin = operation.checkpoints[0].evidence if operation.checkpoints else {}
    deployment = store.read_deployment_record(pin.get("recovery_deployment_record_id", ""))
    if (
        pin.get("recovery_artifact_id") != decision.checklist.production.artifact_id
        or deployment.context != operation.context
        or deployment.instance != operation.instance
        or not deployment_record_passed(deployment)
        or deployment.artifact_identity is None
        or deployment.artifact_identity.artifact_id != pin.get("recovery_artifact_id")
    ):
        raise ValueError(
            "The pinned Odoo release recovery deployment is unavailable or no longer passing."
        )


def odoo_release_wrote_production(operation: object) -> bool:
    return any(
        checkpoint.evidence.get(PRODUCTION_WRITE_KEY) == "true"
        for checkpoint in getattr(operation, "checkpoints", ())
    )


def build_odoo_release_recovery(
    store: PostgresRecordStore, source: OdooProdPromotionOperationRecord
) -> OdooProdRollbackOperationRecord | None:
    if (
        source.status != "fail"
        or source.authorization.grant != "client_release_acceptance"
        or source.idempotency_scope != "client-release"
        or not odoo_release_wrote_production(source)
        or source.result is None
        or source.result.run_status != "fail"
        or not source.result.promotion_record_id
    ):
        return None
    try:
        decision = store.read_release_review_decision_record(
            product=source.product, record_id=source.authorization.release_decision_record_id
        )
        pin = source.checkpoints[0].evidence
        target = OdooProdRollbackTarget(
            artifact_id=pin["recovery_artifact_id"],
            deployment_record_id=pin["recovery_deployment_record_id"],
        )
        deployment = store.read_deployment_record(target.deployment_record_id)
        promotion = store.read_promotion_record(source.result.promotion_record_id)
    except (FileNotFoundError, KeyError, IndexError, ValueError):
        return None
    if (
        decision.decision != "accepted"
        or not decision.release_start
        or not decision.release_issue_url
        or decision.product != source.product
        or decision.actor_github_id != str(source.authorization.caller.github_id)
        or source.request.expected_artifact_id != decision.checklist.candidate.artifact_id
        or source.result.artifact_id != decision.checklist.candidate.artifact_id
        or target.artifact_id != decision.checklist.production.artifact_id
        or deployment.context != source.context
        or deployment.instance != source.instance
        or not deployment_record_passed(deployment)
        or deployment.artifact_identity is None
        or deployment.artifact_identity.artifact_id != target.artifact_id
        or promotion.context != source.context
        or promotion.to_instance != source.instance
        or promotion.artifact_identity.artifact_id != source.result.artifact_id
    ):
        return None
    key = f"{source.operation_id}:failure-recovery"
    request = OdooProdRollbackRequest(
        context=source.context,
        artifact_id=target.artifact_id,
        promotion_record_id=promotion.record_id,
        reason=f"Automatic recovery of failed Client release operation {source.operation_id}.",
    )
    # This is compensation for the effect already admitted under this decision,
    # not a fresh forward release or a caller's manual rollback permission.
    authorization = source.authorization.model_copy(update={"action": ODOO_PROD_ROLLBACK_ACTION})
    return OdooProdRollbackOperationRecord(
        operation_id=build_odoo_prod_rollback_operation_id(
            product=source.product,
            context=source.context,
            idempotency_key=key,
            idempotency_scope=source.idempotency_scope,
        ),
        product=source.product,
        context=source.context,
        instance="prod",
        idempotency_key=key,
        idempotency_scope=source.idempotency_scope,
        request_fingerprint=odoo_prod_rollback_request_fingerprint(
            product=source.product, request=request
        ),
        request=request,
        target=target,
        authorization=authorization,
        created_at=source.finished_at,
        updated_at=source.finished_at,
        checkpoints=(
            OdooProdRollbackCheckpoint(
                phase="created",
                recorded_at=source.finished_at,
                evidence={RECOVERY_SOURCE_KEY: source.operation_id},
            ),
        ),
    )


def odoo_release_recovery_source(operation: OdooProdRollbackOperationRecord) -> str:
    return (
        operation.checkpoints[0].evidence.get(RECOVERY_SOURCE_KEY, "")
        if operation.checkpoints
        else ""
    )


def odoo_release_recovery_allows(store: object, operation: OdooProdRollbackOperationRecord) -> bool:
    if not isinstance(store, PostgresRecordStore):
        return False
    try:
        source = store.read_odoo_prod_promotion_operation_record(
            odoo_release_recovery_source(operation)
        )
    except FileNotFoundError:
        return False
    expected = build_odoo_release_recovery(store, source)
    return expected is not None and all(
        getattr(operation, field) == getattr(expected, field)
        for field in (
            "operation_id",
            "product",
            "context",
            "instance",
            "idempotency_key",
            "idempotency_scope",
            "request_fingerprint",
            "request",
            "target",
            "authorization",
        )
    )


def read_odoo_release_recovery(
    store: PostgresRecordStore, source: OdooProdPromotionOperationRecord
) -> OdooProdRollbackOperationRecord | None:
    key = f"{source.operation_id}:failure-recovery"
    operation_id = build_odoo_prod_rollback_operation_id(
        product=source.product,
        context=source.context,
        idempotency_key=key,
        idempotency_scope=source.idempotency_scope,
    )
    try:
        recovery = store.read_odoo_prod_rollback_operation_record(operation_id)
    except FileNotFoundError:
        return None
    return recovery if odoo_release_recovery_source(recovery) == source.operation_id else None
