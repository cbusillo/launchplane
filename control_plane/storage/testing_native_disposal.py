"""Keep policy and lane authority stable through the bounded host program send."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    parse_launchplane_mutation_timestamp,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.lane_usage import VERIFIED_DOWN_USAGE_EVIDENCE_PREFIX
from control_plane.odoo_testing_drill import (
    DRILL_ROUTE,
    DRILL_SCOPE,
    DrillPrerequisite,
    require_drill_use_unprotected,
)
from control_plane.storage.postgres import (
    LaunchplaneDokployTargetIdRow,
    LaunchplaneOdooStableTargetReplacementOperationRow,
    LaunchplaneProductProfileRow,
    LaunchplaneRouteBindingRow,
    PostgresRecordStore,
)


def dispatch_native_disposal(
    store: PostgresRecordStore,
    operation: OdooStableTargetReplacementOperationRecord,
    identity: dict[str, Any],
    expected_publication: str,
    reservation: LaunchplaneIdempotencyRecord,
    send: Callable[[], None],
) -> None:
    """DB reads and one bounded websocket send only; remote waits stay outside."""
    with store._session_factory() as session:
        store._lock_odoo_stable_lane(
            session, product=operation.product, context=operation.context, instance="testing"
        )
        store._lock_product_authority_bundle_write(session)
        worker = session.get(
            LaunchplaneOdooStableTargetReplacementOperationRow,
            operation.operation_id,
            with_for_update=True,
        )
        now = datetime.now(UTC)
        if (
            worker is None
            or worker.status != "running"
            or worker.attempt != operation.attempt
            or worker.lease_owner != operation.lease_owner
            or parse_launchplane_mutation_timestamp(
                worker.lease_expires_at, field_name="lease_expires_at"
            )
            <= now
        ):
            raise DrillPrerequisite("drill_worker_lease_changed")
        effect = session.scalar(
            store._idempotency_statement(
                scope=reservation.scope,
                route_path=reservation.route_path,
                idempotency_key=reservation.idempotency_key,
                for_update=True,
            )
        )
        current = (
            LaunchplaneIdempotencyRecord.model_validate(effect.payload)
            if effect is not None
            else None
        )
        if (
            current is None
            or current.state != "running"
            or not store._mutation_reservation_matches(current, reservation)
            or parse_launchplane_mutation_timestamp(
                current.lease_expires_at, field_name="lease_expires_at"
            )
            <= now
        ):
            raise DrillPrerequisite("native_testing_copy_disposal_lease_changed")
        profile_row = session.get(
            LaunchplaneProductProfileRow, operation.product, with_for_update=True
        )
        profile = (
            LaunchplaneProductProfileRecord.model_validate(profile_row.payload)
            if profile_row
            else None
        )
        lane = (
            next(
                (
                    lane
                    for lane in profile.lanes
                    if (lane.context, lane.instance) == (operation.context, "testing")
                ),
                None,
            )
            if profile is not None
            else None
        )
        budget = lane.odoo_testing_drill_storage if lane is not None else None
        if (
            profile is None
            or not profile.is_active
            or lane is None
            or not lane.odoo_testing_drill_enabled
            or lane.odoo_data_policy is None
            or lane.odoo_data_policy.data_authority != "resettable"
            or budget is None
            or identity["namespace"] in budget.retained_candidate_namespaces
            or identity["copy_binding"] in budget.retained_copy_bindings
        ):
            raise DrillPrerequisite("native_testing_copy_protected")
        if (
            session.scalar(
                select(LaunchplaneDokployTargetIdRow).where(
                    LaunchplaneDokployTargetIdRow.target_id == identity["target_id"]
                )
            )
            is not None
        ):
            raise DrillPrerequisite("native_testing_copy_has_serving_alias")
        binding = session.get(
            LaunchplaneRouteBindingRow,
            (operation.product, operation.context, "testing"),
            with_for_update=True,
        )
        if (
            binding is None
            or binding.payload.get("source", {})
            .get("source_versions", {})
            .get("testing_publication")
            != expected_publication
        ):
            raise DrillPrerequisite("native_testing_copy_publication_changed")
        slot = session.scalar(
            store._idempotency_statement(
                scope=DRILL_SCOPE,
                route_path=DRILL_ROUTE,
                idempotency_key=operation.idempotency_key,
                for_update=True,
            )
        )
        payload = slot.payload.get("response_payload", {}) if slot is not None else {}
        admitted = payload.get("activation_safety", {})
        if payload.get("activation_attempt") != operation.attempt:
            raise DrillPrerequisite("native_testing_copy_admission_changed")
        accepted = (
            str(payload.get("activation_usage_revision", ""))
            if admitted.get("usage") == "unknown"
            and str(admitted.get("usage_evidence", "")).startswith(
                VERIFIED_DOWN_USAGE_EVIDENCE_PREFIX
            )
            else ""
        )
        outer = getattr(store._disposable_authority_context, "session", None)
        store._disposable_authority_context.session = session
        try:
            require_drill_use_unprotected(
                store,
                operation.product,
                operation.context,
                now=now.timestamp(),
                accepted_usage_revision=accepted,
            )
            send()
        finally:
            store._disposable_authority_context.session = outer
