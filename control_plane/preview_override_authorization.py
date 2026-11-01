from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord


def director_preview_snapshot_allowed(
    existing: LaunchplaneProductProfileRecord | None,
    replacement: LaunchplaneProductProfileRecord,
) -> bool:
    if (
        existing is None
        or not existing.is_active
        or existing.driver_id != "odoo"
        or not existing.preview.enabled
        or existing.preview.clean_testing_snapshot
        or not replacement.preview.clean_testing_snapshot
    ):
        return False
    payload = existing.model_dump(mode="json")
    payload["preview"]["clean_testing_snapshot"] = True
    try:
        expected = LaunchplaneProductProfileRecord.model_validate(payload)
    except ValueError:
        return False
    return replacement == expected
