from control_plane.contracts.odoo_cutover import OdooCutoverRecord
from control_plane.contracts.prepared_public_site import PublicPauseRecord
from control_plane.prepared_public_site import verify_snapshot


class DefiniteBackupFailure(ValueError):
    pass


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
            and record.backup_failed
            and pause.state in {"resuming", "complete"}
            and pause.recovery_binding == plan.old.binding
        )
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
