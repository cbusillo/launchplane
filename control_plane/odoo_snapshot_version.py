from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from control_plane.odoo_copy_aggregates import (
    BASE_VERSION_PATTERN,
    ARCHIVE_UNAVAILABLE_CODES,
    SNAPSHOT_VERSION_PROGRAM,
    read_snapshot_archive,
)
from control_plane.odoo_runtime_reads import OdooRuntimeReadError
from control_plane.preview_reservation_recovery import original_preview_intent
from control_plane.storage.postgres import PostgresRecordStore


ROUTE = "/v1/products/{product}/preview-snapshot-version"
SnapshotDigest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ReservationId = Annotated[str, Field(pattern=r"^mutation-reservation-[a-f0-9]{64}$")]


class SnapshotVersionQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    original_reservation_id: ReservationId
    expected_snapshot_id: SnapshotDigest
    expected_database_sha256: SnapshotDigest


class SnapshotVersionMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recorded_base_version: str = Field(pattern=f"^{BASE_VERSION_PATTERN}$", max_length=64)
    preparation_stage: None = None


class SnapshotVersionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str
    context: str
    original_reservation_id: ReservationId
    snapshot_id: SnapshotDigest
    snapshot_database_sha256: SnapshotDigest
    snapshot_captured_at: str
    observed_at: str
    read_only: Literal[True] = True
    metadata: SnapshotVersionMetadata


class SnapshotVersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    trace_id: str
    result: SnapshotVersionEvidence


def read_snapshot_version(
    *, store: PostgresRecordStore, product: str, query: SnapshotVersionQuery
) -> SnapshotVersionEvidence:
    profile = store.read_product_profile_record(product)
    if not profile.is_active or profile.driver_id != "odoo":
        raise ValueError("Snapshot version reads require an active Odoo product.")
    reservation = store.read_idempotency_record_by_id(query.original_reservation_id)
    if reservation is None or reservation.record_id != query.original_reservation_id:
        raise OdooRuntimeReadError(
            "snapshot_original_unavailable", "The original preview reservation is unavailable.", 404
        )
    try:
        original = original_preview_intent(reservation, profile=profile, store=store)
    except ValueError as error:
        raise OdooRuntimeReadError(
            "snapshot_binding_mismatch",
            "The retained original snapshot binding could not be verified.",
            409,
        ) from error
    snapshot = original.issued_plan.dry_run_plan.clean_snapshot
    if (
        original.issued_plan.operation != "refresh"
        or snapshot is None
        or (snapshot.product, snapshot.context) != (profile.product, original.issued_plan.context)
        or snapshot.snapshot_id != query.expected_snapshot_id
        or snapshot.database_sha256 != query.expected_database_sha256
    ):
        raise OdooRuntimeReadError(
            "snapshot_binding_mismatch",
            "The original preview snapshot binding differs from the requested evidence.",
            409,
        )
    frames = read_snapshot_archive(
        store=store,
        context=snapshot.context,
        snapshot=snapshot,
        program=SNAPSHOT_VERSION_PROGRAM,
        query={"table": "ir_module_module"},
    )
    if (
        len(frames) != 1
        or set(frames[0]) != {"type", "metadata"}
        or frames[0]["type"] != "complete"
    ):
        reason = frames[0].get("unavailable") if len(frames) == 1 else None
        code = (
            str(reason) if reason in ARCHIVE_UNAVAILABLE_CODES else "snapshot_version_unavailable"
        )
        raise OdooRuntimeReadError(code, "The bound snapshot metadata read did not complete.", 503)
    return SnapshotVersionEvidence(
        product=product,
        context=snapshot.context,
        original_reservation_id=reservation.record_id,
        snapshot_id=snapshot.snapshot_id,
        snapshot_database_sha256=snapshot.database_sha256,
        snapshot_captured_at=snapshot.captured_at,
        observed_at=datetime.now(timezone.utc).isoformat(),
        metadata=SnapshotVersionMetadata.model_validate(frames[0]["metadata"]),
    )
