from __future__ import annotations

from pathlib import Path
from typing import cast

import click

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.drivers.generic_web_preview_dispatch import (
    GenericWebPreviewInventoryEnvelope,
    GenericWebPreviewReadinessEnvelope,
    _GENERIC_WEB_PREVIEW_INVENTORY_ROUTE,
    _GENERIC_WEB_PREVIEW_READINESS_ROUTE,
    _write_preview_inventory_scan_if_supported,
)
from control_plane.workflows.generic_web_deploy import product_profile_uses_generic_web_base
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewProfileStore,
    evaluate_generic_web_preview_readiness,
    execute_generic_web_preview_inventory,
)
from control_plane.workflows.ship import utc_now_timestamp


GENERIC_WEB_PREVIEW_INVENTORY_ROUTE = _GENERIC_WEB_PREVIEW_INVENTORY_ROUTE.route_path
GENERIC_WEB_PREVIEW_READINESS_ROUTE = _GENERIC_WEB_PREVIEW_READINESS_ROUTE.route_path
GENERIC_WEB_PREVIEW_INVENTORY_ACTION = "preview_inventory.read"
GENERIC_WEB_PREVIEW_READINESS_ACTION = "preview_readiness.evaluate"

__all__ = [
    "GENERIC_WEB_PREVIEW_INVENTORY_ACTION",
    "GENERIC_WEB_PREVIEW_INVENTORY_ROUTE",
    "GENERIC_WEB_PREVIEW_READINESS_ACTION",
    "GENERIC_WEB_PREVIEW_READINESS_ROUTE",
    "GenericWebPreviewInventoryEnvelope",
    "GenericWebPreviewReadinessEnvelope",
    "GenericWebPreviewProductMismatchError",
    "GenericWebPreviewRouteDependencyError",
    "apply_generic_web_preview_inventory_result",
    "apply_generic_web_preview_readiness_result",
    "resolve_generic_web_preview_profile",
]


class GenericWebPreviewProductMismatchError(ValueError):
    pass


class GenericWebPreviewRouteDependencyError(ValueError):
    pass


def resolve_generic_web_preview_profile(
    *, record_store: object, product: str
) -> LaunchplaneProductProfileRecord:
    read_profile = getattr(record_store, "read_product_profile_record", None)
    if not callable(read_profile):
        raise GenericWebPreviewRouteDependencyError(
            "Product driver validation requires product profile storage."
        )
    try:
        profile = read_profile(product.strip())
    except FileNotFoundError as error:
        raise GenericWebPreviewRouteDependencyError from error
    if not isinstance(profile, LaunchplaneProductProfileRecord):
        profile = LaunchplaneProductProfileRecord.model_validate(profile)
    if not product_profile_uses_generic_web_base(profile):
        raise GenericWebPreviewProductMismatchError(
            "Product profile is not compatible with the requested driver route."
        )
    if not profile.preview.enabled:
        raise click.ClickException(
            f"Product {profile.product!r} does not have generic-web previews enabled."
        )
    if not profile.preview.context.strip():
        raise click.ClickException(
            f"Product {profile.product!r} does not define a preview context."
        )
    return profile


def apply_generic_web_preview_inventory_result(
    *,
    control_plane_root: Path,
    record_store: object,
    request: GenericWebPreviewInventoryEnvelope,
    profile: LaunchplaneProductProfileRecord,
) -> tuple[dict[str, object], dict[str, object]]:
    driver_result = execute_generic_web_preview_inventory(
        control_plane_root=control_plane_root,
        record_store=cast(GenericWebPreviewProfileStore, record_store),
        request=request.inventory,
        profile=profile,
    )
    preview_inventory_scan_id = _write_preview_inventory_scan_if_supported(
        record_store=record_store,
        context=driver_result.context,
        source=driver_result.source,
        preview_slugs=tuple(item.previewSlug for item in driver_result.previews),
    )
    records: dict[str, object] = {}
    if preview_inventory_scan_id:
        records["preview_inventory_scan_id"] = preview_inventory_scan_id
    return records, driver_result.model_dump(mode="json")


def apply_generic_web_preview_readiness_result(
    *,
    control_plane_root: Path,
    record_store: object,
    request: GenericWebPreviewReadinessEnvelope,
    profile: LaunchplaneProductProfileRecord,
) -> tuple[dict[str, object], dict[str, object]]:
    driver_result = evaluate_generic_web_preview_readiness(
        control_plane_root=control_plane_root,
        record_store=cast(GenericWebPreviewProfileStore, record_store),
        request=request.readiness,
        checked_at=utc_now_timestamp(),
        profile=profile,
    )
    return {}, driver_result.model_dump(mode="json")
