from __future__ import annotations

from pathlib import Path
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.preview_lifecycle_cleanup_record import (
    PreviewLifecycleCleanupRecord,
)
from control_plane.contracts.preview_inventory_scan_record import PreviewInventoryScanRecord
from control_plane.contracts.preview_inventory_scan_record import build_preview_inventory_scan_id
from control_plane.contracts.preview_lifecycle_plan_record import PreviewLifecyclePlanRecord
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.drivers.registry import read_driver_descriptor
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewDesiredStateRequest,
    GenericWebPreviewInventoryRequest,
    GenericWebPreviewProfileStore,
    discover_generic_web_preview_desired_state,
    execute_generic_web_preview_inventory,
)
from control_plane.workflows.preview_lifecycle import build_preview_lifecycle_plan
from control_plane.workflows.preview_lifecycle_cleanup import (
    build_preview_lifecycle_cleanup_record,
)
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.workflows.verireel_preview_driver import (
    VeriReelPreviewInventoryRequest,
    execute_verireel_preview_inventory,
)


class PreviewLifecycleCleanupEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    plan_id: str
    source: str = "workflow"
    apply: bool = False
    destroy_reason: str = "preview_lifecycle_cleanup"
    timeout_seconds: int = Field(default=300, ge=1)

    @model_validator(mode="after")
    def _validate_request(self) -> "PreviewLifecycleCleanupEnvelope":
        if not self.product.strip():
            raise ValueError("preview lifecycle cleanup requires product")
        if not self.context.strip():
            raise ValueError("preview lifecycle cleanup requires context")
        if not self.plan_id.strip():
            raise ValueError("preview lifecycle cleanup requires plan_id")
        if not self.source.strip():
            raise ValueError("preview lifecycle cleanup requires source")
        if not self.destroy_reason.strip():
            raise ValueError("preview lifecycle cleanup requires destroy_reason")
        return self


class PreviewLifecycleSweepEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    source: str = "launchplane-preview-lifecycle"
    product: str = ""
    apply: bool = False
    destroy_reason: str = "launchplane_preview_lifecycle_cleanup"
    timeout_seconds: int = Field(default=300, ge=1)
    max_pages: int = Field(default=10, ge=1, le=20)

    @model_validator(mode="after")
    def _validate_request(self) -> "PreviewLifecycleSweepEnvelope":
        if not self.source.strip():
            raise ValueError("preview lifecycle sweep requires source")
        if not self.destroy_reason.strip():
            raise ValueError("preview lifecycle sweep requires destroy_reason")
        return self


class PreviewLifecycleCleanupApplyStore(Protocol):
    def list_preview_lifecycle_plan_records(
        self, *, context_name: str = "", limit: int | None = 25
    ) -> tuple[PreviewLifecyclePlanRecord, ...]: ...

    def write_preview_lifecycle_cleanup_record(
        self, record: PreviewLifecycleCleanupRecord
    ) -> object: ...


class PreviewLifecycleCleanupMutationStore(PreviewLifecycleCleanupApplyStore, Protocol):
    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]: ...

    def write_preview_record(self, record: PreviewRecord) -> object: ...


class PreviewLifecycleSweepStore(Protocol):
    def list_product_profile_records(
        self,
    ) -> tuple[LaunchplaneProductProfileRecord, ...]: ...

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def list_preview_inventory_scan_records(
        self, *, context_name: str = "", limit: int | None = 25
    ) -> tuple[PreviewInventoryScanRecord, ...]: ...

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]: ...

    def write_preview_record(self, record: PreviewRecord) -> object: ...

    def write_preview_inventory_scan_record(self, record: PreviewInventoryScanRecord) -> object: ...

    def write_preview_desired_state_record(self, record: PreviewDesiredStateRecord) -> object: ...

    def write_preview_lifecycle_plan_record(self, record: PreviewLifecyclePlanRecord) -> object: ...

    def write_preview_lifecycle_cleanup_record(
        self, record: PreviewLifecycleCleanupRecord
    ) -> object: ...


def require_preview_lifecycle_cleanup_apply_store(
    record_store: object,
) -> PreviewLifecycleCleanupApplyStore:
    required_methods = (
        "list_preview_lifecycle_plan_records",
        "write_preview_lifecycle_cleanup_record",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support preview lifecycle cleanup applies: "
            f"{missing_summary}"
        )
    return cast(PreviewLifecycleCleanupApplyStore, record_store)


def require_preview_lifecycle_cleanup_mutation_store(
    record_store: object,
) -> PreviewLifecycleCleanupMutationStore:
    required_methods = (
        "list_preview_lifecycle_plan_records",
        "list_preview_records",
        "write_preview_record",
        "write_preview_lifecycle_cleanup_record",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support preview lifecycle cleanup mutations: "
            f"{missing_summary}"
        )
    return cast(PreviewLifecycleCleanupMutationStore, record_store)


def require_preview_lifecycle_sweep_store(
    record_store: object,
) -> PreviewLifecycleSweepStore:
    required_methods = (
        "list_product_profile_records",
        "read_product_profile_record",
        "list_preview_inventory_scan_records",
        "list_preview_records",
        "write_preview_record",
        "write_preview_inventory_scan_record",
        "write_preview_desired_state_record",
        "write_preview_lifecycle_plan_record",
        "write_preview_lifecycle_cleanup_record",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support preview lifecycle sweep applies: "
            f"{missing_summary}"
        )
    return cast(PreviewLifecycleSweepStore, record_store)


def latest_preview_lifecycle_plan(
    *, record_store: PreviewLifecycleCleanupApplyStore, context_name: str, plan_id: str
) -> PreviewLifecyclePlanRecord | None:
    records = record_store.list_preview_lifecycle_plan_records(
        context_name=context_name,
        limit=None,
    )
    return next((record for record in records if record.plan_id == plan_id), None)


def write_preview_lifecycle_cleanup_apply_record(
    *, record_store: PreviewLifecycleCleanupApplyStore, record: PreviewLifecycleCleanupRecord
) -> str:
    record_store.write_preview_lifecycle_cleanup_record(record)
    return record.cleanup_id


def preview_lifecycle_cleanup_driver_id(
    profile: LaunchplaneProductProfileRecord,
) -> str:
    if profile.driver_id == "verireel":
        return "verireel"
    if _product_driver_compatible(profile=profile, expected_driver_id="generic-web"):
        return "generic-web"
    return ""


def preview_lifecycle_cleanup_profile_settings(
    *, record_store: object, product: str
) -> tuple[str, str]:
    cleanup_driver_id = "verireel" if product == "verireel" else ""
    cleanup_slug_template = "pr-{number}"
    read_profile = getattr(record_store, "read_product_profile_record", None)
    if not callable(read_profile):
        return cleanup_driver_id, cleanup_slug_template
    try:
        profile = LaunchplaneProductProfileRecord.model_validate(read_profile(product))
    except FileNotFoundError:
        return cleanup_driver_id, cleanup_slug_template
    return preview_lifecycle_cleanup_driver_id(profile), profile.preview.slug_template


def preview_lifecycle_sweep_profiles(
    *, record_store: object, product: str = ""
) -> tuple[LaunchplaneProductProfileRecord, ...]:
    profiles_store = require_preview_lifecycle_sweep_store(record_store)
    requested_product = product.strip()
    profiles: list[LaunchplaneProductProfileRecord] = []
    for record in profiles_store.list_product_profile_records():
        if not record.is_active:
            continue
        profile = LaunchplaneProductProfileRecord.model_validate(record)
        if requested_product and profile.product != requested_product:
            continue
        if not profile.preview.enabled:
            continue
        if not profile.preview.context.strip():
            continue
        profiles.append(profile)
    return tuple(sorted(profiles, key=lambda profile: profile.product))


def build_preview_lifecycle_sweep(
    *,
    control_plane_root: Path,
    record_store: PreviewLifecycleSweepStore,
    request: PreviewLifecycleSweepEnvelope,
) -> dict[str, object]:
    profiles = preview_lifecycle_sweep_profiles(
        record_store=record_store,
        product=request.product,
    )
    entries: list[dict[str, object]] = []
    for profile in profiles:
        cleanup_driver_id = preview_lifecycle_cleanup_driver_id(profile)
        entry: dict[str, object] = {
            "product": profile.product,
            "context": profile.preview.context,
            "driver_id": profile.driver_id,
            "cleanup_driver_id": cleanup_driver_id,
        }
        if not cleanup_driver_id:
            entry.update(
                {
                    "status": "skipped",
                    "error_message": (
                        "Preview lifecycle cleanup is not implemented for this product driver."
                    ),
                }
            )
            entries.append(entry)
            continue
        if cleanup_driver_id == "verireel":
            verireel_inventory_result = execute_verireel_preview_inventory(
                control_plane_root=control_plane_root,
                request=VeriReelPreviewInventoryRequest(context=profile.preview.context),
            )
            inventory_context = verireel_inventory_result.context
            inventory_source = request.source
            inventory_slugs = tuple(item.previewSlug for item in verireel_inventory_result.previews)
        else:
            generic_web_inventory_result = execute_generic_web_preview_inventory(
                control_plane_root=control_plane_root,
                record_store=cast(GenericWebPreviewProfileStore, record_store),
                request=GenericWebPreviewInventoryRequest(
                    product=profile.product,
                    source=request.source,
                ),
                profile=profile,
            )
            inventory_context = generic_web_inventory_result.context
            inventory_source = generic_web_inventory_result.source
            inventory_slugs = tuple(
                item.previewSlug for item in generic_web_inventory_result.previews
            )
        inventory_scan_id = write_preview_inventory_scan_record(
            record_store=record_store,
            context=inventory_context,
            source=inventory_source,
            preview_slugs=inventory_slugs,
        )
        desired_state = discover_generic_web_preview_desired_state(
            control_plane_root=control_plane_root,
            record_store=cast(GenericWebPreviewProfileStore, record_store),
            request=GenericWebPreviewDesiredStateRequest(
                product=profile.product,
                source=request.source,
                label=profile.preview.enable_label,
                max_pages=request.max_pages,
            ),
            discovered_at=utc_now_timestamp(),
            profile=profile,
        )
        desired_state_id = write_preview_desired_state_record(
            record_store=record_store,
            record=desired_state,
        )
        lifecycle_plan = build_preview_lifecycle_plan(
            product=profile.product,
            context=profile.preview.context,
            planned_at=utc_now_timestamp(),
            source=request.source,
            desired_previews=desired_state.desired_previews,
            desired_state_id=desired_state_id,
            latest_inventory_scan=_latest_preview_inventory_scan(
                record_store=record_store,
                context_name=profile.preview.context,
            ),
        )
        lifecycle_plan_id = write_preview_lifecycle_plan_record(
            record_store=record_store,
            record=lifecycle_plan,
        )
        cleanup_record = build_preview_lifecycle_cleanup_record(
            plan=lifecycle_plan,
            requested_at=utc_now_timestamp(),
            source=request.source,
            apply=request.apply,
            destroy_reason=request.destroy_reason,
            control_plane_root=control_plane_root,
            record_store=record_store,
            timeout_seconds=request.timeout_seconds,
            driver_id=cleanup_driver_id,
            preview_slug_template=profile.preview.slug_template,
        )
        cleanup_id = write_preview_lifecycle_sweep_cleanup_record(
            record_store=record_store,
            record=cleanup_record,
        )
        entry.update(
            {
                "status": cleanup_record.status,
                "preview_inventory_scan_id": inventory_scan_id,
                "preview_desired_state_id": desired_state_id,
                "preview_lifecycle_plan_id": lifecycle_plan_id,
                "preview_lifecycle_cleanup_id": cleanup_id,
                "desired_count": desired_state.desired_count,
                "actual_count": len(lifecycle_plan.actual_slugs),
                "orphaned_slugs": lifecycle_plan.orphaned_slugs,
                "missing_slugs": lifecycle_plan.missing_slugs,
                "destroyed_slugs": cleanup_record.destroyed_slugs,
                "failed_slugs": cleanup_record.failed_slugs,
                "blocked_slugs": cleanup_record.blocked_slugs,
                "error_message": cleanup_record.error_message,
            }
        )
        entries.append(entry)
    status = "pass"
    for entry in entries:
        if entry.get("status") in {"fail", "blocked"}:
            status = "fail"
            break
        if entry.get("status") == "skipped" and status != "fail":
            status = "partial"
    return {
        "status": status,
        "apply": request.apply,
        "profile_count": len(entries),
        "profiles": entries,
    }


def _latest_preview_inventory_scan(
    *, record_store: object, context_name: str
) -> PreviewInventoryScanRecord | None:
    list_scans = getattr(record_store, "list_preview_inventory_scan_records", None)
    if not callable(list_scans):
        return None
    scans = list_scans(context_name=context_name, limit=1)
    return next(iter(scans), None)


def write_preview_inventory_scan_record(
    *,
    record_store: PreviewLifecycleSweepStore,
    context: str,
    source: str,
    preview_slugs: tuple[str, ...],
) -> str:
    scanned_at = utc_now_timestamp()
    scan_id = build_preview_inventory_scan_id(
        context_name=context,
        scanned_at=scanned_at,
    )
    record_store.write_preview_inventory_scan_record(
        PreviewInventoryScanRecord(
            scan_id=scan_id,
            context=context,
            scanned_at=scanned_at,
            source=source,
            status="pass",
            preview_count=len(preview_slugs),
            preview_slugs=preview_slugs,
        )
    )
    return scan_id


def write_preview_desired_state_record(
    *, record_store: PreviewLifecycleSweepStore, record: PreviewDesiredStateRecord
) -> str:
    record_store.write_preview_desired_state_record(record)
    return record.desired_state_id


def write_preview_lifecycle_plan_record(
    *, record_store: PreviewLifecycleSweepStore, record: PreviewLifecyclePlanRecord
) -> str:
    record_store.write_preview_lifecycle_plan_record(record)
    return record.plan_id


def write_preview_lifecycle_sweep_cleanup_record(
    *, record_store: PreviewLifecycleSweepStore, record: PreviewLifecycleCleanupRecord
) -> str:
    record_store.write_preview_lifecycle_cleanup_record(record)
    return record.cleanup_id


def _product_driver_compatible(
    *, profile: LaunchplaneProductProfileRecord, expected_driver_id: str
) -> bool:
    expected = expected_driver_id.strip()
    profile_driver_id = profile.driver_id.strip()
    if profile_driver_id == expected:
        return True
    try:
        descriptor = read_driver_descriptor(profile_driver_id)
    except FileNotFoundError:
        return False
    return descriptor.base_driver_id == expected
