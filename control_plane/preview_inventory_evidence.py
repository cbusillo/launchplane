"""One interpretation of persisted provider inventory, independent of generation health."""

from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel

from control_plane.contracts.data_provenance import DataProvenance
from control_plane.contracts.preview_inventory_scan_record import PreviewInventoryScanRecord

PREVIEW_INVENTORY_STALE_AFTER = timedelta(minutes=30)
PREVIEW_INVENTORY_SCAN_READ_LIMIT = 100


class PreviewInventoryEvidence(BaseModel):
    status: Literal["empty", "nonempty", "failed", "stale", "unsupported", "unavailable", "missing"]
    observed_count: int | None = None
    observed_slugs: tuple[str, ...] = ()
    provenance: DataProvenance


def read_preview_inventory_evidence(
    *, record_store: object, context: str, product: str = "", repository: str = ""
) -> PreviewInventoryEvidence:
    read = getattr(record_store, "list_preview_inventory_scan_records", None)
    status: Literal["missing", "unsupported", "unavailable"] = "missing"
    detail = "No matching scoped preview inventory scan is available in the recent record window."
    scan = None
    if not callable(read):
        status, detail = "unsupported", "The record store does not support preview inventory scans."
    else:
        try:
            # Context-only legacy scans cannot prove which product/repository was
            # enumerated (or whether applications versus composes were read).
            candidates = (
                item
                for item in read(context_name=context, limit=PREVIEW_INVENTORY_SCAN_READ_LIMIT)
                if item.context == context
                and item.product == product
                and item.repository == repository
                and bool(product)
                and bool(repository)
            )
            scan = next(iter(candidates), None)
        except Exception:
            status, detail = "unavailable", "Preview inventory scan records could not be read."
    if scan is None:
        return PreviewInventoryEvidence(
            status=status,
            provenance=DataProvenance(
                source_kind="unsupported" if status == "unsupported" else "record",
                freshness_status="unsupported" if status == "unsupported" else "missing",
                detail=detail,
            ),
        )
    return inventory_scan_evidence(scan)


def inventory_scan_evidence(scan: PreviewInventoryScanRecord) -> PreviewInventoryEvidence:
    try:
        observed = datetime.fromisoformat(scan.scanned_at.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError("Inventory timestamp requires timezone")
        stale_at = observed + PREVIEW_INVENTORY_STALE_AFTER
        now = datetime.now(timezone.utc)
        valid_time = observed <= now
    except ValueError:
        stale_at, valid_time = None, False
    status: Literal["empty", "nonempty", "failed", "stale", "unsupported", "unavailable"]
    if scan.status == "unsupported":
        status = "unsupported"
    elif scan.status == "unavailable":
        status = "unavailable"
    elif scan.status != "pass" or scan.truncated or not valid_time:
        status = "failed"
    elif stale_at is not None and now > stale_at:
        status = "stale"
    else:
        status = "nonempty" if scan.preview_count else "empty"
    fresh = status in {"empty", "nonempty"}
    return PreviewInventoryEvidence(
        status=status,
        observed_count=scan.preview_count if scan.status == "pass" and not scan.truncated else None,
        observed_slugs=scan.preview_slugs if scan.status == "pass" and not scan.truncated else (),
        provenance=DataProvenance(
            source_kind="record",
            source_record_id=scan.scan_id,
            recorded_at=scan.scanned_at,
            refreshed_at=scan.scanned_at,
            stale_after=stale_at.isoformat().replace("+00:00", "Z") if stale_at else "",
            freshness_status="verified"
            if fresh
            else "stale"
            if status == "stale"
            else "unsupported"
            if status == "unsupported"
            else "missing",
            detail=f"Provider preview inventory scan: {status}; {scan.preview_count} observed item(s).",
        ),
    )
