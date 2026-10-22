"""Authentic record-only preview retirement evidence; never a provider receipt."""

from typing import Protocol

from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    parse_launchplane_mutation_timestamp,
)
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_retirement import canonical_sha256
from control_plane.workflows.launchplane import apply_preview_destroyed_transition


LEGACY_PREVIEW_RECONCILIATION_ROUTE = "/v1/previews/legacy-generic-web/reconciliation"


class LegacyPreviewRetirementConflict(ValueError):
    """A stale writer cannot replace an independently completed preview retirement."""


class ReconciliationReceiptStore(Protocol):
    def read_idempotency_record_by_id(
        self, record_id: str
    ) -> LaunchplaneIdempotencyRecord | None: ...


def reconciled_preview(
    preview: PreviewRecord, *, destroyed_at: str, reason: str, receipt_id: str = ""
) -> PreviewRecord:
    return apply_preview_destroyed_transition(
        preview=preview, destroyed_at=destroyed_at, destroy_reason=reason
    ).model_copy(update={"reconciliation_receipt_id": receipt_id})


def reconciliation_completion_time(
    store: ReconciliationReceiptStore, preview: PreviewRecord, *, product: str = ""
) -> str:
    """Return an ordering barrier only for this exact atomically reconciled record."""
    if preview.state != "destroyed" or not preview.reconciliation_receipt_id:
        return ""
    receipt = store.read_idempotency_record_by_id(preview.reconciliation_receipt_id)
    return reconciliation_receipt_time(receipt, preview, product=product)


def reconciliation_receipt_time(
    receipt: LaunchplaneIdempotencyRecord | None,
    preview: PreviewRecord,
    *,
    product: str = "",
    allow_unpointed: bool = False,
) -> str:
    if receipt is None:
        return ""
    result = receipt.response_payload.get("result")
    records = receipt.response_payload.get("records")
    if (
        preview.state != "destroyed"
        or (not preview.reconciliation_receipt_id and not allow_unpointed)
        or (
            bool(preview.reconciliation_receipt_id)
            and receipt.record_id != preview.reconciliation_receipt_id
        )
        or receipt.state != "completed"
        or receipt.route_path != LEGACY_PREVIEW_RECONCILIATION_ROUTE
        or receipt.response_status_code != 202
        or receipt.response_payload.get("status") != "accepted"
        or receipt.response_payload.get("trace_id") != receipt.response_trace_id
        or not isinstance(result, dict)
        or not isinstance(records, dict)
        or records.get("preview_id") != preview.preview_id
        or not isinstance(result.get("product"), str)
        or not result["product"].strip()
        or (product and result["product"] != product)
        or result.get("mode") != "apply"
        or result.get("preview_id") != preview.preview_id
        or result.get("context") != preview.context
        or result.get("preview_state") != "destroyed"
        or result.get("destroyed_at") != preview.destroyed_at
        or result.get("provider_state") != "absent"
        or result.get("provider_absence_verified") is not True
        or result.get("provider_writes") is not False
        or result.get("terminal_preview_digest")
        != canonical_sha256(preview.model_dump(mode="json"))
    ):
        return ""
    return preview.destroyed_at


def requested_after_retirement(requested_at: str, barrier: str) -> bool:
    try:
        return parse_launchplane_mutation_timestamp(
            requested_at, field_name="requested_at"
        ) > parse_launchplane_mutation_timestamp(barrier, field_name="destroyed_at")
    except ValueError as error:
        raise LegacyPreviewRetirementConflict(
            "Preview operation has no usable retirement ordering evidence."
        ) from error
