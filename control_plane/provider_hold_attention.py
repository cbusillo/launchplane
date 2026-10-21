"""Closed service attention models; retained holds never imply recovery authority."""

from datetime import datetime, timedelta, timezone
import re
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from control_plane.provider_target_holds import ProviderTargetHold

PROVIDER_HOLD_ATTENTION_ROUTE = "/v1/operations/provider-hold-attention"
PROVIDER_HOLD_PAGE_LIMIT = 100


class ProviderHoldScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product: str
    context: str
    instance: str


class ProviderHoldAttentionItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_reference: str | None
    original_scope: ProviderHoldScope | None
    attribution: Literal["retained", "legacy", "unavailable", "corrupt"]
    state: Literal["running", "reconcile_required"]
    phase: Literal["not_recorded", "effect_recorded"]
    created_at: str
    updated_at: str
    lease_expires_at: str
    effect_started_at: str


def public_provider_hold(hold: ProviderTargetHold) -> ProviderHoldAttentionItem:
    row = hold.reservation
    return ProviderHoldAttentionItem(
        operation_reference=hold.reference or None,
        original_scope=(
            ProviderHoldScope(
                product=hold.origin.product,
                context=hold.origin.context,
                instance=hold.origin.instance,
            )
            if hold.origin
            else None
        ),
        attribution=hold.attribution,
        state="running" if row.state == "running" else "reconcile_required",
        phase="effect_recorded" if row.provider_effect_phase else "not_recorded",
        created_at=row.created_at,
        updated_at=row.updated_at,
        lease_expires_at=row.lease_expires_at,
        effect_started_at=row.provider_effect_started_at,
    )


def provider_hold_cursor(record_id: str) -> str:
    return f"v1:{record_id}"


def parse_provider_hold_cursor(cursor: str | None) -> str:
    if cursor is None:
        return ""
    if re.fullmatch(r"v1:mutation-reservation-[0-9a-f]{64}", cursor) is None:
        raise ValueError("Invalid provider attention cursor.")
    return cursor[3:]


class ProviderHoldAttentionPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[ProviderHoldAttentionItem, ...]
    next_cursor: str | None
    has_more: bool
    # A continuation, including an empty final page, never proves a global absence.
    completeness: Literal["single_page", "partial"]
    observed_at: str
    stale_after: str


def provider_hold_page(
    holds: tuple[ProviderTargetHold, ...],
    *,
    limit: int,
    cursor: str | None,
) -> ProviderHoldAttentionPage:
    now = datetime.now(timezone.utc)
    has_more = len(holds) > limit
    selected = holds[:limit]
    return ProviderHoldAttentionPage(
        items=tuple(public_provider_hold(hold) for hold in selected),
        next_cursor=provider_hold_cursor(selected[-1].reservation.record_id) if has_more else None,
        has_more=has_more,
        completeness="partial" if cursor is not None or has_more else "single_page",
        observed_at=now.isoformat().replace("+00:00", "Z"),
        stale_after=(now + timedelta(seconds=60)).isoformat().replace("+00:00", "Z"),
    )


class ProviderHoldAttentionReadStore(Protocol):
    def read_provider_hold_attention_page(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> ProviderHoldAttentionPage: ...
