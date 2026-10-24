from __future__ import annotations

from datetime import datetime
from typing import Protocol, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget

if TYPE_CHECKING:
    from control_plane.storage.postgres import PostgresRecordStore
    from control_plane.contracts.odoo_stable_target_replacement_operation import (
        OdooStableTargetReplacementOperationRecord,
    )


CLEAN_SNAPSHOT_SCOPE = "launchplane:clean-testing-snapshot:v1"
CLEAN_SNAPSHOT_ROUTE = "service:clean-testing-snapshot"


class CleanTestingSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product: str = Field(min_length=1)
    context: str = Field(min_length=1)
    snapshot_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    server_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    database_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    filestore_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    captured_at: str

    @field_validator("captured_at")
    @classmethod
    def aware_capture_time(cls, value: str) -> str:
        if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("Snapshot capture time requires a timezone.")
        return value


class CleanTestingSnapshotStore(Protocol):
    def read_clean_testing_snapshot(
        self, product: str, context: str
    ) -> CleanTestingSnapshot | None: ...


def snapshot_lane_key(product: str, context: str) -> str:
    return canonical_json_sha256([product, context, "testing"])


def snapshot_storage_budget(
    profile: LaunchplaneProductProfileRecord, context: str
) -> OdooTestingDrillStorageBudget | None:
    if not profile.preview.clean_testing_snapshot:
        return None
    lane = next(
        item for item in profile.lanes if (item.context, item.instance) == (context, "testing")
    )
    return lane.odoo_testing_drill_storage


def preview_snapshot(
    store: CleanTestingSnapshotStore, profile: LaunchplaneProductProfileRecord
) -> CleanTestingSnapshot:
    if (
        not profile.preview.clean_testing_snapshot
        or profile.driver_id != "odoo"
        or profile.preview.template_instance != "testing"
    ):
        raise ValueError("Preview requires an opted-in clean snapshot source.")
    snapshot = store.read_clean_testing_snapshot(profile.product, profile.preview.context)
    if snapshot is None:
        raise ValueError("No completed clean snapshot is available for this product.")
    if (snapshot.product, snapshot.context) != (profile.product, profile.preview.context):
        raise ValueError("The clean snapshot belongs to another product or context.")
    return snapshot


def clean_snapshot_from_evidence(
    *,
    product: str,
    context: str,
    snapshot_id: str,
    server_id: str,
    evidence: dict[str, str],
    captured_at: str,
) -> CleanTestingSnapshot:
    if any(
        evidence.get(key) != "true"
        for key in (
            "testing_copy_sanitized",
            "integration_readback_ok",
            "odoo_restore_completed",
            "clean_testing_snapshot_completed",
        )
    ):
        raise ValueError("A clean snapshot requires a completed sanitized refresh and capture.")
    return CleanTestingSnapshot(
        product=product,
        context=context,
        snapshot_id=snapshot_id,
        server_id=server_id,
        database_sha256=evidence.get("clean_snapshot_database_sha256", ""),
        filestore_sha256=evidence.get("clean_snapshot_filestore_sha256", ""),
        captured_at=captured_at,
    )


def completed_operation_snapshot(
    store: PostgresRecordStore, operation: OdooStableTargetReplacementOperationRecord
) -> CleanTestingSnapshot | None:
    from control_plane.odoo_testing_restore import generation_binding

    if operation.status != "pass" or operation.instance != "testing" or operation.result is None:
        return None
    if operation.result.health_status != "pass":
        return None
    if (
        not operation.is_native_testing_rebuild
        and operation.request.data_source_mode != "upstream_restore"
    ):
        return None
    if operation.is_native_testing_rebuild:
        namespace = operation.result.runtime_source.get("testing_candidate", "")
        candidate = store.read_testing_candidate(namespace) if namespace else None
        if candidate is None or candidate.application is None:
            return None
        app = candidate.application
        generation = app.plan.generation
        if generation is None or not generation.clean_snapshot_enabled:
            return None
        if candidate.operation_id != operation.operation_id or app.prepared is None:
            raise ValueError("Clean snapshot publication requires its prepared candidate.")
        evidence = app.workflow_evidence
        server_id = candidate.server_id
        captured_at = app.prepared.observed_at.isoformat()
    else:
        generation = store.read_fresh_odoo_testing_generation(operation.product, operation.context)
        if generation is None or not generation.clean_snapshot_enabled:
            return None
        if not generation.completed_at or operation.result.target_id != generation.target_id:
            raise ValueError(
                "Clean snapshot publication requires its completed testing generation."
            )
        prefix = "post_deploy_readback_"
        evidence = {
            key.removeprefix(prefix): value
            for key, value in operation.result.post_deploy_override_evidence.items()
            if key.startswith(prefix)
        }
        server_id = evidence.get("clean_snapshot_server_id", "")
        captured_at = generation.completed_at
    return clean_snapshot_from_evidence(
        product=operation.product,
        context=operation.context,
        snapshot_id=generation_binding(generation),
        server_id=server_id,
        evidence=evidence,
        captured_at=captured_at,
    )
