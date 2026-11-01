"""Stable identities shared by Odoo preview execution and its record reconciliation."""

import hashlib

from control_plane.contracts.preview_record import PreviewRecord


def odoo_preview_reconciliation_key(*, context: str, compose_name: str) -> str:
    if not context.strip() or not compose_name.strip():
        raise ValueError("Preview execution requires context and compose name.")
    return f"dokploy:compose:{context.strip()}:{compose_name.strip()}"


def odoo_preview_target_key(*, context: str, compose_name: str) -> str:
    key = odoo_preview_reconciliation_key(context=context, compose_name=compose_name)
    return "dokploy-provider-target:" + hashlib.sha256(key.encode()).hexdigest()


def preview_execution_lock_name(target_key: str) -> str:
    return f"preview:{target_key}"


ODOO_PREVIEW_DESTROY_REASON_PREFIX = "odoo_preview_destroy"


def odoo_preview_operation_token(plan_id: str) -> str:
    normalized_plan_id = plan_id.strip()
    if not normalized_plan_id:
        raise ValueError("Odoo preview lifecycle evidence requires plan_id.")
    return hashlib.sha256(normalized_plan_id.encode("utf-8")).hexdigest()[:20]


def odoo_preview_destroy_reason(*, plan_id: str, issued_at: str) -> str:
    return (
        f"{ODOO_PREVIEW_DESTROY_REASON_PREFIX}:{odoo_preview_operation_token(plan_id)}:{issued_at}"
    )


def odoo_preview_destroy_issued_at(reason: str) -> str:
    prefix = ODOO_PREVIEW_DESTROY_REASON_PREFIX + ":"
    return reason[len(prefix) :].partition(":")[2] if reason.startswith(prefix) else ""


def matches_odoo_preview_service_destroy(preview: PreviewRecord, expected_reason: str) -> bool:
    return (
        preview.state == "destroyed"
        and not preview.reconciliation_receipt_id
        and preview.destroy_reason == expected_reason
    )
