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
from control_plane.preview_reservation_recovery import (
    original_preview_intent,
    preview_reservation_reference,
    require_unrecorded_preview_original,
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
    try:
        existing = store.read_odoo_instance_override_record(
            context_name=request.context, instance_name=request.instance
        )
    except FileNotFoundError:
        existing = None
    if existing is not None and existing.apply_on != ("preview",):
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
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    result = {
        "context": request.context,
        "instance": request.instance,
        "website_bootstrap": request.website_bootstrap is not None,
        "plan_sha256": digest,
        "original_reference": reference,
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
        apply_on=("preview",),
        website_bootstrap=request.website_bootstrap,
        last_apply=existing.last_apply.for_override_edit()
        if existing
        else OdooOverrideApplyResult(),
        updated_at=datetime.now(timezone.utc).isoformat(),
        source_label=reference,
    )
    store.write_odoo_instance_override_record(
        record,
        expected_record=existing,
        require_absent=existing is None,
        required_context_owner=(profile.product, request.context),
        expected_product_profile=profile,
    )
    readback = store.read_odoo_instance_override_record(
        context_name=request.context, instance_name=request.instance
    )
    if readback != record:
        raise ValueError("Preview bootstrap intent readback did not match.")
    return {**result, "applied": True, "read_back_matches": True}
