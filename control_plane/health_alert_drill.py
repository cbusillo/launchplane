"""Exercise existing alert routing/transport without persisting a health transition."""

from datetime import datetime, timezone
import hashlib
import json
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressIncidentRecord,
    PublicIngressNotificationDestination,
    PublicIngressObservationRecord,
    PublicIngressTargetObservation,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.public_ingress_monitor import (
    PublicIngressNotificationDriverSet,
    effective_health_check_monitor_target,
    public_ingress_notification_drivers,
)

DRILL_WINDOW_SECONDS = 600
DRILL_ROUTE = "/v1/health-alert-drills"
_SCOPE = "launchplane:health-alert-drill"
_FINGERPRINT = "health-alert-drill-v1"


class HealthAlertDrillWindowError(ValueError):
    """The caller's window cannot be read or sent."""


class HealthAlertDrillScope(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    product: str = Field(min_length=1, max_length=200)
    context: str = Field(min_length=1, max_length=200)
    instance: str = Field(min_length=1, max_length=200)
    check_name: str = Field(min_length=1, max_length=200)


class HealthAlertDrillRequest(HealthAlertDrillScope):
    mode: Literal["dry-run", "send"] = "dry-run"
    window: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def require_send_window(self) -> "HealthAlertDrillRequest":
        if self.mode == "send" and self.window is None:
            raise ValueError("Send requires the window from the routing dry run.")
        return self


class HealthAlertDrillReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_id: str
    scope: HealthAlertDrillScope
    attempted_at: str
    status: Literal["delivered", "failed", "unavailable"]
    delivered_count: int = 0
    failed_count: int = 0
    unavailable_count: int = 0


class HealthAlertDrillEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: HealthAlertDrillScope
    routing: Literal["enabled", "missing", "disabled", "unavailable", "unknown_check"]
    enabled_discord_destinations: int = 0
    window: int
    window_seconds: int = DRILL_WINDOW_SECONDS
    next_window_at: str
    rate_limited: bool = False
    receipt: HealthAlertDrillReceipt | None = None


def _timestamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


def _window_key(scope: HealthAlertDrillScope, window: int) -> str:
    # Every check and every caller in this lane shares the same drill budget.
    lane = json.dumps([scope.product, scope.context, scope.instance])
    return f"{hashlib.sha256(lane.encode()).hexdigest()}:{window}"


def _routing(
    store: PostgresRecordStore, scope: HealthAlertDrillScope, now: float
) -> tuple[
    Literal["enabled", "missing", "disabled", "unknown_check"],
    PublicIngressIncidentRecord | None,
    tuple[PublicIngressNotificationDestination, ...],
]:
    try:
        profile = store.read_product_profile_record(scope.product)
    except FileNotFoundError:
        return "unknown_check", None, ()
    lane = next(
        (
            lane
            for lane in profile.lanes
            if lane.context == scope.context and lane.instance == scope.instance
        ),
        None,
    )
    check = (
        next(
            (check for check in lane.health_monitoring.checks if check.name == scope.check_name),
            None,
        )
        if lane
        else None
    )
    if lane is None or check is None:
        return "unknown_check", None, ()
    target = effective_health_check_monitor_target(
        profile=profile, lane=lane, check=check, record_store=store
    )
    if target is None or not target.incident_eligible:
        return "disabled", None, ()
    # These are in-memory envelopes for the existing matcher and driver only.
    # Neither envelope ever enters observation/incident storage or the outbox.
    incident = PublicIngressIncidentRecord(
        incident_id="health-alert-drill",
        **scope.model_dump(),
        check_kind=check.kind,
        status="open",
        opened_at=_timestamp(now),
        opened_observation_id="health-alert-drill",
        latest_observation_id="health-alert-drill",
        latest_observed_at=_timestamp(now),
        failure_code="http_error",
        summary="DRILL — no production failure was observed.",
    )
    policies = tuple(
        policy
        for policy in store.list_public_ingress_notification_policy_records(
            product=scope.product, context_name=scope.context, instance_name=scope.instance
        )
        if policy.matches(incident)
    )
    discord = tuple(
        (policy, destination)
        for policy in policies
        for destination in policy.destinations
        if destination.kind == "discord"
    )
    enabled = tuple(
        destination
        for policy, destination in discord
        if policy.status == "enabled" and destination.status == "enabled"
    )
    return ("enabled" if enabled else "disabled" if discord else "missing"), incident, enabled


def health_alert_drill(
    store: PostgresRecordStore,
    *,
    scope: HealthAlertDrillScope,
    now: float,
    send: bool = False,
    window: int | None = None,
    drivers: PublicIngressNotificationDriverSet | None = None,
) -> HealthAlertDrillEvidence:
    current_window = int(now // DRILL_WINDOW_SECONDS)
    selected_window = current_window if window is None else window
    if not 0 <= selected_window <= current_window or (send and selected_window != current_window):
        raise HealthAlertDrillWindowError(
            "Read a current or past drill window; send only in the current window."
        )
    evidence = HealthAlertDrillEvidence(
        scope=scope,
        routing="unavailable",
        window=selected_window,
        next_window_at=_timestamp((selected_window + 1) * DRILL_WINDOW_SECONDS),
    )
    key = _window_key(scope, selected_window)
    record = store.read_idempotency_record(
        scope=_SCOPE, route_path=DRILL_ROUTE, idempotency_key=key
    )
    if record is not None:
        if record.request_fingerprint != _FINGERPRINT:
            return evidence
        receipt = HealthAlertDrillReceipt.model_validate(record.response_payload)
        evidence.rate_limited = selected_window == current_window
        if receipt.scope == scope:
            evidence.receipt = receipt
    try:
        routing, incident, destinations = _routing(store, scope, now)
    except Exception:  # A routing read failure must not cause a send or leak private details.
        return evidence
    evidence.routing = routing
    evidence.enabled_discord_destinations = len(destinations)
    if not send or evidence.rate_limited or routing != "enabled" or incident is None:
        return evidence
    receipt = HealthAlertDrillReceipt(
        receipt_id=f"health-alert-drill-{uuid4().hex}",
        scope=scope,
        attempted_at=_timestamp(now),
        status="unavailable",
        unavailable_count=len(destinations),
    )
    record = LaunchplaneIdempotencyRecord(
        record_id=receipt.receipt_id,
        scope=_SCOPE,
        route_path=DRILL_ROUTE,
        idempotency_key=key,
        request_fingerprint=_FINGERPRINT,
        recorded_at=_timestamp(now),
        response_status_code=200,
        response_trace_id=receipt.receipt_id,
        response_payload=receipt.model_dump(mode="json"),
    )
    # Commit the unknown outcome BEFORE network I/O. A crash/retry never resends.
    if not store.write_idempotency_record_once(record):
        return health_alert_drill(store, scope=scope, now=now, window=selected_window)
    observation = PublicIngressObservationRecord(
        record_id=receipt.receipt_id,
        **scope.model_dump(),
        check_kind=incident.check_kind,
        observed_at=_timestamp(now),
        status="fail",
        failure_code="http_error",
        targets=(
            PublicIngressTargetObservation(
                target="health_url",
                url="drill://synthetic",
                status="fail",
                failure_code="http_error",
                summary="DRILL — no endpoint was contacted.",
            ),
        ),
        summary="DRILL — synthetic envelope only; no health check was run.",
    )
    driver = drivers or public_ingress_notification_drivers(record_store=store)
    for destination in destinations:
        try:
            _, _, current_destinations = _routing(store, scope, now)
        except Exception:
            continue
        if destination not in current_destinations:
            continue
        delivery = driver.send(
            destination=destination,
            event="opened",
            incident=incident,
            observation=observation,
            notification_body=(
                f"DRILL — test alert only. No production failure was observed.\n"
                f"Product: {scope.product}\nLane: {scope.context}/{scope.instance}\n"
                f"Check: {scope.check_name}\nReceipt: {receipt.receipt_id}\n"
                "Exercises existing failure-alert routing and Discord transport. "
                "Production checks, health and incidents are unchanged."
            ),
            drill=True,
        )
        if delivery.action in {"missing_discord_webhook", "invalid_discord_webhook"}:
            continue
        receipt.unavailable_count -= 1
        if delivery.delivery_status == "delivered":
            receipt.delivered_count += 1
        else:
            receipt.failed_count += 1
    receipt.status = (
        "unavailable"
        if receipt.unavailable_count
        else "failed"
        if receipt.failed_count
        else "delivered"
    )
    # Do not retain destination ids, URLs, secret ids, or raw provider exceptions.
    store.write_idempotency_record(
        record.model_copy(update={"response_payload": receipt.model_dump(mode="json")})
    )
    evidence.receipt = receipt
    evidence.rate_limited = True
    return evidence
