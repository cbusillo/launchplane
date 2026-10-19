"""Receipt-bound storage maintenance before a drill selects its new database."""

from collections.abc import Callable
from pathlib import Path
import time
import re
from typing import Any, cast

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.dokploy import api, source
from control_plane.dokploy.native_copy_stream import _read_remote
from control_plane.odoo_testing_drill import (
    DRILL_ROUTE,
    DRILL_SCOPE,
    DrillPrerequisite,
    DrillSkipped,
    drill_receipt,
    require_drill_use_unprotected,
)
from control_plane.odoo_testing_restore import (
    FRESH_TESTING_RESTORE_HOST_PROGRAM,
    FreshOdooTestingGeneration,
    fresh_testing_authority_sha256,
    generation_binding,
    read_current_testing_generation,
    TESTING_COPY_COMPLETION_PARAMETER,
)
from control_plane.storage.postgres import PostgresRecordStore

from control_plane._odoo_testing_storage_program import MAX_DISPOSALS_PER_PASS

MaintenanceDispatch = Callable[[Callable[[], None]], None]


def storage_key(operation_id: str) -> str:
    return "storage:" + operation_id


def completed_storage_key(operation_id: str, evidence: dict[str, Any]) -> str:
    return storage_key(operation_id) + ":completed:" + canonical_json_sha256(evidence)


def storage_receipt(
    store: PostgresRecordStore, operation_id: str
) -> LaunchplaneIdempotencyRecord | None:
    return store.read_idempotency_record(
        scope=DRILL_SCOPE, route_path=DRILL_ROUTE, idempotency_key=storage_key(operation_id)
    )


def maintenance_spec(
    store: PostgresRecordStore,
    generation: FreshOdooTestingGeneration,
    copies: list[dict[str, str]],
    budget: dict[str, int],
    expires_at: float,
    *,
    check_capacity: bool = True,
) -> dict[str, object]:
    """Only stored recipient authority; host code independently inspects every consumer."""
    from control_plane.dokploy.compose import ODOO_DATABASE_DATA_DIRECTORY

    target = store.read_dokploy_target_record(
        context_name=generation.context, instance_name="testing"
    )
    values: dict[str, Any] = {}
    for scope in ("context", "instance"):
        for runtime in store.list_runtime_environment_records(context_name=generation.context):
            if runtime.scope == scope and runtime.instance == (
                "testing" if scope == "instance" else ""
            ):
                values.update(runtime.env)
    values.update(target.env)
    inventory = store.read_environment_inventory(
        context_name=generation.context, instance_name="testing"
    )
    if inventory.artifact_identity is None:
        raise DrillPrerequisite("testing_artifact_required")
    manifest = store.read_artifact_manifest(inventory.artifact_identity.artifact_id)
    return {
        "generation": generation.model_dump(mode="json"),
        "compose_project": generation.namespace,
        "database_name": values.get("ODOO_DB_NAME", ""),
        "image_reference": manifest.image.repository + "@" + manifest.image.digest,
        "expected_overrides_payload": values.get("ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64", ""),
        "database_data_directory": ODOO_DATABASE_DATA_DIRECTORY,
        "copy_binding_sha256": generation_binding(generation),
        "copy_witness_parameter": TESTING_COPY_COMPLETION_PARAMETER,
        # Maintenance never restores or executes modules; no module intent is invented.
        "storage_maintenance": {
            "copies": copies,
            "budget": budget,
            "expires_at": expires_at,
            "check_capacity": check_capacity,
        },
    }


def execute_maintenance(
    root: Path, target_id: str, spec: dict[str, object], dispatch_guard: MaintenanceDispatch
) -> dict[str, object]:
    host, token = source.read_dokploy_config(control_plane_root=root)
    target = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=target_id
    )
    project = str(target.get("appName") or "")
    if not re.fullmatch(re.escape(str(spec["compose_project"])) + r"(?:-[a-z0-9]{6})?", project):
        raise DrillPrerequisite("testing_storage_project_changed")
    frames: list[dict[str, object]] = []
    _read_remote(
        host=host,
        token=token,
        server_id=str(target.get("serverId") or ""),
        program=FRESH_TESTING_RESTORE_HOST_PROGRAM,
        payload={**spec, "compose_project": project},
        timeout_seconds=120,
        receive=frames.append,
        dispatch_guard=dispatch_guard,
    )
    if len(frames) != 1 or frames[0].get("type") != "complete":
        raise DrillPrerequisite("testing_storage_readback_unavailable")
    return frames[0]


def prepare_drill_storage(
    store: PostgresRecordStore,
    operation: Any,
    root: Path,
    *,
    execute: Callable[[Path, str, dict[str, object], MaintenanceDispatch], dict[str, object]]
    | None = None,
    now: float | None = None,
    checkpoint: Callable[[], None],
) -> None:
    if operation.idempotency_scope != DRILL_SCOPE:
        return
    from control_plane.testing_lane_hold import read_staff_testing_hold

    if read_staff_testing_hold(record_store=store, context=operation.context) is not None:
        raise DrillSkipped("staff_testing_hold")
    clock = time.time if now is None else lambda: now
    generation = read_current_testing_generation(
        store, product=operation.product, context=operation.context, instance="testing"
    )
    if generation is None:
        raise DrillPrerequisite("testing_generation_required")
    slot = store.read_idempotency_record(
        scope=DRILL_SCOPE, route_path=DRILL_ROUTE, idempotency_key=operation.idempotency_key
    )
    if slot is None:
        raise DrillPrerequisite("drill_receipt_missing")
    if not operation.is_native_testing_rebuild and generation_binding(
        generation
    ) == slot.response_payload.get("generation_binding"):
        # Original activation already consumed the admission. Recovery keeps its
        # same generation; it must not collect or delete against a different copy.
        return
    profile = store.read_product_profile_record(operation.product)
    lane = next(
        lane
        for lane in profile.lanes
        if (lane.context, lane.instance) == (operation.context, "testing")
    )
    if lane.odoo_testing_drill_storage is None:
        store.update_odoo_testing_drill_receipt(
            operation.idempotency_key, time.time(), {"storage_status": "unconfigured"}
        )
        raise DrillPrerequisite("testing_storage_budget_required")
    budget = lane.odoo_testing_drill_storage.model_dump(
        exclude={"retained_copy_bindings", "retained_candidate_namespaces"}
    )
    pending = store.list_odoo_testing_disposal_receipts(operation.product, operation.context)
    copies = []
    selected = []
    retained = {}
    protected = {generation.drill_database, generation.retained_drill_database}
    for receipt in pending:
        planned = FreshOdooTestingGeneration.model_validate(
            receipt.response_payload["planned_generation"]
        )
        if (
            planned.discard_drill_database in protected
            or not planned.discard_drill_database
            or planned.discard_copy_binding
            in lane.odoo_testing_drill_storage.retained_copy_bindings
        ):
            retained[receipt.idempotency_key] = planned.model_dump(mode="json")
            continue
        if (planned.namespace, planned.target_id, planned.resources_binding_sha256) != (
            generation.namespace,
            generation.target_id,
            generation.resources_binding_sha256,
        ):
            retained[receipt.idempotency_key] = planned.model_dump(mode="json")
            continue
        copies.append(
            {
                "discard_drill_database": planned.discard_drill_database,
                "discard_copy_binding": planned.discard_copy_binding,
            }
        )
        selected.append(receipt.idempotency_key)
        if len(copies) == MAX_DISPOSALS_PER_PASS:
            break
    intent = storage_receipt(store, operation.operation_id)
    if (
        operation.is_native_testing_rebuild
        and not copies
        and not (intent is not None and intent.response_payload.get("phase") == "dispatching")
    ):
        # Different-project legacy copies retain their original custody. With
        # no eligible legacy backlog, no legacy receiver or old-pool capacity
        # requirement belongs to the independent native allocation.
        for key in retained:
            store.update_odoo_testing_drill_receipt(key, clock(), {"disposal_status": "retained"})
        return
    binding = fresh_testing_authority_sha256(store, generation)
    payload = {
        "binding": binding,
        "copies": copies,
        "receipts": selected,
        "budget": budget,
        "phase": "dispatching",
        "retained_receipts": retained,
    }
    if intent is not None and intent.response_payload.get("phase") == "dispatching":
        # Unknown transport outcomes retain the original cleanup identities.
        if intent.response_payload.get("binding") != binding:
            raise DrillPrerequisite("testing_storage_authority_changed")
        payload = dict(intent.response_payload)
        copies = cast(list[dict[str, str]], payload["copies"])
    else:
        store.write_idempotency_record(
            drill_receipt(storage_key(operation.operation_id), time.time(), cast(Any, payload))
        )

    def perform(dispatch_guard: MaintenanceDispatch) -> dict[str, object]:
        safety = store.read_disposable_lane_safety(operation.product, operation.context, "testing")
        if copies and safety is None:
            raise DrillPrerequisite("usage_custody_or_isolation_unverified")
        spec = maintenance_spec(
            store,
            generation,
            copies,
            budget,
            safety.expires_at.timestamp() if safety is not None else 0,
            check_capacity=not operation.is_native_testing_rebuild,
        )
        return (execute or execute_maintenance)(root, generation.target_id, spec, dispatch_guard)

    from control_plane.disposable_lanes import DisposableLaneSafetyUnknown

    try:
        result = store.reconcile_odoo_testing_storage(
            operation, binding, payload, perform, clock=clock, checkpoint=checkpoint
        )
    except DisposableLaneSafetyUnknown as error:
        require_drill_use_unprotected(store, operation.product, operation.context, now=clock())
        raise DrillPrerequisite("usage_custody_or_isolation_unverified") from error
    if result.get("skip_code"):
        raise DrillSkipped(str(result["skip_code"]))
    if not result["authority_current"]:
        raise DrillPrerequisite("testing_storage_authority_changed")
    if not result["capacity"]:
        raise DrillPrerequisite("testing_storage_capacity_insufficient")
    if store.list_odoo_testing_disposal_receipts(operation.product, operation.context):
        # A healthy copy remains healthy. Refuse a *new* capture while old
        # witnessed storage is still deferred, rather than accumulating copies.
        raise DrillPrerequisite(
            "testing_disposal_deferred"
            if "deferred" in cast(list[str], result["disposals"])
            else "testing_storage_reconciliation_pending"
        )


def capacity_admission_binding(generation: FreshOdooTestingGeneration, operation: Any) -> str:
    return canonical_json_sha256(
        [generation_binding(generation), operation.operation_id, operation.attempt]
    )
