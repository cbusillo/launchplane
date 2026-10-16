"""Provider reads and append-only inventory evidence; no desired state or cleanup."""

from collections.abc import Callable
from pathlib import Path
from datetime import datetime, timezone
from typing import Protocol, cast
import uuid

import click

from control_plane.contracts.preview_inventory_scan_record import (
    PreviewInventoryScanRecord,
    PreviewInventoryScanStatus,
    build_preview_inventory_scan_id,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.dokploy import source as dokploy_source
from control_plane.drivers.registry import read_driver_descriptor
from control_plane.preview_lifecycle_cleanup_routes import PreviewLifecycleSweepCancelled
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewInventoryRequest,
    GenericWebPreviewProfileStore,
    effective_preview_app_name_prefix,
    execute_generic_web_preview_inventory,
    preview_slug_from_application_name,
)
from control_plane.workflows.odoo_preview_runtime import _complete_preview_compose_inventory


class PreviewInventoryCollectionStore(Protocol):
    def list_product_profile_records(self) -> tuple[LaunchplaneProductProfileRecord, ...]: ...

    def write_preview_inventory_scan_record(self, record: PreviewInventoryScanRecord) -> object: ...


def collect_preview_inventory(
    *,
    control_plane_root: Path,
    record_store: PreviewInventoryCollectionStore,
    should_stop: Callable[[], bool] = lambda: False,
) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    for profile in record_store.list_product_profile_records():
        if should_stop():
            raise PreviewLifecycleSweepCancelled()
        if not profile.is_active or not profile.preview.enabled:
            continue
        status: PreviewInventoryScanStatus = "pass"
        slugs: tuple[str, ...] = ()
        error_message = ""
        try:
            descriptor = read_driver_descriptor(profile.driver_id)
            base = descriptor.base_driver_id or descriptor.driver_id
            if descriptor.driver_id == "odoo" or base == "odoo":
                host, token = dokploy_source.read_dokploy_config(
                    control_plane_root=control_plane_root
                )
                inventory = _complete_preview_compose_inventory(host=host, token=token)
                slugs = tuple(
                    slug
                    for _, _, name in inventory
                    if (
                        slug := preview_slug_from_application_name(
                            app_name_suffix=profile.preview.app_name_suffix,
                            app_name_prefix=effective_preview_app_name_prefix(profile=profile),
                            application_name=name,
                        )
                    )
                )
            elif base == "generic-web":
                result = execute_generic_web_preview_inventory(
                    control_plane_root=control_plane_root,
                    record_store=cast(GenericWebPreviewProfileStore, record_store),
                    request=GenericWebPreviewInventoryRequest(product=profile.product),
                    profile=profile,
                )
                slugs = tuple(item.previewSlug for item in result.previews)
            else:
                status = "unsupported"
            if len(slugs) != len(set(slugs)):
                raise click.ClickException(
                    "Provider inventory has ambiguous duplicate preview slugs."
                )
        except click.ClickException:
            status, slugs = "fail", ()
            error_message = "Provider inventory scan failed or returned incomplete inventory."
        except Exception:
            # Do not persist provider response text or credential-bearing errors.
            status, slugs = "unavailable", ()
            error_message = "Provider inventory collection is unavailable."
        scanned_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        scan = PreviewInventoryScanRecord(
            scan_id=build_preview_inventory_scan_id(
                context_name=profile.preview.context, scanned_at=scanned_at
            )
            + "-"
            + uuid.uuid4().hex,
            context=profile.preview.context,
            product=profile.product,
            repository=profile.repository,
            scanned_at=scanned_at,
            source="service-preview-inventory",
            status=status,
            preview_count=len(slugs),
            preview_slugs=slugs,
            error_message=error_message,
        )
        record_store.write_preview_inventory_scan_record(scan)
        entries.append(
            {
                "product": profile.product,
                "context": profile.preview.context,
                "status": status,
                "scan_id": scan.scan_id,
            }
        )
    return {
        "status": "pass" if all(e["status"] == "pass" for e in entries) else "partial",
        "apply": False,
        "profiles": entries,
    }
