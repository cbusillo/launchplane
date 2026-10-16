"""Service-owned daily rebuilds; the normal Odoo worker owns every provider effect."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import re
from threading import Event, Thread
import time
from typing import cast
from uuid import uuid4

from pydantic import JsonValue

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.disposable_lane import DisposableLaneSafety
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_mutation_reservation_id,
)
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.disposable_lanes import (
    disposable_lane_binding_sha256,
    disposable_lane_configuration,
)
from control_plane.launchplane_reconcile_authorization import (
    build_launchplane_reconcile_authorization,
)
from control_plane.odoo_target_replacement_apply_http import (
    _validate_target_replacement_apply_artifact_authority,
    build_odoo_target_replacement_apply_operation_record,
)
from control_plane.odoo_testing_restore import (
    FreshOdooTestingGeneration,
    generation_binding,
    generation_snapshot_matches,
    read_current_testing_generation,
)
from control_plane.odoo_testing_drill_usage import (
    read_drill_recent_signins,
    testing_drill_environment,
    DrillCopyAlreadyServing,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_lane_hold import read_staff_testing_hold
from control_plane.provider_operations import _ReservationHeartbeat
from control_plane.contracts.odoo_testing_drill import OdooTestingDrillEvidence

DRILL_INTERVAL_SECONDS = 24 * 60 * 60
DRILL_TIMEOUT_SECONDS = 2 * 60 * 60
DRILL_POLL_SECONDS = 30
DRILL_ALERT_RETRY_SECONDS = 300
DRILL_SCOPE = "launchplane:odoo-testing-drill:v1"
DRILL_ROUTE = "service:odoo-testing-drill"
DRILL_DEADLINE_STEP = "completion_deadline"
_LOGGER = logging.getLogger(__name__)


class DrillSkipped(ValueError):
    """Protected lane use or custody refuses the next dispatch."""


class DrillPrerequisite(ValueError):
    """A fixed, value-free prerequisite code for the lane and its Discord alert."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def timestamp(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")


def lane_key(product: str, context: str) -> str:
    return canonical_json_sha256([product, context, "testing"])


def drill_admission_revision(store: PostgresRecordStore, product: str, context: str) -> str:
    def optional[T](read: Callable[[], T]) -> T | None:
        try:
            return read()
        except FileNotFoundError:
            return None

    target = optional(
        lambda: store.read_dokploy_target_record(context_name=context, instance_name="testing")
    )
    target_id = optional(
        lambda: store.read_dokploy_target_id_record(context_name=context, instance_name="testing")
    )
    safety = store.read_disposable_lane_safety(product, context, "testing")
    generation = store.read_fresh_odoo_testing_generation(product, context)
    inventory = optional(
        lambda: store.read_environment_inventory(context_name=context, instance_name="testing")
    )
    return canonical_json_sha256(
        [
            disposable_lane_binding_sha256(
                store.read_product_profile_record(product),
                target,
                target_id,
                configuration=disposable_lane_configuration(store, context, "testing"),
            ),
            safety.model_dump(mode="json") if safety else None,
            generation.model_dump(mode="json") if generation else None,
            inventory.model_dump(mode="json") if inventory else None,
        ]
    )


def drill_receipt(
    key: str, now: float, payload: dict[str, JsonValue]
) -> LaunchplaneIdempotencyRecord:
    return LaunchplaneIdempotencyRecord(
        record_id=build_launchplane_mutation_reservation_id(
            scope=DRILL_SCOPE, route_path=DRILL_ROUTE, idempotency_key=key
        ),
        scope=DRILL_SCOPE,
        route_path=DRILL_ROUTE,
        idempotency_key=key,
        request_fingerprint=key,
        response_status_code=200,
        response_trace_id="testing-drill:" + key,
        recorded_at=timestamp(now),
        response_payload=payload,
    )


@dataclass(frozen=True)
class PreparedDrill:
    previous: FreshOdooTestingGeneration
    generation: FreshOdooTestingGeneration
    runtime: RuntimeEnvironmentRecord
    safety: DisposableLaneSafety
    operation: OdooStableTargetReplacementOperationRecord
    previous_database: str


def require_drill_use_unprotected(store: PostgresRecordStore, product: str, context: str) -> None:
    try:
        # Missing tracked authority is an unreadable hold check, never no hold.
        store.read_dokploy_target_record(context_name=context, instance_name="testing")
        hold = read_staff_testing_hold(record_store=store, context=context)
        recorded = store.read_disposable_lane_safety(product, context, "testing")
    except Exception as error:
        raise DrillSkipped("staff_hold_or_protection_check_unreadable") from error
    if hold is not None:
        raise DrillSkipped("staff_testing_hold")
    if recorded is not None and (recorded.usage == "in_use" or recorded.data == "only_copy"):
        raise DrillSkipped("lane_use_or_only_copy_protected")
    if recorded is not None and recorded.production_resources in {"shared", "separation_ready"}:
        raise DrillSkipped("testing_resources_protected")


def collect_drill_admission(
    store: PostgresRecordStore,
    generation: FreshOdooTestingGeneration,
    now: float,
    *,
    history_generation: FreshOdooTestingGeneration | None = None,
    history_database: str = "",
) -> DisposableLaneSafety:
    """Q347 evidence belongs to this drill, never the universal observation API."""
    require_drill_use_unprotected(store, generation.product, generation.context)
    current = read_current_testing_generation(
        store, product=generation.product, context=generation.context, instance="testing"
    )
    if not generation_snapshot_matches(current, generation):
        raise DrillSkipped("drill_admission_snapshot_changed")
    try:
        used = read_drill_recent_signins(
            store,
            generation,
            now,
            history_generation=history_generation,
            history_database=history_database,
        )
    except DrillCopyAlreadyServing as error:
        raise DrillSkipped("new_testing_copy_already_serving") from error
    except Exception as error:
        raise DrillSkipped("successful_login_check_unreadable") from error
    if used:
        raise DrillSkipped("recent_successful_signin")
    require_drill_use_unprotected(store, generation.product, generation.context)
    profile = store.read_product_profile_record(generation.product)
    target = store.read_dokploy_target_record(
        context_name=generation.context, instance_name="testing"
    )
    target_id = store.read_dokploy_target_id_record(
        context_name=generation.context, instance_name="testing"
    )
    binding = disposable_lane_binding_sha256(
        profile,
        target,
        target_id,
        configuration=disposable_lane_configuration(store, generation.context, "testing"),
    )
    reference = "service:odoo-testing-drill:" + generation_binding(generation)
    observed = datetime.fromtimestamp(now, timezone.utc)
    return DisposableLaneSafety(
        product=generation.product,
        context=generation.context,
        instance="testing",
        observed_by="service:odoo-testing-drill",
        observer_kind="director_agent",
        binding_sha256=binding,
        usage="unused",
        data="reproducible",
        production_resources="isolated",
        usage_evidence=reference,
        data_evidence=reference,
        isolation_evidence=reference,
        observed_at=observed,
        expires_at=observed + timedelta(minutes=5),
    )


def prepare_drill(
    store: PostgresRecordStore,
    product: str,
    context: str,
    key: str,
    now: float,
    *,
    database: str = "",
    prepared_at: str = "",
) -> PreparedDrill:
    """Build a read-only candidate; the caller revalidates it under authority locks."""
    require_drill_use_unprotected(store, product, context)
    profile = store.read_product_profile_record(product)
    if profile.driver_id != "odoo" or not profile.is_active:
        raise DrillSkipped("not_active_odoo")
    lane = next(
        lane for lane in profile.lanes if (lane.context, lane.instance) == (context, "testing")
    )
    if not lane.odoo_testing_drill_enabled:
        raise DrillSkipped("drill_disabled")
    production = [candidate for candidate in profile.lanes if candidate.instance == "prod"]
    if (
        len(production) != 1
        or not lane.odoo_data_policy.allows_rebuild_source("upstream_restore")
        or lane.odoo_data_policy.upstream_source != f"{product}:{production[0].context}:prod"
    ):
        raise DrillPrerequisite("same_product_production_source_required")
    previous = read_current_testing_generation(
        store, product=product, context=context, instance="testing"
    )
    if previous is None or not previous.completed_at:
        raise DrillPrerequisite("completed_qualified_testing_generation_required")
    safety = collect_drill_admission(store, previous, now)
    target = store.read_dokploy_target_record(context_name=context, instance_name="testing")
    if "ODOO_DB_NAME" in target.env:
        raise DrillPrerequisite("target_database_override_requires_migration")
    runtime = next(
        (
            item
            for item in store.list_runtime_environment_records(
                scope="instance", context_name=context, instance_name="testing"
            )
        ),
        None,
    )
    if runtime is None:
        raise DrillPrerequisite("testing_runtime_required")
    source_binding = ""
    if previous.source_mode == "container-export":
        from control_plane.odoo_testing_native_source import native_testing_source_authority

        source_binding = native_testing_source_authority(
            store, profile=profile, testing_context=context
        ).binding_sha256
    database = database or "lp_drill_" + uuid4().hex
    generation = previous.model_copy(
        update={
            "drill_database": database,
            "retained_drill_database": previous.drill_database,
            "retained_copy_binding": generation_binding(previous)
            if previous.drill_database
            else "",
            "discard_drill_database": previous.retained_drill_database,
            "discard_copy_binding": previous.retained_copy_binding,
            "prepared_at": prepared_at or timestamp(now),
            "completed_at": "",
            "reconcile_requested_at": prepared_at or timestamp(now),
            "request_sha256": canonical_json_sha256([key, database]),
            "source_binding_sha256": source_binding,
            "drill_storage_budget": lane.odoo_testing_drill_storage,
        }
    )
    runtime = runtime.model_copy(
        update={"env": {**runtime.env, "ODOO_DB_NAME": database}, "updated_at": timestamp(now)}
    )
    inventory = store.read_environment_inventory(context_name=context, instance_name="testing")
    if inventory.artifact_identity is None:
        raise DrillPrerequisite("testing_artifact_required")
    manifest = store.read_artifact_manifest(inventory.artifact_identity.artifact_id)
    request = OdooStableTargetReplacementApplyRequest(
        product=product,
        instance="testing",
        allow_empty_data=True,
        data_source_mode="upstream_restore",
        artifact_id=manifest.artifact_id,
        source_git_ref=manifest.source_commit,
        expected_current_artifact_id=manifest.artifact_id,
    )
    _validate_target_replacement_apply_artifact_authority(
        operation_store=store, replacement_request=request, context=context
    )
    operation = build_odoo_target_replacement_apply_operation_record(
        replacement_request=request,
        context=context,
        idempotency_key=key,
        idempotency_scope=DRILL_SCOPE,
        request_fingerprint=canonical_json_sha256(request.model_dump(mode="json")),
        created_at=timestamp(now),
        authorization=build_launchplane_reconcile_authorization(
            product=product, context=context, authorized_at=timestamp(now)
        ),
    )
    # The scoped receipt never rewrites or renews the general observation row.
    return PreparedDrill(
        previous,
        generation,
        runtime,
        safety,
        operation,
        testing_drill_environment(store, context).get("ODOO_DB_NAME", ""),
    )


def require_drill_checkpoint(
    store: object, operation: OdooStableTargetReplacementOperationRecord
) -> None:
    if operation.idempotency_scope != DRILL_SCOPE:
        return
    if not isinstance(store, PostgresRecordStore) or operation.instance != "testing":
        raise DrillPrerequisite("testing_drill_storage_unavailable")
    require_drill_use_unprotected(store, operation.product, operation.context)
    receipt = store.read_idempotency_record(
        scope=DRILL_SCOPE, route_path=DRILL_ROUTE, idempotency_key=operation.idempotency_key
    )
    generation = read_current_testing_generation(
        store, product=operation.product, context=operation.context, instance="testing"
    )
    if (
        receipt is None
        or receipt.response_payload.get("operation_id") != operation.operation_id
        or generation is None
        or receipt.response_payload.get("generation_binding") != generation_binding(generation)
    ):
        raise DrillPrerequisite("testing_drill_binding_changed")
    admitted = DisposableLaneSafety.model_validate(
        receipt.response_payload.get("activation_safety")
    )
    if (
        (admitted.product, admitted.context, admitted.instance)
        != (operation.product, operation.context, "testing")
        or admitted.usage != "unused"
        or admitted.data != "reproducible"
        or admitted.production_resources != "isolated"
        or receipt.response_payload.get("activation_attempt") != operation.attempt
    ):
        raise DrillSkipped("testing_drill_use_evidence_changed")


class OdooTestingDrillScheduler:
    def __init__(
        self,
        store: PostgresRecordStore,
        *,
        clock: Callable[[], float] = time.time,
        notify: Callable[[str, str, dict[str, JsonValue]], bool] | None = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.notify = notify or self._notify
        self.stop_event = Event()
        self.thread: Thread | None = None
        self.owner = uuid4().hex

    def start(self) -> None:
        self.stop_event.clear()
        self.thread = Thread(target=self._loop, name="odoo-testing-drill", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()
            self.thread = None

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                _LOGGER.exception("Odoo testing drill schedule pass unavailable")
            self.stop_event.wait(DRILL_POLL_SECONDS)

    def run_once(self) -> None:
        now = self.clock()
        for profile in self.store.list_product_profile_records(driver_id="odoo"):
            if self.stop_event.is_set():
                return
            if not profile.is_active:
                continue
            for lane in profile.lanes:
                if lane.instance != "testing":
                    continue
                try:
                    self._lane(profile, lane.context, now)
                except Exception:
                    _LOGGER.exception("Odoo testing drill lane pass unavailable")

    def _read(self, key: str) -> LaunchplaneIdempotencyRecord | None:
        return self.store.read_idempotency_record(
            scope=DRILL_SCOPE, route_path=DRILL_ROUTE, idempotency_key=key
        )

    def _lane(self, profile: LaunchplaneProductProfileRecord, context: str, now: float) -> None:
        claim = self.store.reserve_mutation(
            scope=DRILL_SCOPE,
            route_path=DRILL_ROUTE,
            idempotency_key="observer:" + lane_key(profile.product, context),
            request_fingerprint="observer",
            lease_owner=self.owner,
            lease_seconds=60,
        )
        if claim.status != "acquired":
            return
        heartbeat = _ReservationHeartbeat(
            store=self.store, reservation=claim.record, lease_seconds=60, interval_seconds=20
        )
        heartbeat.start()
        try:
            heartbeat.assert_current()
            self._advance_lane(profile, context, now)
        finally:
            reservation, _ = heartbeat.stop()
            self.store.release_mutation_reservation(reservation=reservation)

    def _advance_lane(
        self, profile: LaunchplaneProductProfileRecord, context: str, now: float
    ) -> None:
        prefix = lane_key(profile.product, context)
        latest = self._read(prefix)
        blocked = False
        if latest is not None:
            key = str(latest.response_payload["key"])
            receipt = self._read(key)
            if receipt is not None:
                blocked = self._observe(profile.product, context, receipt, now)
        self._deliver_alerts(profile.product, context, now)
        if blocked:
            return
        if not any(
            lane.context == context
            and lane.instance == "testing"
            and lane.odoo_testing_drill_enabled
            for lane in profile.lanes
        ):
            return
        # Spread recurring slots across the day without storing a second lane catalog.
        offset = int(prefix[:8], 16) % DRILL_INTERVAL_SECONDS
        key = f"{prefix}:{int((now - offset) // DRILL_INTERVAL_SECONDS)}"
        self.store.enqueue_odoo_testing_drill(profile.product, context, key, now)

    def _observe(
        self, product: str, context: str, receipt: LaunchplaneIdempotencyRecord, now: float
    ) -> bool:
        payload = dict(receipt.response_payload)
        if payload["status"] in {"queued", "fail"} and payload.get("operation_id"):
            operation = self.store.read_odoo_stable_target_replacement_operation_record(
                str(payload["operation_id"])
            )
            if (
                operation.status in {"pending", "running"}
                and now
                - datetime.fromisoformat(operation.created_at.replace("Z", "+00:00")).timestamp()
                < DRILL_TIMEOUT_SECONDS
            ):
                return True
            payload.update(
                step=operation.phase, trace=operation.runner_trace_id or operation.operation_id
            )
            if (
                operation.status == "pass"
                and operation.result is not None
                and operation.result.health_status == "pass"
            ):
                generation = self.store.read_fresh_odoo_testing_generation(product, context)
                if (
                    generation is not None
                    and generation.completed_at
                    and generation_binding(generation) == payload["generation_binding"]
                ):
                    payload.update(
                        status="pass", deployment_record_id=operation.result.deployment_record_id
                    )
                    if payload.get("failure_step") == DRILL_DEADLINE_STEP:
                        payload["step"] = "late_completion"
                else:
                    payload.update(status="fail", step="copy_completion_missing")
            elif operation.status == "cancelled":
                generation = self.store.read_fresh_odoo_testing_generation(product, context)
                activated = (
                    generation is not None
                    and generation_binding(generation) == payload["generation_binding"]
                )
                payload.update(
                    status="fail" if activated else "skipped",
                    step=(
                        "protected_use_after_activation"
                        if activated
                        else operation.cancellation.reason
                        if operation.cancellation is not None
                        and operation.cancellation.reason.replace("_", "").isalnum()
                        else "staff_testing_hold"
                    ),
                )
            else:
                payload.update(
                    status="fail",
                    step=operation.error_code
                    or (
                        DRILL_DEADLINE_STEP
                        if operation.status in {"pending", "running"}
                        else operation.phase
                    ),
                )
            if payload["status"] == "fail":
                if payload.get("failure_step") != payload["step"]:
                    payload.update(
                        failure_step=payload["step"],
                        failure_trace=payload["trace"],
                        alert_delivered=False,
                        delivered_destinations=[],
                        next_alert_at=0,
                    )
            if operation.result is not None:
                evidence = operation.result.post_deploy_override_evidence
                if (
                    evidence.get("post_deploy_readback_testing_drill_disposal_deferred") == "true"
                    and "disposal_status" not in payload
                ):
                    payload.update(disposal_status="deferred", disposal_pending=1)
                    if (
                        payload["status"] != "fail"
                        and payload.get("failure_step") != "testing_disposal_deferred"
                    ):
                        payload.update(
                            failure_step="testing_disposal_deferred",
                            failure_trace=payload["trace"],
                            alert_delivered=False,
                            delivered_destinations=[],
                            next_alert_at=0,
                        )
                elif (
                    evidence.get("post_deploy_readback_testing_drill_disposal_completed") == "true"
                ):
                    payload["disposal_status"] = "disposed"
            if payload != receipt.response_payload:
                payload["observed_at"] = timestamp(now)
                self.store.update_odoo_testing_drill_receipt(
                    receipt.idempotency_key,
                    now,
                    {
                        key: value
                        for key, value in payload.items()
                        if receipt.response_payload.get(key) != value
                    },
                )
        # Never replace an unfinished or held generation with a new snapshot/key.
        if payload.get("operation_id") and payload["status"] == "fail":
            operation = self.store.read_odoo_stable_target_replacement_operation_record(
                str(payload["operation_id"])
            )
            if operation.status in {"pending", "running", "reconciliation_required"}:
                return True
            generation = self.store.read_fresh_odoo_testing_generation(product, context)
            if generation is not None and not generation.completed_at:
                return True
        return False

    def _deliver_alerts(self, product: str, context: str, now: float) -> None:
        pending = self.store.list_pending_odoo_testing_drill_alerts(product, context)
        for receipt in pending[:-1]:
            self.store.update_odoo_testing_drill_receipt(
                receipt.idempotency_key, now, {"alert_superseded_by": pending[-1].idempotency_key}
            )
        for receipt in pending[-1:]:
            payload = dict(receipt.response_payload)
            if now < float(str(payload.get("next_alert_at", 0))):
                continue
            payload["alert_delivered"] = self.notify(product, context, payload)
            payload["next_alert_at"] = now + DRILL_ALERT_RETRY_SECONDS
            self.store.update_odoo_testing_drill_receipt(
                receipt.idempotency_key,
                now,
                {
                    key: payload[key]
                    for key in ("alert_delivered", "next_alert_at", "delivered_destinations")
                    if key in payload
                },
                expected_failure=(
                    receipt.response_payload.get(
                        "failure_step", receipt.response_payload.get("step")
                    ),
                    receipt.response_payload.get(
                        "failure_trace", receipt.response_payload.get("trace")
                    ),
                ),
            )

    def _notify(self, product: str, context: str, payload: dict[str, JsonValue]) -> bool:
        from control_plane.notifications import post_discord_webhook
        from control_plane.workflows.public_ingress_monitor import (
            managed_notification_secret_value,
        )

        # Use existing lane alert policy/managed secrets; no sender, person or token catalog.
        policies = self.store.list_public_ingress_notification_policy_records(
            product=product, context_name=context, instance_name="testing", status="enabled"
        )
        destinations = {
            destination.destination_id: destination
            for policy in policies
            for destination in policy.destinations
            if destination.kind == "discord" and destination.status == "enabled"
        }
        if not destinations:
            return False

        def safe(value: object) -> str:
            return (
                str(value) if re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", str(value)) else "unavailable"
            )

        delivered = True
        delivered_destinations = list(cast(list[str], payload.get("delivered_destinations", [])))
        for destination in destinations.values():
            destination_key = canonical_json_sha256(destination.model_dump(mode="json"))
            if destination_key in delivered_destinations:
                continue
            try:
                sender = managed_notification_secret_value(
                    self.store, destination.discord_webhook_secret, context, "testing"
                )
                post_discord_webhook(
                    sender,
                    {
                        "allowed_mentions": {"parse": []},
                        "content": f"Odoo testing {'storage cleanup needs attention' if payload.get('failure_step') in {'testing_disposal_deferred', 'testing_disposal_retained'} else 'rebuild failed'}: {safe(product)} / testing. Step: {safe(payload.get('failure_step', payload.get('step')))}. Trace: {safe(payload.get('failure_trace', payload.get('trace')))}. Inspect the retained drill operation; no automatic recapture.",
                    },
                )
                delivered_destinations.append(destination_key)
            except Exception:
                delivered = False
        payload["delivered_destinations"] = cast(list[JsonValue], delivered_destinations)
        return delivered


def testing_drill_evidence(
    store: object, product: str, context: str, instance: str
) -> OdooTestingDrillEvidence | None:
    if instance != "testing" or not isinstance(store, PostgresRecordStore):
        return None
    latest = store.read_idempotency_record(
        scope=DRILL_SCOPE, route_path=DRILL_ROUTE, idempotency_key=lane_key(product, context)
    )
    if latest is None:
        return None
    receipt = store.read_idempotency_record(
        scope=DRILL_SCOPE,
        route_path=DRILL_ROUTE,
        idempotency_key=str(latest.response_payload["key"]),
    )
    if receipt is None:
        return None
    payload = dict(receipt.response_payload)
    from control_plane._odoo_testing_storage_program import MAX_DISPOSALS_PER_PASS

    pending = store.list_odoo_testing_disposal_receipts(product, context)
    payload["disposal_pending"] = len(pending)
    payload["disposal_pending_bindings"] = [
        cast(dict[str, JsonValue], item.response_payload["planned_generation"])[
            "discard_copy_binding"
        ]
        for item in pending[:MAX_DISPOSALS_PER_PASS]
    ]
    payload["disposal_attention"] = len(
        store.list_odoo_testing_disposal_receipts(product, context, include_retained=True)
    ) - cast(int, payload["disposal_pending"])
    return OdooTestingDrillEvidence.model_validate(
        {
            "observed_at": receipt.response_payload.get("observed_at", receipt.recorded_at),
            **{
                key: value
                for key, value in payload.items()
                if key in OdooTestingDrillEvidence.model_fields and key != "observed_at"
            },
        }
    )
