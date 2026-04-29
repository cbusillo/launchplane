from control_plane.contracts.preview_inventory_scan_record import PreviewInventoryScanRecord
from control_plane.contracts.preview_lifecycle_plan_record import (
    PreviewLifecycleDesiredPreview,
    PreviewLifecyclePlanRecord,
    build_preview_lifecycle_plan_id,
)


def build_preview_lifecycle_plan(
    *,
    product: str,
    context: str,
    planned_at: str,
    source: str,
    desired_previews: tuple[PreviewLifecycleDesiredPreview, ...],
    latest_inventory_scan: PreviewInventoryScanRecord | None,
    desired_state_id: str = "",
) -> PreviewLifecyclePlanRecord:
    desired_by_slug = {
        preview.preview_slug.strip(): preview.model_copy(
            update={"preview_slug": preview.preview_slug.strip()}
        )
        for preview in desired_previews
    }
    desired_slugs = tuple(sorted(desired_by_slug))
    normalized_desired_previews = tuple(desired_by_slug[slug] for slug in desired_slugs)
    plan_id = build_preview_lifecycle_plan_id(context_name=context, planned_at=planned_at)
    if latest_inventory_scan is None:
        return PreviewLifecyclePlanRecord(
            plan_id=plan_id,
            product=product,
            context=context,
            planned_at=planned_at,
            source=source,
            status="missing_inventory",
            desired_state_id=desired_state_id,
            desired_previews=normalized_desired_previews,
            desired_slugs=desired_slugs,
            error_message="Launchplane has not recorded a preview inventory scan for this context.",
        )

    actual_slugs = tuple(sorted(set(latest_inventory_scan.preview_slugs)))
    desired_set = set(desired_slugs)
    actual_set = set(actual_slugs)
    return PreviewLifecyclePlanRecord(
        plan_id=plan_id,
        product=product,
        context=context,
        planned_at=planned_at,
        source=source,
        status="pass" if latest_inventory_scan.status == "pass" else "fail",
        desired_state_id=desired_state_id,
        inventory_scan_id=latest_inventory_scan.scan_id,
        desired_previews=normalized_desired_previews,
        desired_slugs=desired_slugs,
        actual_slugs=actual_slugs,
        keep_slugs=tuple(sorted(desired_set & actual_set)),
        orphaned_slugs=tuple(sorted(actual_set - desired_set)),
        missing_slugs=tuple(sorted(desired_set - actual_set)),
        error_message=latest_inventory_scan.error_message,
    )
