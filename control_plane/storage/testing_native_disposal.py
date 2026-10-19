"""Keep policy and lane authority stable through the bounded host program send."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.dokploy_target_record import DokployTargetRecord

from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_mutation_reservation_id,
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
    LaunchplaneDokployTargetRow,
    LaunchplaneIdempotencyRow,
    LaunchplaneOdooStableTargetReplacementOperationRow,
    LaunchplaneProductProfileRow,
    LaunchplaneRouteBindingRow,
    PostgresRecordStore,
)


def _native_child(
    store: PostgresRecordStore,
    session: Any,
    expected: LaunchplaneIdempotencyRecord,
    *,
    running: bool,
) -> LaunchplaneIdempotencyRecord:
    row = session.scalar(
        store._idempotency_statement(
            scope=expected.scope,
            route_path=expected.route_path,
            idempotency_key=expected.idempotency_key,
            for_update=True,
        )
    )
    current = LaunchplaneIdempotencyRecord.model_validate(row.payload) if row else None
    if (
        current is None
        or not store._mutation_reservation_matches(
            current, expected.model_copy(update={"lease_expires_at": current.lease_expires_at})
        )
        or current.state != expected.state
        or current.provider_target_key != expected.provider_target_key
        or current.request_fingerprint != expected.request_fingerprint
    ):
        raise DrillPrerequisite("native_testing_copy_admission_changed")
    if running and (
        current.state != "running"
        or parse_launchplane_mutation_timestamp(
            current.lease_expires_at, field_name="lease_expires_at"
        )
        <= parse_launchplane_mutation_timestamp(
            store._database_mutation_timestamp(session), field_name="now"
        )
    ):
        raise DrillPrerequisite("native_testing_copy_admission_changed")
    return current


def _native_family(
    session: Any, child: LaunchplaneIdempotencyRecord, route: str
) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(LaunchplaneIdempotencyRow).where(
            LaunchplaneIdempotencyRow.scope == child.scope,
            LaunchplaneIdempotencyRow.route_path == route,
            LaunchplaneIdempotencyRow.payload["response_payload"]["effect_record_id"].as_string()
            == child.record_id,
        )
    ).all()
    values = [LaunchplaneIdempotencyRecord.model_validate(row.payload) for row in rows]
    if any(
        value.request_fingerprint != child.request_fingerprint
        or value.response_payload.get("provider_target_key") != child.provider_target_key
        for value in values
    ):
        raise DrillPrerequisite("native_testing_copy_admission_changed")
    return sorted((value.response_payload for value in values), key=lambda value: value["attempt"])


def native_admission_family(
    store: PostgresRecordStore, child: LaunchplaneIdempotencyRecord, route: str
) -> list[dict[str, Any]]:
    """Read every admitted invocation of this exact original effect, including prior attempts."""
    with store._session_factory() as session:
        _native_child(store, session, child, running=False)
        return _native_family(session, child, route)


def _native_closure(
    store: PostgresRecordStore, session: Any, child: LaunchplaneIdempotencyRecord
) -> LaunchplaneIdempotencyRecord | None:
    row = session.scalar(
        store._idempotency_statement(
            scope=child.scope,
            route_path=child.route_path + ":native-closure",
            idempotency_key=child.record_id,
        )
    )
    record = LaunchplaneIdempotencyRecord.model_validate(row.payload) if row else None
    if record is not None and record.request_fingerprint != child.request_fingerprint:
        raise DrillPrerequisite("native_testing_copy_admission_changed")
    return record


def native_closure(
    store: PostgresRecordStore, child: LaunchplaneIdempotencyRecord
) -> dict[str, Any] | None:
    with store._session_factory() as session:
        record = _native_closure(store, session, child)
        return record.response_payload if record else None


def _native_evidence(
    store: PostgresRecordStore,
    child: LaunchplaneIdempotencyRecord,
    route: str,
    key: str,
    payload: dict[str, Any],
    stamp: str,
) -> LaunchplaneIdempotencyRecord:
    return LaunchplaneIdempotencyRecord(
        record_id=build_launchplane_mutation_reservation_id(
            scope=child.scope, route_path=route, idempotency_key=key
        ),
        scope=child.scope,
        route_path=route,
        idempotency_key=key,
        request_fingerprint=child.request_fingerprint,
        created_at=stamp,
        updated_at=stamp,
        recorded_at=stamp,
        response_status_code=200,
        response_trace_id=child.record_id,
        response_payload=payload,
    )


def _native_custody_loss(
    store: PostgresRecordStore, session: Any, child: LaunchplaneIdempotencyRecord
) -> bool:
    return (
        session.scalar(
            store._idempotency_statement(
                scope=child.scope,
                route_path=child.route_path + ":native-custody-loss",
                idempotency_key=child.record_id,
            )
        )
        is not None
    )


def native_custody_lost(store: PostgresRecordStore, child: LaunchplaneIdempotencyRecord) -> bool:
    with store._session_factory() as session:
        return _native_custody_loss(store, session, child)


def note_native_custody_loss(
    store: PostgresRecordStore, child: LaunchplaneIdempotencyRecord, proof: dict[str, Any]
) -> None:
    with store._session_factory() as session:
        store._begin_serialized_write(session)
        row = session.scalar(
            store._idempotency_statement(
                scope=child.scope,
                route_path=child.route_path,
                idempotency_key=child.idempotency_key,
                for_update=True,
            )
        )
        current = LaunchplaneIdempotencyRecord.model_validate(row.payload) if row else None
        # Negative custody evidence belongs to the immutable original across
        # lease/attempt renewal. Never lose it merely because a worker took over.
        stable = (
            "record_id",
            "scope",
            "route_path",
            "idempotency_key",
            "request_fingerprint",
            "reconciliation_key",
            "provider_target_key",
            "created_at",
        )
        if current is None or any(
            getattr(current, name) != getattr(child, name) for name in stable
        ):
            raise DrillPrerequisite("native_testing_copy_admission_changed")
        child = current
        original = store._original_mutation_intent(session, child)
        if not original:
            raise DrillPrerequisite("native_testing_copy_admission_changed")
        if not _native_custody_loss(store, session, child):
            record = _native_evidence(
                store,
                child,
                child.route_path + ":native-custody-loss",
                child.record_id,
                {"original_intent_sha256": canonical_json_sha256(original), "proof": proof},
                store._database_mutation_timestamp(session),
            )
            session.add(store._idempotency_row(record))
            session.commit()


def register_native_admission(
    store: PostgresRecordStore,
    child: LaunchplaneIdempotencyRecord,
    route: str,
    family: list[dict[str, Any]],
    values: dict[str, Any],
) -> dict[str, Any]:
    """Serialize admission with reservation supersession and terminal closure."""
    with store._session_factory() as session:
        store._begin_serialized_write(session)
        _native_child(store, session, child, running=True)
        if (
            _native_custody_loss(store, session, child)
            or _native_closure(store, session, child) is not None
            or _native_family(session, child, route) != family
        ):
            raise DrillPrerequisite("native_testing_copy_admission_changed")
        existing = next((item for item in family if item["attempt"] == child.attempt), None)
        if existing is not None:
            return existing
        payload = {
            **values,
            "effect_record_id": child.record_id,
            "attempt": child.attempt,
            "provider_target_key": child.provider_target_key,
        }
        record = _native_evidence(
            store,
            child,
            route,
            child.idempotency_key + ":" + str(child.attempt),
            payload,
            store._database_mutation_timestamp(session),
        )
        session.add(store._idempotency_row(record))
        session.commit()
        return payload


def close_native_admissions(
    store: PostgresRecordStore,
    child: LaunchplaneIdempotencyRecord,
    route: str,
    family: list[dict[str, Any]],
    proof: dict[str, Any],
    *,
    rollback: bool = False,
) -> dict[str, Any]:
    """Commit a host-sealed no-stop disposition only against the complete original family."""
    if proof.get("status") != "preserved" or proof.get("no_stop_dispatched") is not True:
        raise DrillPrerequisite("native_testing_copy_retirement_exclusion_unproven")
    with store._session_factory() as session:
        store._begin_serialized_write(session)
        _native_child(store, session, child, running=False)
        if (
            _native_custody_loss(store, session, child)
            or _native_family(session, child, route) != family
        ):
            raise DrillPrerequisite("native_testing_copy_admission_changed")
        original = store._original_mutation_intent(session, child).get("native_retirement", {})
        if (
            not original
            or proof.get("identity_sha256") != original.get("identity_sha256")
            or any(item.get("namespace_sha256") != proof.get("namespace_sha256") for item in family)
        ):
            raise DrillPrerequisite("native_testing_copy_retirement_exclusion_unproven")
        prior = _native_closure(store, session, child)
        if prior is not None and prior.response_payload["proof"].get(
            "namespace_sha256"
        ) != proof.get("namespace_sha256"):
            raise DrillPrerequisite("native_testing_copy_retirement_exclusion_unproven")
        payload = {
            "family_sha256": canonical_json_sha256(
                [{key: str(value) for key, value in item.items()} for item in family]
            ),
            "proof": prior.response_payload["proof"] if prior else proof,
            "rollback_requested": rollback
            or bool(prior and prior.response_payload.get("rollback_requested")),
        }
        record = _native_evidence(
            store,
            child,
            child.route_path + ":native-closure",
            child.record_id,
            payload,
            store._database_mutation_timestamp(session),
        )
        if prior is not None:
            record = record.model_copy(update={"created_at": prior.created_at})
        session.merge(store._idempotency_row(record))
        session.commit()
        return payload


def require_native_send(
    store: PostgresRecordStore, session: Any, child: LaunchplaneIdempotencyRecord, route: str
) -> None:
    _native_child(store, session, child, running=True)
    if (
        _native_custody_loss(store, session, child)
        or _native_closure(store, session, child) is not None
        or not any(
            item["attempt"] == child.attempt for item in _native_family(session, child, route)
        )
    ):
        raise DrillPrerequisite("native_testing_copy_admission_changed")


def dispatch_testing_retirement(
    store: PostgresRecordStore,
    operation: OdooStableTargetReplacementOperationRecord,
    checkpoint_and_send: Callable[[], None],
) -> None:
    """Hold policy stable before generic retirement checkpoints through its bounded API call.

    PostgreSQL owns cross-connection exclusion. SQLite fixtures exercise the
    same refusal/order but cannot qualify nested-transaction policy locking.
    """
    if store.database_dialect_name != "postgresql":
        target = store.read_dokploy_target_record(
            context_name=operation.context, instance_name="testing"
        )
        if target.policies.staff_testing_hold is not None:
            raise DrillPrerequisite("staff_testing_hold")
        checkpoint_and_send()
        return
    with store._session_factory() as session:
        store._lock_odoo_stable_lane(
            session, product=operation.product, context=operation.context, instance="testing"
        )
        store._lock_product_authority_bundle_write(session)
        target = session.scalar(
            select(LaunchplaneDokployTargetRow)
            .where(
                LaunchplaneDokployTargetRow.context == operation.context,
                LaunchplaneDokployTargetRow.instance == "testing",
            )
            .with_for_update()
        )
        if (
            target is None
            or DokployTargetRecord.model_validate(target.payload).policies.staff_testing_hold
            is not None
        ):
            raise DrillPrerequisite("staff_testing_hold")
        # Existing checkpoint methods use their own child transactions. They
        # reuse this held authority guard and never reacquire its advisory lock.
        outer = getattr(store._disposable_authority_context, "session", None)
        store._disposable_authority_context.session = session
        try:
            checkpoint_and_send()
        finally:
            store._disposable_authority_context.session = outer


def dispatch_native_retirement(
    store: PostgresRecordStore,
    operation: OdooStableTargetReplacementOperationRecord,
    child: LaunchplaneIdempotencyRecord,
    route: str,
    send: Callable[[], None],
) -> None:
    """Fence the actual bounded retirement send against supported hold/worker writes."""
    with store._session_factory() as session:
        store._lock_odoo_stable_lane(
            session, product=operation.product, context=operation.context, instance="testing"
        )
        store._lock_product_authority_bundle_write(session)
        now = parse_launchplane_mutation_timestamp(
            store._database_mutation_timestamp(session), field_name="now"
        )
        if operation.lease_owner:
            worker = session.get(
                LaunchplaneOdooStableTargetReplacementOperationRow,
                operation.operation_id,
                with_for_update=True,
            )
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
                raise DrillPrerequisite("native_testing_copy_worker_lease_changed")
        target = session.scalar(
            select(LaunchplaneDokployTargetRow)
            .where(
                LaunchplaneDokployTargetRow.context == operation.context,
                LaunchplaneDokployTargetRow.instance == "testing",
            )
            .with_for_update()
        )
        if (
            target is None
            or DokployTargetRecord.model_validate(target.payload).policies.staff_testing_hold
            is not None
        ):
            raise DrillPrerequisite("staff_testing_hold")
        require_native_send(store, session, child, route)
        send()


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
        require_native_send(store, session, reservation, reservation.route_path + ":attempt")
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
