"""Operator view of what the event reconciler last decided for a product's targets.

The saved plan and error carry GitHub, provider and build text Launchplane did not
write itself, so every string passes through the shared redactor before it leaves
the service. Exact commit SHAs and image digests stay readable: they are what an
operator compares against the build they expect.
"""

import re
from typing import Protocol

from pydantic import BaseModel, ConfigDict, JsonValue

from control_plane.child_process_errors import redact_untrusted_text
from control_plane.contracts.product_reconcile import (
    ProductReconcileRequestRecord,
    ProductReconcileRequestState,
    ProductReconcileTargetKind,
)

_MAX_TEXT_LENGTH = 400
_MAX_DEPTH = 4
_MAX_ITEMS = 50
_IDENTIFIER_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|sha256:[0-9a-f]{64})$")
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_REDACTED = "[redacted]"


class ProductReconcileRequestReader(Protocol):
    def list_product_reconcile_requests(
        self, *, product: str
    ) -> tuple[ProductReconcileRequestRecord, ...]: ...


class ProductReconcileRequestView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_key: str
    target_kind: ProductReconcileTargetKind
    pull_request_number: int | None
    state: ProductReconcileRequestState
    requested_at: str
    updated_at: str
    request_count: int
    attempt: int
    last_delivery_id: str
    last_error: str
    last_plan: dict[str, JsonValue]


def product_reconcile_request_view(
    record: ProductReconcileRequestRecord,
) -> ProductReconcileRequestView:
    plan = _safe_value(record.last_plan, depth=0)
    return ProductReconcileRequestView(
        target_key=record.target_key,
        target_kind=record.target_kind,
        pull_request_number=record.pull_request_number,
        state=record.state,
        requested_at=record.requested_at,
        updated_at=record.updated_at,
        request_count=record.request_count,
        attempt=record.attempt,
        last_delivery_id=_safe_text(record.last_delivery_id),
        last_error=_safe_text(record.last_error),
        last_plan=plan if isinstance(plan, dict) else {},
    )


def _safe_value(value: JsonValue, *, depth: int) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _safe_text(value)
    if depth >= _MAX_DEPTH:
        return _REDACTED
    if isinstance(value, list):
        return [_safe_value(item, depth=depth + 1) for item in value[:_MAX_ITEMS]]
    return {
        (key if _KEY_PATTERN.match(key) else _REDACTED): _safe_value(item, depth=depth + 1)
        for key, item in list(value.items())[:_MAX_ITEMS]
    }


def _safe_text(value: str) -> str:
    text = value.strip()
    if not text or _IDENTIFIER_PATTERN.match(text):
        return text
    return redact_untrusted_text(text, fallback=_REDACTED, maximum_length=_MAX_TEXT_LENGTH)
