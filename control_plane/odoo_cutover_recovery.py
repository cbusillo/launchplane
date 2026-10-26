from typing import Protocol
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.odoo_prod_rollback_operation import (
    OdooProdRollbackRequest,
    OdooProdRollbackOperationRecord,
)
from control_plane.contracts.odoo_cutover import OdooCutoverRecord, ODOO_CUTOVER_TERMINAL_STEPS
from control_plane.contracts.prepared_public_site import PublicPauseRecord
from control_plane.prepared_public_site import verify_snapshot
from control_plane.odoo_cutover_storage import DeploymentStore
from control_plane.contracts.odoo_prod_promotion_operation import OdooProdPromotionOperationRecord
from control_plane.odoo_cutover_plan import cutover_identity


class CutoverEntryRefusedError(RuntimeError):
    code = "odoo_cutover_entry_refused"


class BackupVerificationRejected(ValueError):
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


def maintenance_reconciliation_eligible(record: OdooCutoverRecord) -> bool:
    return (
        record.step == "maintaining"
        and record.backup is not None
        and record.backup.binding == record.plan.old.binding
        and record.backup.image == record.plan.old.image
        and backup_recovery_eligible(
            record.model_copy(
                update={
                    "step": "backing_up",
                    "backup": None,
                    "maintenance": None,
                    "website_settings_attempted": False,
                }
            )
        )
    )


def publication_recovery_pause(record: OdooCutoverRecord) -> PublicPauseRecord | None:
    if record.step in {"maintaining", "ready", "switching", "resuming"}:
        return startup_recovery_pause(record.model_copy(update={"step": "maintaining"}))
    return startup_recovery_pause(record)


class RollbackCutoverStore(Protocol):
    def read_deployment_record(self, record_id: str) -> DeploymentRecord: ...
    def list_deployment_records(
        self, *, context_name: str = "", instance_name: str = "", limit: int | None = None
    ) -> tuple[DeploymentRecord, ...]: ...
    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory: ...


def rollback_cutover_deployment(
    store: RollbackCutoverStore,
    request: OdooProdRollbackRequest,
    *,
    include_terminal_rollback: bool = False,
) -> DeploymentRecord | None:
    attempts = tuple(
        record
        for record in store.list_deployment_records(
            context_name=request.context, instance_name=request.instance
        )
        if record.odoo_cutover is not None
        and record.odoo_cutover.step not in ODOO_CUTOVER_TERMINAL_STEPS
    )
    if len(attempts) > 1:
        raise ValueError("Multiple unfinished cutovers require lane reconciliation.")
    if attempts:
        return attempts[0]
    try:
        inventory = store.read_environment_inventory(
            context_name=request.context, instance_name=request.instance
        )
        current = store.read_deployment_record(inventory.deployment_record_id)
    except FileNotFoundError:
        return None
    if current.odoo_cutover is not None:
        return current
    if not include_terminal_rollback:
        return None
    original_id = current.runtime_source.get("cutover_deployment_record_id", "")
    if not original_id:
        return None
    original = store.read_deployment_record(original_id)
    progress = original.odoo_cutover
    if (
        progress is None
        or progress.step != "rolled_back"
        or current.artifact_identity is None
        or current.artifact_identity.artifact_id != progress.plan.production.artifact_id
        or current.runtime_source.get("retained_deployment_record_id")
        != progress.plan.old.runtime_identity.deployment_record_id
    ):
        raise ValueError("rollback completion lost its original cutover provenance")
    return original


def rollback_operation_cutover(
    store: RollbackCutoverStore, operation: OdooProdRollbackOperationRecord
) -> OdooCutoverRecord | None:
    deployment = rollback_cutover_deployment(
        store, operation.request, include_terminal_rollback=True
    )
    record = deployment.odoo_cutover if deployment is not None else None
    if record is None:
        return None
    plan = record.plan
    return (
        record
        if (
            plan.new.binding.product_id,
            plan.new.runtime_identity.context,
            plan.new.binding.environment_id,
            plan.production.artifact_id,
        )
        == (operation.product, operation.context, operation.instance, operation.target.artifact_id)
        else None
    )


def cutover_reconciliation_eligible(record: OdooCutoverRecord) -> bool:
    if abort_reconciliation_eligible(record) or record.step in {
        "preparing",
        "prepared",
        "pausing",
        "ready",
        "switching",
        "resuming",
        "draining",
        "complete",
        "rolled_back",
    }:
        return True
    if maintenance_reconciliation_eligible(record):
        return record.maintenance is not None or record.error in {
            "",
            RuntimeError.__name__,
            TimeoutError.__name__,
        }
    return record.step == "rolling_back" and (
        startup_recovery_pause(record) is not None
        or record.plan.compatibility.classification == "compatible"
        and not record.website_settings_attempted
        and record.rollback_pause is None
        and record.maintenance is not None
        and record.backup is not None
    )


def cutover_reconciliation_uses_budget(record: OdooCutoverRecord) -> bool:
    return record.step in {
        "preparing",
        "prepared",
        "pausing",
        "ready",
        "switching",
        "resuming",
        "draining",
    } or (record.step == "maintaining" and record.maintenance is not None)
