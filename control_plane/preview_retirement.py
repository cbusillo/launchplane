from datetime import datetime

from control_plane.contracts.idempotency_record import (
    format_launchplane_mutation_timestamp,
    parse_launchplane_mutation_timestamp,
)
from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_reconcile import product_reconcile_target_key
from control_plane.contracts.preview_execution import odoo_preview_destroy_reason


def verified_preview_retirement_digest(
    store: object, *, profile: LaunchplaneProductProfileRecord, number: int
) -> str:
    from control_plane.preview_reservation_recovery import original_preview_intent
    from control_plane.storage.postgres import PostgresRecordStore

    if not isinstance(store, PostgresRecordStore) or profile.driver_id != "odoo":
        return ""
    previews = tuple(
        preview
        for preview in store.list_preview_records(
            context_name=profile.preview.context, anchor_pr_number=number
        )
        if preview.anchor_repo.casefold()
        in {profile.repository.casefold(), profile.repository.rsplit("/", 1)[-1].casefold()}
    )
    if len(previews) != 1:
        return ""
    preview = previews[0]
    if (
        preview.state != "destroyed"
        or preview.active_generation_id
        or preview.serving_generation_id
        or preview.reconciliation_receipt_id
        or not preview.destroyed_at
    ):
        return ""
    originals = store.list_odoo_preview_originals(product=profile.product, number=number)
    for reservation in originals:
        if reservation.state != "completed" or reservation.response_status_code != 202:
            continue
        try:
            original = original_preview_intent(reservation, profile=profile, store=store)
        except ValueError:
            continue
        issued = original.issued_plan
        provenance = issued.plan_provenance
        if (
            issued.operation != "destroy"
            or provenance is None
            or issued.plan_request.pr_number != number
        ):
            continue
        records = reservation.response_payload.get("records", {})
        result = reservation.response_payload.get("result", {})
        if (
            not isinstance(records, dict)
            or not isinstance(result, dict)
            or records.get("preview_id") != preview.preview_id
            or records.get("transition") != "destroyed"
            or records.get("lifecycle_evidence_status") not in {"applied", "replayed"}
            or result.get("status") != "pass"
            or result.get("operation") != "destroy"
            or result.get("product") != profile.product
            or result.get("repository") != profile.repository
            or result.get("compose_name") != issued.dry_run_plan.compose_name
            or result.get("preview_url") != preview.canonical_url
            or preview.destroy_reason
            != odoo_preview_destroy_reason(
                plan_id=provenance.plan_id, issued_at=preview.destroyed_at
            )
            or preview.destroyed_at != format_launchplane_mutation_timestamp(provenance.issued_at)
        ):
            continue
        if store.list_held_provider_target_reservations(
            provider_target_key=reservation.provider_target_key
        ) or any(
            other.record_id != reservation.record_id
            and other.provider_target_key == reservation.provider_target_key
            and _timestamp(other.created_at) >= _timestamp(preview.destroyed_at)
            for other in originals
        ):
            return ""
        try:
            request = store.read_product_reconcile_request(
                product_reconcile_target_key(
                    product=profile.product, target_kind="preview", pull_request_number=number
                )
            )
        except FileNotFoundError:
            request = None
        if (
            request is not None
            and _timestamp(request.updated_at) >= _timestamp(preview.destroyed_at)
            and (request.last_error or request.last_plan.get("held") is True)
        ):
            return ""
        if any(
            feedback.product == profile.product
            and feedback.anchor_pr_number == number
            and feedback.repository.casefold() == profile.repository.casefold()
            and feedback.status not in {"destroyed", "cleared"}
            and _timestamp(feedback.requested_at) >= _timestamp(preview.destroyed_at)
            for feedback in store.list_preview_pr_feedback_records(
                context_name=profile.preview.context
            )
        ):
            return ""
        return nonvalidating_compact_sorted_json_sha256(
            {
                "preview": preview.model_dump(mode="json"),
                "reservation": reservation.model_dump(mode="json"),
                "original": original.issued_plan.model_dump(mode="json"),
            }
        )
    return ""


def _timestamp(value: str) -> datetime:
    return parse_launchplane_mutation_timestamp(value, field_name="preview evidence timestamp")
