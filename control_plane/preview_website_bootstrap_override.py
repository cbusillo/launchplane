import hashlib
import json
from datetime import datetime, timezone
from typing import cast

from control_plane.contracts.odoo_instance_override_record import (
    OdooInstanceOverrideRecord,
    OdooOverrideApplyResult,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.odoo_instance_overrides import resolve_preview_website_bootstrap
from control_plane.odoo_post_deploy_http import OdooWebsiteBootstrapOverrideRequest
from control_plane.odoo_preview_apply_http import resolve_odoo_preview_apply_profile
from control_plane.odoo_preview_apply_execution import preview_execution_lock_name
from control_plane.disposable_lanes import resolve_disposable_preview
from control_plane.preview_reservation_recovery import (
    original_preview_intent,
    preview_reservation_reference,
    require_unrecorded_preview_original,
    require_current_preview_original_target,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import OdooInstanceOverrideConflictError


def preview_bootstrap_profile(
    store: object, request: OdooWebsiteBootstrapOverrideRequest
) -> tuple[LaunchplaneProductProfileRecord, str]:
    if not isinstance(store, PostgresRecordStore):
        raise ValueError("Preview bootstrap preparation requires database storage.")
    profile = resolve_odoo_preview_apply_profile(record_store=store, product=request.product)
    reservation = store.read_idempotency_record_by_id(request.preview_original_reservation_id)
    if reservation is None:
        raise ValueError("Original preview reservation is unavailable.")
    original = original_preview_intent(reservation, profile=profile, store=store)
    if (request.context, request.instance) != (
        original.issued_plan.context,
        original.issued_plan.preview_slug,
    ):
        raise ValueError("Preview bootstrap target differs from the retained original.")
    require_current_preview_original_target(
        profile=profile, reservation=reservation, original=original, instance=request.instance
    )
    if any(
        (lane.context, lane.instance) == (request.context, request.instance)
        for product in store.list_product_profile_records()
        for lane in product.lanes
    ):
        raise ValueError("Preview bootstrap target is a stable lane.")
    previews = store.list_preview_records(context_name=request.context)
    if any(
        profile.preview.slug_template.format(number=item.anchor_pr_number) == request.instance
        for item in previews
    ):
        resolve_disposable_preview(
            store, profile, request.context, request.instance, allow_destroyed=True
        )
    else:
        require_unrecorded_preview_original(
            store,
            profile=profile,
            number=original.issued_plan.plan_request.pr_number,
            reservation_id=reservation.record_id,
        )
    return profile, preview_reservation_reference(
        reservation, store.read_original_mutation_intent(reservation)
    )


def prepare_preview_bootstrap(
    store: object, request: OdooWebsiteBootstrapOverrideRequest
) -> dict[str, object]:
    profile, reference = preview_bootstrap_profile(store, request)
    store = cast(PostgresRecordStore, store)
    original = store.read_idempotency_record_by_id(request.preview_original_reservation_id)
    if original is None:
        raise ValueError("Original preview reservation is unavailable.")
    try:
        existing = store.read_odoo_instance_override_record(
            context_name=request.context, instance_name=request.instance
        )
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        existing.apply_on not in {("preview",), ("manual",)}
        or existing.config_parameters
        or existing.addon_settings
        or existing.website_settings is not None
        or (
            existing.apply_on == ("manual",)
            and (
                existing.website_bootstrap is not None
                or not existing.source_label.startswith("preview-reservation-")
            )
        )
    ):
        raise ValueError("Existing exact-instance intent is not preview-owned.")
    resolve_preview_website_bootstrap(
        store,
        context=request.context,
        template_instance=profile.preview.template_instance,
        preview_slug=request.instance,
    )
    desired = (
        request.website_bootstrap.model_dump(mode="json") if request.website_bootstrap else None
    )
    digest = hashlib.sha256(
        json.dumps(
            {
                "profile": profile.model_dump(mode="json"),
                "original": reference,
                "before": existing.model_dump(mode="json") if existing else None,
                "after": desired,
                "inherit_template": request.inherit_template,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    result = {
        "context": request.context,
        "instance": request.instance,
        "website_bootstrap": request.website_bootstrap is not None,
        "inherit_template": request.inherit_template,
        "plan_sha256": digest,
        "original_reference": reference,
        "original_settled": original.state == "completed",
        "mode": request.mode,
        "applied": False,
        "read_back_matches": False,
    }
    if request.mode == "dry-run":
        return result
    if request.reviewed_plan_sha256 != digest:
        raise OdooInstanceOverrideConflictError("Preview intent changed. Review a fresh plan.")
    record = OdooInstanceOverrideRecord(
        context=request.context,
        instance=request.instance,
        apply_on=("manual",) if request.inherit_template else ("preview",),
        website_bootstrap=request.website_bootstrap,
        last_apply=existing.last_apply.for_override_edit()
        if existing
        else OdooOverrideApplyResult(),
        updated_at=datetime.now(timezone.utc).isoformat(),
        source_label=reference,
    )
    with store.testing_replacement_execution_lock(
        preview_execution_lock_name(original.provider_target_key)
    ) as check:
        if check is None:
            raise ValueError("Preview executor is active.")
        check()
        if original.state != "completed" or store.list_held_provider_target_reservations(
            provider_target_key=original.provider_target_key
        ):
            raise ValueError("Settle original preview effects before changing bootstrap intent.")
        current_profile, current_reference = preview_bootstrap_profile(store, request)
        if current_profile != profile or current_reference != reference:
            raise OdooInstanceOverrideConflictError(
                "Original preview authority changed before commit."
            )
        store.write_odoo_instance_override_record(
            record,
            expected_record=existing,
            require_absent=existing is None,
            required_context_owner=(profile.product, request.context),
            expected_product_profile=profile,
        )
        check()
    readback = store.read_odoo_instance_override_record(
        context_name=request.context, instance_name=request.instance
    )
    if readback != record:
        raise ValueError("Preview bootstrap intent readback did not match.")
    return {**result, "applied": True, "read_back_matches": True}
