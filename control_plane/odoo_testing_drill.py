"""Service-owned daily rebuilds; the normal Odoo worker owns every provider effect."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
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
    DisposableLaneStore,
    DisposableLaneSafetyUnknown,
    disposable_lane_binding_sha256,
    disposable_lane_configuration,
    require_disposable_lane,
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
    read_current_testing_generation,
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
_LOGGER = logging.getLogger(__name__)


class DrillSkipped(ValueError):
    """No provider effect: a person, custody, isolation or another operation protects the lane."""


class DrillPrerequisite(ValueError):
    """A fixed, value-free prerequisite code for the lane and its Discord alert."""


def timestamp(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")


def lane_key(product: str, context: str) -> str:
    return canonical_json_sha256([product, context, "testing"])


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
    """Called under the lane and authority locks; nothing here writes or calls a provider."""
    if read_staff_testing_hold(record_store=store, context=context) is not None:
        raise DrillSkipped("staff_testing_hold")
    try:
        require_disposable_lane(
            cast(DisposableLaneStore, store),
            product=product,
            context=context,
            instance="testing",
            now=datetime.fromtimestamp(now, timezone.utc),
        )
    except (DisposableLaneSafetyUnknown, FileNotFoundError) as error:
        safety = store.read_disposable_lane_safety(product, context, "testing")
        if safety is not None and (safety.usage == "in_use" or safety.data == "only_copy"):
            raise DrillSkipped("lane_use_or_only_copy_protected") from error
        raise DrillPrerequisite("usage_custody_or_isolation_unverified") from error
    profile = store.read_product_profile_record(product)
    if profile.driver_id != "odoo" or not profile.is_active:
        raise DrillSkipped("not_active_odoo")
    lane = next(
        lane for lane in profile.lanes if (lane.context, lane.instance) == (context, "testing")
    )
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
            "discard_drill_database": previous.drill_database,
            "discard_copy_binding": generation_binding(previous) if previous.drill_database else "",
            "prepared_at": prepared_at or timestamp(now),
            "completed_at": "",
            "reconcile_requested_at": prepared_at or timestamp(now),
            "request_sha256": canonical_json_sha256([key, database]),
            "source_binding_sha256": source_binding,
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
    # Preserve the fact sources and their original expiry; our own atomic database-name
    # change can rebind them, but cannot manufacture or renew an unused observation.
    safety = store.read_disposable_lane_safety(product, context, "testing")
    assert safety is not None
    configuration = tuple(
        runtime
        if isinstance(item, RuntimeEnvironmentRecord)
        and (item.scope, item.context, item.instance) == (runtime.scope, context, "testing")
        else item
        for item in disposable_lane_configuration(store, context, "testing")
    )
    safety = safety.model_copy(
        update={
            "binding_sha256": disposable_lane_binding_sha256(
                profile,
                target,
                store.read_dokploy_target_id_record(context_name=context, instance_name="testing"),
                configuration=configuration,
            )
        }
    )
    return PreparedDrill(previous, generation, runtime, safety, operation)


def require_drill_checkpoint(
    store: object, operation: OdooStableTargetReplacementOperationRecord
) -> None:
    if operation.idempotency_scope != DRILL_SCOPE:
        return
    if not isinstance(store, PostgresRecordStore) or operation.instance != "testing":
        raise DrillPrerequisite("testing_drill_storage_unavailable")
    receipt = store.read_idempotency_record(
        scope=DRILL_SCOPE, route_path=DRILL_ROUTE, idempotency_key=operation.idempotency_key
    )
    generation = read_current_testing_generation(
        store, product=operation.product, context=operation.context, instance="testing"
    )
    safety = store.read_disposable_lane_safety(operation.product, operation.context, "testing")
    if (
        receipt is None
        or receipt.response_payload.get("operation_id") != operation.operation_id
        or generation is None
        or receipt.response_payload.get("generation_binding") != generation_binding(generation)
    ):
        raise DrillPrerequisite("testing_drill_binding_changed")
    if (
        safety is None
        or safety.usage != "unused"
        or safety.data != "reproducible"
        or safety.production_resources != "isolated"
        or safety.expires_at <= datetime.now(timezone.utc)
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
        if latest is not None:
            key = str(latest.response_payload["key"])
            receipt = self._read(key)
            if receipt is not None and self._observe(profile.product, context, receipt, now):
                return
        key = f"{prefix}:{int(now // DRILL_INTERVAL_SECONDS)}"
        if self._read(key) is not None:
            return
        self.store.enqueue_odoo_testing_drill(profile.product, context, key, now)

    def _observe(
        self, product: str, context: str, receipt: LaunchplaneIdempotencyRecord, now: float
    ) -> bool:
        payload = dict(receipt.response_payload)
        if payload["status"] == "queued":
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
                else:
                    payload.update(status="fail", step="copy_completion_missing")
            elif operation.status == "cancelled":
                payload.update(status="skipped", step="staff_testing_hold")
            else:
                payload.update(
                    status="fail",
                    step=operation.error_code
                    or (
                        "completion_deadline"
                        if operation.status in {"pending", "running"}
                        else operation.phase
                    ),
                )
            self.store.write_idempotency_record(
                drill_receipt(receipt.idempotency_key, now, payload)
            )
        if (
            payload["status"] == "fail"
            and not payload.get("alert_delivered")
            and now >= float(str(payload.get("next_alert_at", 0)))
        ):
            payload["alert_delivered"] = self.notify(product, context, payload)
            payload["next_alert_at"] = now + DRILL_ALERT_RETRY_SECONDS
            self.store.write_idempotency_record(
                drill_receipt(receipt.idempotency_key, now, payload)
            )
        # Never replace an unfinished or held generation with a new snapshot/key.
        if payload.get("operation_id") and payload["status"] == "fail":
            operation = self.store.read_odoo_stable_target_replacement_operation_record(
                str(payload["operation_id"])
            )
            if operation.status in {"pending", "running", "reconciliation_required"}:
                return True
        return payload["status"] == "fail" and not payload.get("alert_delivered", False)

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
        for destination in destinations.values():
            try:
                sender = managed_notification_secret_value(
                    self.store, destination.discord_webhook_secret, context, "testing"
                )
                post_discord_webhook(
                    sender,
                    {
                        "allowed_mentions": {"parse": []},
                        "content": f"Odoo testing rebuild failed: {safe(product)} / testing. Step: {safe(payload.get('step'))}. Trace: {safe(payload.get('trace'))}. Inspect the retained drill operation; no automatic recapture.",
                    },
                )
            except Exception:
                delivered = False
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
    return OdooTestingDrillEvidence.model_validate(
        {
            "observed_at": receipt.recorded_at,
            **{
                key: value
                for key, value in receipt.response_payload.items()
                if key in OdooTestingDrillEvidence.model_fields and key != "observed_at"
            },
        }
    )
