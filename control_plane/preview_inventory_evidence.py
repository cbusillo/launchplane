"""One interpretation of persisted provider inventory, independent of generation health."""

from datetime import datetime, timedelta, timezone
import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from control_plane.contracts.data_provenance import DataProvenance
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.preview_inventory_scan_record import (
    PreviewInventoryScanRecord,
    PreviewSleepObservation,
    PreviewSleepEvidence,
)

PREVIEW_INVENTORY_STALE_AFTER = timedelta(minutes=30)
PREVIEW_INVENTORY_INTERVAL_SECONDS = int(PREVIEW_INVENTORY_STALE_AFTER.total_seconds() / 3)
PREVIEW_INVENTORY_SCAN_READ_LIMIT = 100
_LOGGER = logging.getLogger(__name__)


class PreviewInventoryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["empty", "nonempty", "failed", "stale", "unsupported", "unavailable", "missing"]
    observed_count: int | None = None
    observed_slugs: tuple[str, ...] = ()
    sleep_observations: tuple[PreviewSleepObservation, ...] = ()
    provenance: DataProvenance


def read_preview_inventory_evidence(
    *,
    record_store: object,
    context: str,
    product: str = "",
    repository: str = "",
    include_sleep_evidence: bool = False,
) -> PreviewInventoryEvidence:
    read = getattr(record_store, "list_preview_inventory_scan_records", None)
    status: Literal["missing", "unsupported", "unavailable"] = "missing"
    detail = "No matching scoped preview inventory scan is available in the recent record window."
    scan: PreviewInventoryScanRecord | None = None
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
    return inventory_scan_evidence(scan, include_sleep_evidence=include_sleep_evidence)


def inventory_scan_evidence(
    scan: PreviewInventoryScanRecord, *, include_sleep_evidence: bool = False
) -> PreviewInventoryEvidence:
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
        sleep_observations=tuple(
            item if include_sleep_evidence else item.model_copy(update={"evidence": None})
            for item in scan.sleep_observations
        )
        if fresh
        else (),
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


def current_sleep_binding(
    record_store: object,
    evidence: PreviewSleepEvidence,
    profile: LaunchplaneProductProfileRecord,
    slug: str,
) -> str:
    binding = evidence.binding
    if binding is None:
        return "unknown"
    if (binding.product, binding.context) != (profile.product, profile.preview.context):
        return "changed"
    try:
        preview = getattr(record_store, "read_preview_record")(binding.preview_id)
        if (
            preview.context != binding.context
            or preview.anchor_repo not in {profile.repository, profile.repository.partition("/")[2]}
            or profile.preview.slug_template.format(number=preview.anchor_pr_number) != slug
            or preview.state != "active"
            or preview.serving_generation_id != binding.generation_id
            or preview.active_generation_id != binding.generation_id
        ):
            return "changed"
        generation = getattr(record_store, "read_preview_generation_record")(binding.generation_id)
        identity = generation.runtime_identity
        if identity is None:
            return "unknown"
        if any(
            value != getattr(identity, "preview_generation_id" if key == "generation_id" else key)
            for key, value in binding.model_dump().items()
            if key != "provider_target_id"
        ):
            return "changed"
        preparation = getattr(record_store, "read_idempotency_record_by_id")(
            identity.deployment_record_id
        )
        if preparation is None or preparation.state != "completed":
            return "unknown"
        result = preparation.response_payload.get("result")
        target = result.get("compose_id") if isinstance(result, dict) else None
        if not isinstance(target, str) or not target:
            return "unknown"
        return "current" if target == binding.provider_target_id else "changed"
    except FileNotFoundError:
        return "unknown"
    except (OSError, ValueError, TypeError, AttributeError, SQLAlchemyError):
        _LOGGER.warning("Current preview sleep binding records are unavailable")
        return "unknown"


def project_sleep_observations(
    inventory: PreviewInventoryEvidence,
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    include_private: bool,
) -> PreviewInventoryEvidence:
    projected = []
    for observation in inventory.sleep_observations:
        evidence = observation.evidence or PreviewSleepEvidence()
        current = current_sleep_binding(record_store, evidence, profile, observation.slug)
        try:
            completed = datetime.fromisoformat(evidence.completed_at.replace("Z", "+00:00"))
            started = datetime.fromisoformat(
                inventory.provenance.recorded_at.replace("Z", "+00:00")
            )
            now = datetime.now(timezone.utc)
            fresh = started <= completed <= now <= completed + PREVIEW_INVENTORY_STALE_AFTER
            freshness = "fresh" if fresh else "stale" if started <= completed <= now else "invalid"
        except (ValueError, TypeError):
            fresh = False
            freshness = "invalid" if evidence.completed_at else "unknown"
        evidence = evidence.model_copy(
            update={"current_binding": current if fresh else "unknown", "freshness": freshness}
        )
        updates: dict[str, object] = {"evidence": evidence if include_private else None}
        if observation.evidence is not None and (current != "current" or not fresh):
            updates.update(state="unknown", reason="Exact current sleep binding is unqualified.")
        projected.append(observation.model_copy(update=updates))
    return inventory.model_copy(update={"sleep_observations": tuple(projected)})
