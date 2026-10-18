"""Retain uncertain, undeployed storage allocations without deleting their custody."""

from collections.abc import Callable
from typing import Any

from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    parse_launchplane_mutation_timestamp,
)
from control_plane.http_routes.mutation_support import request_fingerprint
from control_plane.provider_operations import provider_operation_response_payload
from control_plane.application_state_volume_errors import StateVolumeRefusal
from control_plane.storage.postgres import PostgresRecordStore


def require_state_volume_retention_eligibility(
    store: PostgresRecordStore, *, product: str, context: str, application_id: str
) -> None:
    """Check current lane ownership and untracked custody under the authority lock."""
    try:
        current = store.read_provider_target_record(context_name=context, instance_name="testing")
    except FileNotFoundError:
        current = None
    if current is not None and current.target_id == application_id:
        raise StateVolumeRefusal(
            "application_state_volume_already_adopted",
            "Original allocation is already tracked; use original-key recovery.",
        )
    require_state_volume_owner(store, product=product, context=context)


def require_state_volume_owner(store: PostgresRecordStore, *, product: str, context: str) -> None:
    """Require the active exclusive owner for a state-volume allocation."""
    from control_plane.authz_candidate_preparation import (
        is_exclusive_product_lane_context,
        product_context_owners,
    )

    profile = store.read_product_profile_record(product)
    if profile.lifecycle_state != "active" or not is_exclusive_product_lane_context(
        context=context, product=product, owners=product_context_owners(store)
    ):
        raise StateVolumeRefusal(
            "application_state_volume_owner_changed",
            "Storage custody requires its current active exclusive owner.",
        )


def read_state_volume_progress(store: PostgresRecordStore, scope: str, plan_id: str) -> str:
    """Read allocation progress independently of the provider runner's attempts."""
    from control_plane.application_state_volume import PLAN_ROUTE

    row = store.read_idempotency_record(
        scope=scope, route_path=PLAN_ROUTE + "/progress", idempotency_key=plan_id
    )
    return _progress_phase(row)


def _progress_phase(row: LaunchplaneIdempotencyRecord | None) -> str:
    if row is None:
        return ""
    _progress_operation_key(row)
    phase = row.response_payload.get("phase")
    if (
        row.state != "completed"
        or not isinstance(phase, str)
        or (
            not phase.strip()
            and row.response_payload.get("allocation_state") != "retained_uncertain"
        )
    ):
        raise ValueError("Original allocation progress is unavailable.")
    return phase


def _progress_operation_key(row: LaunchplaneIdempotencyRecord | None) -> str:
    if row is None:
        return ""
    key = row.response_payload.get("operation_key")
    if row.state != "completed" or not isinstance(key, str) or not key.strip():
        raise ValueError("Original allocation operation key is unavailable.")
    return key


def state_volume_progress_operation_key(
    store: PostgresRecordStore, scope: str, plan_id: str
) -> str:
    """Return the only operation key allowed to continue this allocation."""
    from control_plane.application_state_volume import PLAN_ROUTE

    row = store.read_idempotency_record(
        scope=scope, route_path=PLAN_ROUTE + "/progress", idempotency_key=plan_id
    )
    return _progress_operation_key(row)


def state_volume_plan_is_retained(store: PostgresRecordStore, scope: str, plan_id: str) -> bool:
    """A retained allocation cannot be resumed under any operation key."""
    from control_plane.application_state_volume import PLAN_ROUTE

    row = store.read_idempotency_record(
        scope=scope, route_path=PLAN_ROUTE + "/progress", idempotency_key=plan_id
    )
    if row is None:
        return False
    _progress_phase(row)
    return row.response_payload.get("allocation_state") == "retained_uncertain"


def state_volume_custody_sha256(
    store: PostgresRecordStore, reservation: LaunchplaneIdempotencyRecord
) -> str:
    original = store.read_original_mutation_intent(reservation)
    plan_id = original.get("plan_id")
    if not isinstance(plan_id, str):
        raise ValueError("Original storage plan is unavailable.")
    return request_fingerprint(
        {
            "reservation": reservation.model_dump(mode="json"),
            "phase": read_state_volume_progress(store, reservation.scope, plan_id),
        }
    )


def checkpoint_state_volume_progress(
    store: PostgresRecordStore,
    *,
    reservation: LaunchplaneIdempotencyRecord,
    plan_id: str,
    phase: str,
) -> None:
    """Persist a phase only for its live, already-checkpointed allocation lease."""
    from control_plane.application_state_volume import PLAN_ROUTE, ROUTE, state_volume_target_key
    from control_plane.dokploy_target_setup_http import DokployTargetSetupEnvelope

    if reservation.state != "running" or reservation.route_path != ROUTE or not phase.strip():
        raise ValueError("Allocation progress requires its live setup reservation.")
    with store._session_factory() as session:
        store._begin_serialized_write(session)
        current = store.require_current_provider_execution(session, reservation)
        if (
            current.provider_effect_phase != phase
            or current.provider_effect_started_at != reservation.provider_effect_started_at
        ):
            raise ValueError("Allocation progress requires the current execution lease.")
        now = store._database_mutation_timestamp(session)
        original = store._original_mutation_intent(session, current, for_update=True)
        request = DokployTargetSetupEnvelope.model_validate(
            {**original.get("request", {}), "mode": "dry-run"}
        )
        if (
            original.get("plan_id") != plan_id
            or request.operation != "create-application"
            or request.instance != "testing"
            or not request.state_volume_path
            or current.provider_target_key != state_volume_target_key(request.context)
            or current.reconciliation_key != original.get("plan_sha256")
            or current.request_fingerprint != original.get("plan_sha256")
        ):
            raise ValueError("Progress requires an original state-volume allocation.")
        plan_row = session.scalar(
            store._idempotency_statement(
                scope=current.scope,
                route_path=PLAN_ROUTE,
                idempotency_key=plan_id,
                for_update=True,
            )
        )
        if plan_row is None or plan_row.payload.get("response_payload") != original:
            raise ValueError("Original storage plan is unavailable to this caller.")
        progress_row = session.scalar(
            store._idempotency_statement(
                scope=current.scope,
                route_path=PLAN_ROUTE + "/progress",
                idempotency_key=plan_id,
                for_update=True,
            )
        )
        if (
            progress_row is not None
            and _progress_operation_key(
                LaunchplaneIdempotencyRecord.model_validate(progress_row.payload)
            )
            != current.idempotency_key
        ):
            raise ValueError("Allocation progress belongs to its original operation key.")
        if (
            progress_row is not None
            and progress_row.payload.get("response_payload", {}).get("allocation_state")
            == "retained_uncertain"
        ):
            raise ValueError("A retained storage allocation cannot receive progress.")
        progress = LaunchplaneIdempotencyRecord(
            record_id="application-state-progress-" + plan_id,
            scope=current.scope,
            route_path=PLAN_ROUTE + "/progress",
            idempotency_key=plan_id,
            request_fingerprint=current.request_fingerprint,
            response_status_code=202,
            response_trace_id="application-state-progress",
            recorded_at=now,
            response_payload={"phase": phase, "operation_key": current.idempotency_key},
        )
        if progress_row is None:
            session.add(store._idempotency_row(progress))
        else:
            store._sync_idempotency_row(progress_row, progress)
        session.commit()


def retain_state_volume_allocation(
    store: PostgresRecordStore,
    *,
    reservation: LaunchplaneIdempotencyRecord,
    expected_custody_sha256: str,
    expected_original_intent: dict[str, Any],
    trace: str,
    checkpoint: Callable[[], None],
) -> dict[str, object]:
    """Fence an expired original allocation; caller holds the stable lane lock.

    The reviewed custody digest binds the reservation and durable allocation phase.
    Retention permits a separate fresh plan, never deletion or redispatch of
    this allocation. Its original intent and effect checkpoints remain intact.
    """
    from control_plane.application_state_volume import (
        PLAN_ROUTE,
        ROUTE,
        state_volume_target_key,
        state_volume_allocation_names,
        validate_volume_request,
    )
    from control_plane.dokploy_target_setup_http import DokployTargetSetupEnvelope

    if not trace.strip():
        raise ValueError("Review the exact original allocation custody before retention.")
    original = expected_original_intent
    request = DokployTargetSetupEnvelope.model_validate({**original["request"], "mode": "dry-run"})
    validate_volume_request(request)
    plan_id = original.get("plan_id")
    if not isinstance(plan_id, str):
        raise ValueError("Retention requires an original application state-volume allocation.")
    names = state_volume_allocation_names(plan_id)
    if (
        reservation.route_path != ROUTE
        or reservation.provider_target_key != state_volume_target_key(request.context)
        or reservation.reconciliation_key != original.get("plan_sha256")
        or reservation.request_fingerprint != original.get("plan_sha256")
        or any(original.get(name) != value for name, value in names.items())
    ):
        raise ValueError("Retention requires an original application state-volume allocation.")
    result: dict[str, object] = {
        "applied": False,
        "allocation_state": "retained_uncertain",
        "original_key": reservation.idempotency_key,
        "plan_id": plan_id,
        "deployment_performed": False,
        "resources_retained": True,
        "reviewed_custody_sha256": expected_custody_sha256,
    }
    with store._session_factory() as session:
        store._begin_serialized_write(session)
        store._lock_product_authority_bundle_write(session)
        row = session.scalar(
            store._idempotency_statement(
                scope=reservation.scope,
                route_path=reservation.route_path,
                idempotency_key=reservation.idempotency_key,
                for_update=True,
            )
        )
        if row is None:
            raise ValueError("Original allocation custody is unavailable.")
        current = LaunchplaneIdempotencyRecord.model_validate(row.payload)
        if store._original_mutation_intent(session, current, for_update=True) != original:
            raise ValueError("Original allocation intent changed; review retention again.")
        plan_row = session.scalar(
            store._idempotency_statement(
                scope=reservation.scope,
                route_path=PLAN_ROUTE,
                idempotency_key=plan_id,
                for_update=True,
            )
        )
        if plan_row is None or plan_row.payload.get("response_payload") != original:
            raise ValueError("Original storage plan is unavailable to this caller.")
        progress_row = session.scalar(
            store._idempotency_statement(
                scope=reservation.scope,
                route_path=PLAN_ROUTE + "/progress",
                idempotency_key=plan_id,
                for_update=True,
            )
        )
        progress_record = (
            LaunchplaneIdempotencyRecord.model_validate(progress_row.payload)
            if progress_row is not None
            else None
        )
        if progress_record is not None and _progress_operation_key(progress_record) != (
            reservation.idempotency_key
        ):
            raise ValueError("Allocation progress belongs to its original operation key.")
        phase = _progress_phase(progress_record)
        supplied_digest = request_fingerprint(
            {"reservation": reservation.model_dump(mode="json"), "phase": phase}
        )
        result["retained_provider_effect_phase"] = phase
        if current.state == "completed":
            # A lost retention response can replay against the original reviewed
            # snapshot, or against the resulting retained receipt. A successful
            # allocation is never eligible for this operation.
            completed_fields = {
                "state",
                "updated_at",
                "recorded_at",
                "response_status_code",
                "response_trace_id",
                "response_payload",
            }
            receipt = current.response_payload.get("result", {})
            if not isinstance(receipt, dict):
                raise ValueError("A completed allocation cannot be retained.")
            if receipt.get("allocation_state") != "retained_uncertain":
                raise StateVolumeRefusal(
                    "application_state_volume_already_adopted",
                    "Original allocation is completed; use its original-key receipt.",
                )
            reviewed_digest = receipt.get("reviewed_custody_sha256")
            if not isinstance(reviewed_digest, str) or (
                supplied_digest != expected_custody_sha256
                and not (reservation == current and expected_custody_sha256 == reviewed_digest)
            ):
                raise ValueError("Review the exact original allocation custody before retention.")
            result["reviewed_custody_sha256"] = reviewed_digest
            if (
                current.response_status_code != 202
                or current.response_payload
                != provider_operation_response_payload(
                    trace_id=current.response_trace_id, records={}, result=result
                )
                or current.model_dump(exclude=completed_fields)
                != reservation.model_dump(exclude=completed_fields)
            ):
                raise ValueError("A completed allocation cannot be retained.")
            checkpoint()
            return result
        if supplied_digest != expected_custody_sha256:
            raise StateVolumeRefusal(
                "application_state_volume_custody_changed",
                "Review the exact original allocation custody before retention.",
            )
        if current != reservation or current.state not in {"running", "reconcile_required"}:
            raise StateVolumeRefusal(
                "application_state_volume_custody_changed",
                "Original allocation custody changed; review retention again.",
            )
        now = store._database_mutation_timestamp(session)
        if not current.lease_expires_at or parse_launchplane_mutation_timestamp(
            current.lease_expires_at, field_name="lease_expires_at"
        ) > parse_launchplane_mutation_timestamp(now, field_name="observed_at"):
            raise StateVolumeRefusal(
                "application_state_volume_execution_active",
                "The original allocation execution lease is still active.",
            )
        retained = store._updated_idempotency_record(
            current,
            state="completed",
            updated_at=now,
            recorded_at=now,
            response_status_code=202,
            response_trace_id=trace.strip(),
            response_payload=provider_operation_response_payload(
                trace_id=trace.strip(), records={}, result=result
            ),
        )
        store._sync_idempotency_row(row, retained)
        retained_progress = LaunchplaneIdempotencyRecord(
            record_id="application-state-progress-" + plan_id,
            scope=current.scope,
            route_path=PLAN_ROUTE + "/progress",
            idempotency_key=plan_id,
            request_fingerprint=current.request_fingerprint,
            response_status_code=202,
            response_trace_id=trace.strip(),
            recorded_at=now,
            response_payload={
                "phase": phase,
                "operation_key": current.idempotency_key,
                "allocation_state": "retained_uncertain",
            },
        )
        if progress_row is None:
            session.add(store._idempotency_row(retained_progress))
        else:
            store._sync_idempotency_row(progress_row, retained_progress)
        checkpoint()
        session.commit()
    return result
