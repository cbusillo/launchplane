from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord


ProviderTargetBackfillStatus = Literal[
    "would-create",
    "created",
    "skipped-exists",
    "skipped-incomplete",
    "skipped-conflict",
    "unsupported-provider",
    "no_matching_routes",
]
ProviderTargetBackfillSeverity = Literal["ok", "warning", "blocked"]


class ProviderTargetBackfillStore(Protocol):
    def list_physical_provider_target_records(self) -> tuple[ProviderTargetRecord, ...]: ...

    def list_dokploy_target_records(self) -> tuple[DokployTargetRecord, ...]: ...

    def list_dokploy_target_id_records(self) -> tuple[DokployTargetIdRecord, ...]: ...

    def write_provider_target_record(self, record: ProviderTargetRecord) -> None: ...


ProviderTargetConditionalCreate = Callable[[ProviderTargetRecord], Literal["created", "exists"]]


class ProviderTargetBackfillItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    instance: str
    status: ProviderTargetBackfillStatus
    severity: ProviderTargetBackfillSeverity
    detail: str
    provider_id: str = "dokploy"
    physical_record: ProviderTargetRecord | None = None
    projected_record: ProviderTargetRecord | None = None

    @model_validator(mode="after")
    def _validate_item(self) -> "ProviderTargetBackfillItem":
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        self.detail = self.detail.strip()
        self.provider_id = self.provider_id.strip().lower()
        if not self.context:
            raise ValueError("provider-target backfill item requires context")
        if not self.instance:
            raise ValueError("provider-target backfill item requires instance")
        if not self.detail:
            raise ValueError("provider-target backfill item requires detail")
        if not self.provider_id:
            raise ValueError("provider-target backfill item requires provider_id")
        return self


class ProviderTargetBackfillResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied: bool = False
    items: tuple[ProviderTargetBackfillItem, ...] = ()
    counts: dict[str, int] = Field(default_factory=dict)
    inspected_route_count: int = 0
    blocked_count: int = 0
    warning_count: int = 0

    @property
    def ok(self) -> bool:
        return self.blocked_count == 0

    def to_report_payload(self) -> dict[str, object]:
        return {
            "status": "ok" if self.ok else "blocked",
            "applied": self.applied,
            "inspected_route_count": self.inspected_route_count,
            "blocked_count": self.blocked_count,
            "warning_count": self.warning_count,
            "counts": self.counts,
            "items": [item.model_dump(mode="json", exclude_none=True) for item in self.items],
        }


def backfill_provider_targets(
    record_store: ProviderTargetBackfillStore,
    *,
    apply: bool = False,
    provider_id: str = "",
    context_name: str = "",
    instance_name: str = "",
) -> ProviderTargetBackfillResult:
    normalized_provider_id = provider_id.strip().lower()
    normalized_context = context_name.strip()
    normalized_instance = instance_name.strip()
    physical_records = {
        _route_key(record.context, record.instance): record
        for record in record_store.list_physical_provider_target_records()
    }
    dokploy_target_records = {
        _route_key(record.context, record.instance): record
        for record in record_store.list_dokploy_target_records()
    }
    dokploy_target_id_records = {
        _route_key(record.context, record.instance): record
        for record in record_store.list_dokploy_target_id_records()
    }
    route_keys = sorted(
        physical_records.keys() | dokploy_target_records.keys() | dokploy_target_id_records.keys()
    )
    items: list[ProviderTargetBackfillItem] = []
    inspected_route_count = 0
    for context, instance in route_keys:
        if normalized_context and context != normalized_context:
            continue
        if normalized_instance and instance != normalized_instance:
            continue
        physical_record = physical_records.get((context, instance))
        target_record = dokploy_target_records.get((context, instance))
        target_id_record = dokploy_target_id_records.get((context, instance))
        if not _matches_provider_filter(
            normalized_provider_id=normalized_provider_id,
            physical_record=physical_record,
            target_record=target_record,
            target_id_record=target_id_record,
        ):
            continue
        inspected_route_count += 1
        item = _plan_backfill_route(
            context=context,
            instance=instance,
            physical_record=physical_record,
            target_record=target_record,
            target_id_record=target_id_record,
        )
        items.append(item)
    if inspected_route_count == 0 and (
        normalized_provider_id or normalized_context or normalized_instance
    ):
        items.append(
            _item(
                context=normalized_context or "*",
                instance=normalized_instance or "*",
                status="no_matching_routes",
                severity="blocked",
                detail="provider-target backfill filters did not match any stored route",
                provider_id=normalized_provider_id or "dokploy",
            )
        )
    if apply and not any(item.severity == "blocked" for item in items):
        conditional_create = getattr(record_store, "create_provider_target_record_if_absent", None)
        applied_items: list[ProviderTargetBackfillItem] = []
        for item in items:
            if item.status != "would-create":
                applied_items.append(item)
                continue
            record = item.projected_record
            if record is None:
                applied_items.append(item)
                continue
            try:
                create_status = _create_provider_target_record(
                    record_store=record_store,
                    conditional_create=conditional_create,
                    record=record,
                )
            except ValueError as error:
                applied_items.append(
                    _item(
                        context=record.context,
                        instance=record.instance,
                        status="skipped-conflict",
                        severity="blocked",
                        detail=str(error),
                        projected_record=record,
                    )
                )
                continue
            if create_status == "exists":
                applied_items.append(
                    _item(
                        context=record.context,
                        instance=record.instance,
                        status="skipped-conflict",
                        severity="blocked",
                        detail="provider-target row changed before backfill could create it",
                        projected_record=record,
                    )
                )
                continue
            applied_items.append(
                _item(
                    context=record.context,
                    instance=record.instance,
                    status="created",
                    severity="ok",
                    detail="created explicit provider-target row",
                    projected_record=record,
                )
            )
        items = applied_items
    counts = Counter(item.status for item in items)
    blocked_count = sum(1 for item in items if item.severity == "blocked")
    return ProviderTargetBackfillResult(
        applied=apply,
        items=tuple(items),
        counts=dict(sorted(counts.items())),
        inspected_route_count=inspected_route_count,
        blocked_count=blocked_count,
        warning_count=sum(1 for item in items if item.severity == "warning"),
    )


def _create_provider_target_record(
    *,
    record_store: ProviderTargetBackfillStore,
    conditional_create: object,
    record: ProviderTargetRecord,
) -> Literal["created", "exists"]:
    if conditional_create is not None:
        return cast(ProviderTargetConditionalCreate, conditional_create)(record)
    current_record = next(
        (
            item
            for item in record_store.list_physical_provider_target_records()
            if item.context == record.context and item.instance == record.instance
        ),
        None,
    )
    if current_record is not None:
        return "exists"
    record_store.write_provider_target_record(record)
    return "created"


def _plan_backfill_route(
    *,
    context: str,
    instance: str,
    physical_record: ProviderTargetRecord | None,
    target_record: DokployTargetRecord | None,
    target_id_record: DokployTargetIdRecord | None,
) -> ProviderTargetBackfillItem:
    if physical_record is not None and physical_record.provider_id != "dokploy":
        return _item(
            context=context,
            instance=instance,
            status="unsupported-provider",
            severity="warning",
            detail="explicit provider-target row is not managed by Dokploy backfill",
            provider_id=physical_record.provider_id,
            physical_record=physical_record,
        )
    if target_record is None or target_id_record is None:
        missing_parts = []
        if target_record is None:
            missing_parts.append("target")
        if target_id_record is None:
            missing_parts.append("target-id")
        severity: ProviderTargetBackfillSeverity = (
            "blocked" if physical_record is not None else "warning"
        )
        return _item(
            context=context,
            instance=instance,
            status="skipped-incomplete",
            severity=severity,
            detail="Dokploy route is incomplete; missing " + ", ".join(missing_parts),
            physical_record=physical_record,
        )
    projected_record = ProviderTargetRecord.from_dokploy_records(
        target_record=target_record,
        target_id_record=target_id_record,
    )
    if physical_record is None:
        return _item(
            context=context,
            instance=instance,
            status="would-create",
            severity="ok",
            detail="would create explicit provider-target row",
            projected_record=projected_record,
        )
    if _authority_payload(physical_record) == _authority_payload(projected_record):
        return _item(
            context=context,
            instance=instance,
            status="skipped-exists",
            severity="ok",
            detail="explicit provider-target row already matches the Dokploy projection",
            physical_record=physical_record,
            projected_record=projected_record,
        )
    return _item(
        context=context,
        instance=instance,
        status="skipped-conflict",
        severity="blocked",
        detail="existing provider-target row differs from the Dokploy projection",
        physical_record=physical_record,
        projected_record=projected_record,
    )


def _item(
    *,
    context: str,
    instance: str,
    status: ProviderTargetBackfillStatus,
    severity: ProviderTargetBackfillSeverity,
    detail: str,
    provider_id: str = "dokploy",
    physical_record: ProviderTargetRecord | None = None,
    projected_record: ProviderTargetRecord | None = None,
) -> ProviderTargetBackfillItem:
    return ProviderTargetBackfillItem(
        context=context,
        instance=instance,
        status=status,
        severity=severity,
        detail=detail,
        provider_id=provider_id,
        physical_record=physical_record,
        projected_record=projected_record,
    )


def _matches_provider_filter(
    *,
    normalized_provider_id: str,
    physical_record: ProviderTargetRecord | None,
    target_record: DokployTargetRecord | None,
    target_id_record: DokployTargetIdRecord | None,
) -> bool:
    if not normalized_provider_id:
        return True
    if physical_record is not None and physical_record.provider_id == normalized_provider_id:
        return True
    if normalized_provider_id == "dokploy" and (
        target_record is not None or target_id_record is not None
    ):
        return True
    return False


def _authority_payload(record: ProviderTargetRecord) -> dict[str, object]:
    return {
        "provider_id": record.provider_id,
        "target_category": record.target_category,
        "target_id": record.target_id,
        "display_name": record.display_name,
        "provider_target_type": record.provider_target_type,
        "provider_evidence": dict(record.provider_evidence),
    }


def _route_key(context: str, instance: str) -> tuple[str, str]:
    return (context.strip(), instance.strip())
