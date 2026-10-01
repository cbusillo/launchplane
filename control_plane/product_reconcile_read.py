"""Operator view of what the event reconciler last decided for a product's targets.

The saved plan and error carry GitHub, provider and build text Launchplane did not
write itself, so every string passes through the shared redactor before it leaves
the service. Exact commit SHAs and image digests stay readable: they are what an
operator compares against the build they expect. So do the ids Launchplane records
itself, the GitHub delivery id and the plan's top-level `*_id` fields, when they
have an id's shape; the shared redactor would otherwise take their hex for a token.
The same ids stay readable where the saved error names them. A failed testing deploy's
summary gets a longer limit: Launchplane writes it from a fixed description and
validated key names, and a plan blocker can name dozens of keys.
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
_MAX_FAILURE_SUMMARY_LENGTH = 1500
_FAILURE_SUMMARY_KEY = "last_failed_error_summary"
_UNBOUNDED_TEXT_LENGTH = 100_000
_MAX_DEPTH = 4
_MAX_ITEMS = 50
_IDENTIFIER_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|sha256:[0-9a-f]{64})$")
_RECORDED_ID_PATTERN = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|[a-z][A-Za-z0-9]*(?:[-.][A-Za-z0-9]+)*-[0-9a-f]{16,64})$"
)
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
    plan = plan if isinstance(plan, dict) else {}
    recorded_ids: list[str] = []
    for key, item in record.last_plan.items():
        if key in plan and key.endswith("_id") and isinstance(item, str) and _is_recorded_id(item):
            plan[key] = item
            recorded_ids.append(item)
    summary = record.last_plan.get(_FAILURE_SUMMARY_KEY)
    if isinstance(summary, str):
        plan[_FAILURE_SUMMARY_KEY] = _safe_text(summary, maximum_length=_MAX_FAILURE_SUMMARY_LENGTH)
    return ProductReconcileRequestView(
        target_key=record.target_key,
        target_kind=record.target_kind,
        pull_request_number=record.pull_request_number,
        state=record.state,
        requested_at=record.requested_at,
        updated_at=record.updated_at,
        request_count=record.request_count,
        attempt=record.attempt,
        last_delivery_id=(
            record.last_delivery_id
            if _is_recorded_id(record.last_delivery_id)
            else _safe_text(record.last_delivery_id)
        ),
        last_error=_safe_text_naming_ids(record.last_error, recorded_ids),
        last_plan=plan,
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


def _is_recorded_id(value: JsonValue) -> bool:
    return isinstance(value, str) and _RECORDED_ID_PATTERN.match(value) is not None


def _safe_text(value: str, *, maximum_length: int = _MAX_TEXT_LENGTH) -> str:
    text = value.strip()
    if not text or _IDENTIFIER_PATTERN.match(text):
        return text
    return redact_untrusted_text(text, fallback=_REDACTED, maximum_length=maximum_length)


def _safe_text_naming_ids(value: str, recorded_ids: list[str]) -> str:
    """Redact the text but keep the ids the plan already shows, which it often names."""
    placeholders = {f"recorded{index}": item for index, item in enumerate(recorded_ids)}
    text = value
    for placeholder, item in placeholders.items():
        text = text.replace(item, placeholder)
    # Bound after the ids are back, so they cannot push the text past the limit.
    text = _safe_text(text, maximum_length=_UNBOUNDED_TEXT_LENGTH)
    for placeholder, item in reversed(placeholders.items()):
        text = re.sub(rf"\b{placeholder}\b", item, text)
    if len(text) <= _MAX_TEXT_LENGTH:
        return text
    return text[: _MAX_TEXT_LENGTH - 3].rstrip() + "..."
