from control_plane.contracts.odoo_cutover import OdooCutoverRecord
from control_plane.contracts.prepared_public_site import PublicPauseRecord
from control_plane.prepared_public_site import verify_snapshot
from control_plane.odoo_cutover_storage import DeploymentStore
from control_plane.contracts.odoo_prod_promotion_operation import OdooProdPromotionOperationRecord
from control_plane.odoo_cutover_plan import cutover_identity


class DefiniteBackupFailure(ValueError):
    pass


def operation_cutover(
    store: DeploymentStore, operation: OdooProdPromotionOperationRecord
) -> OdooCutoverRecord | None:
    identity = cutover_identity(
        operation.product, operation.context, operation.instance, operation.request.request_id
    )
    try:
        deployment = store.read_deployment_record("deployment-cutover-" + identity)
    except FileNotFoundError:
        return None
    record = deployment.odoo_cutover
    if record is None:
        return None
    plan = record.plan
    return (
        record
        if (
            record.record_id,
            plan.new.binding.product_id,
            plan.new.runtime_identity.context,
            plan.new.binding.environment_id,
            plan.release_id,
            plan.new.runtime_identity.deployment_record_id,
        )
        == (
            identity,
            operation.product,
            operation.context,
            operation.instance,
            operation.request.request_id,
            deployment.record_id,
        )
        else None
    )


def operation_aborted(store: DeploymentStore, operation: OdooProdPromotionOperationRecord) -> bool:
    record = operation_cutover(store, operation)
    return record is not None and record.step == "aborted" and abort_reconciliation_eligible(record)


def backup_recovery_eligible(record: OdooCutoverRecord) -> bool:
    if (
        record.step not in {"backing_up", "aborting"}
        or record.backup is not None
        or record.maintenance is not None
        or record.website_settings_attempted
        or record.rollback_pause is not None
    ):
        return False
    pause, site, plan = record.pause, record.public_site, record.plan
    if pause is None:
        return (
            site is None
            and plan.compatibility.classification == "compatible"
            and plan.website_settings is None
        )
    if site is None:
        return False
    try:
        verify_snapshot(site)
    except ValueError:
        return False
    return (
        pause.pause_id == record.record_id
        and pause.binding == plan.old.binding == site.plan.binding
        and site.plan.writers == plan.writers
        and pause.content_digest == site.content_digest
        and pause.served_mode == "prepared-public"
        and pause.drained_at is not None
        and len(pause.writers) == len(plan.writers)
        and {item.writer for item in pause.writers} == set(plan.writers)
        and all(
            item.pause_id == pause.pause_id
            and item.binding == pause.binding
            and item.fenced
            and item.active_jobs == 0
            and item.evidence_id
            for item in pause.writers
        )
        and (
            pause.state == "drained"
            and pause.ended_at is None
            or record.step == "aborting"
            and pause.state in {"resuming", "complete"}
            and pause.recovery_binding == plan.old.binding
        )
    )


def abort_reconciliation_eligible(record: OdooCutoverRecord) -> bool:
    return backup_recovery_eligible(record) or (
        record.step == "aborted"
        and record.ended_at is not None
        and backup_recovery_eligible(record.model_copy(update={"step": "aborting"}))
    )


def startup_recovery_pause(record: OdooCutoverRecord) -> PublicPauseRecord | None:
    from control_plane.dokploy.slot_maintenance import maintenance_payload, payload_digest

    if (
        record.step not in {"maintaining", "rolling_back"}
        or record.maintenance is None
        or record.public_site is None
        or record.backup is None
        or not record.website_settings_attempted
    ):
        return None
    plan = record.plan
    intent = (
        plan.rollback_website_settings if record.step == "rolling_back" else plan.website_settings
    )
    pause = record.rollback_pause if record.step == "rolling_back" else record.pause
    receipt = record.maintenance
    if (
        intent is None
        or pause is None
        or pause.state != "drained"
        or pause.ended_at is not None
        or pause.served_mode != "prepared-public"
        or pause.binding != plan.old.binding
        or pause.content_digest != record.public_site.content_digest
        or pause.drained_at is None
        or len(pause.writers) != len(plan.writers)
        or {item.writer for item in pause.writers} != set(plan.writers)
        or any(
            item.binding != pause.binding
            or item.pause_id != pause.pause_id
            or not item.fenced
            or item.active_jobs != 0
            or not item.evidence_id
            for item in pause.writers
        )
    ):
        return None
    if (
        receipt.binding != plan.new.binding
        or receipt.image != plan.new.image
        or receipt.release_id != plan.release_id
        or receipt.candidate_manifest_sha256 != plan.compatibility.candidate_manifest_sha256
        or receipt.evidence_id != payload_digest(maintenance_payload(plan))
    ):
        return None
    try:
        verify_snapshot(record.public_site)
    except ValueError:
        return None
    return pause


def publication_recovery_pause(record: OdooCutoverRecord) -> PublicPauseRecord | None:
    if record.step in {"maintaining", "verifying", "switching", "resuming"}:
        return startup_recovery_pause(record.model_copy(update={"step": "maintaining"}))
    return startup_recovery_pause(record)
