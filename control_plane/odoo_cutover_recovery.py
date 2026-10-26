from control_plane.contracts.odoo_cutover import OdooCutoverRecord
from control_plane.contracts.prepared_public_site import PublicPauseRecord
from control_plane.prepared_public_site import verify_snapshot


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
