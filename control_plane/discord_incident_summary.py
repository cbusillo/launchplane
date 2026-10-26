from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
import json
from typing import TYPE_CHECKING

from control_plane import secrets
from control_plane.contracts.outbox_delivery import (
    OutboxDeliveryRecord,
    build_outbox_dedupe_key,
    build_outbox_delivery_id,
)
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressIncidentRecord,
    PublicIngressIncidentEvent,
    PublicIngressNotificationPolicyRecord,
    RETIRED_PRODUCTION_DELIVERY_LAG_CHECK,
)
from control_plane.notifications import (
    DiscordNotificationError,
    DiscordNotificationNotSentError,
    DiscordNotificationSuperseded,
    public_discord_url_error,
)

if TYPE_CHECKING:
    from control_plane.workflows.public_ingress_monitor import (
        PublicIngressMonitorStore,
        PublicIngressNotificationDriverSet,
        PublicIngressNotificationOutboxStore,
    )

SUMMARY_AGGREGATE_TYPE = "public_ingress_discord_summary"
SUMMARY_WINDOW_SECONDS = 300


def discord_incident_needs_summary(
    incident: PublicIngressIncidentRecord, event: PublicIngressIncidentEvent
) -> bool:
    return incident.severity == "warning" or event == "reminder"


def summary_incidents(
    record_store: PublicIngressMonitorStore, policy: PublicIngressNotificationPolicyRecord
) -> tuple[PublicIngressIncidentRecord, ...]:
    return tuple(
        sorted(
            (
                value
                for value in record_store.list_public_ingress_incident_records(status="open")
                if value.notification_state == "active"
                and policy.matches(value)
                and value.check_name != RETIRED_PRODUCTION_DELIVERY_LAG_CHECK
            ),
            key=lambda value: value.incident_id,
        )
    )


def summary_states(incidents: tuple[PublicIngressIncidentRecord, ...]) -> list[list[str]]:
    return [[value.incident_id, value.material_fingerprint_sha256] for value in incidents]


def summary_covers(previous: OutboxDeliveryRecord | None, current: OutboxDeliveryRecord) -> bool:
    if previous is None:
        return False
    if int(str(previous.payload["send_window"])) >= int(str(current.payload["send_window"])):
        return True
    if previous.payload.get("webhook_reference") != current.payload.get("webhook_reference"):
        return False
    states = current.payload.get("incident_states")
    covered = previous.payload.get("incident_states")
    return (
        isinstance(states, list)
        and bool(states)
        and isinstance(covered, list)
        and all(state in covered for state in states)
    )


def build_discord_incident_summary_delivery(
    *,
    policy_id: str,
    destination_id: str,
    created_at: str,
    due_now: bool = False,
) -> OutboxDeliveryRecord:
    window = int(
        datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
    ) // SUMMARY_WINDOW_SECONDS + int(not due_now)
    ready_at = (
        datetime.fromtimestamp(window * SUMMARY_WINDOW_SECONDS, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    key = build_outbox_dedupe_key(
        kind="public_ingress_notification",
        parts=(SUMMARY_AGGREGATE_TYPE, json.dumps([policy_id, destination_id, window])),
    )
    return OutboxDeliveryRecord(
        delivery_id=build_outbox_delivery_id(kind="public_ingress_notification", dedupe_key=key),
        kind="public_ingress_notification",
        aggregate_type=SUMMARY_AGGREGATE_TYPE,
        aggregate_id=policy_id,
        dedupe_key=key,
        created_at=created_at,
        updated_at=created_at,
        next_attempt_at=ready_at,
        payload={
            "policy_id": policy_id,
            "destination_id": destination_id,
            "send_window": window,
        },
    )


def deliver_discord_incident_summary(
    *,
    record: OutboxDeliveryRecord,
    record_store: PublicIngressNotificationOutboxStore,
    drivers: PublicIngressNotificationDriverSet,
    mark_provider_started: Callable[[OutboxDeliveryRecord, str, str], None],
) -> OutboxDeliveryRecord:
    def result(
        state: str, action: str = "", error: str = "", started: bool = False
    ) -> OutboxDeliveryRecord:
        return record.model_copy(
            update={
                "state": state,
                "action": action,
                "error_code": error,
                "lease_owner": "",
                "lease_expires_at": "",
                "provider_operation_key": (record.provider_operation_key or record.delivery_id)
                if started
                else "",
                "provider_id": "discord" if started else "",
            }
        )

    if record.provider_operation_key:
        return result("failed", error="delivery_outcome_unknown", started=True)
    policy = next(
        (
            value
            for value in record_store.list_public_ingress_notification_policy_records(
                status="enabled"
            )
            if value.policy_id == record.aggregate_id and value.purpose == "incident_alerts"
        ),
        None,
    )
    destination = (
        next(
            (
                value
                for value in policy.destinations
                if value.destination_id == record.payload.get("destination_id")
                and value.kind == "discord"
                and value.status == "enabled"
            ),
            None,
        )
        if policy
        else None
    )
    if policy is None or destination is None:
        return result("delivered", action="skipped_disabled_summary")
    current = build_discord_incident_summary_delivery(
        policy_id=policy.policy_id,
        destination_id=destination.destination_id,
        created_at=record.updated_at,
        due_now=True,
    )
    if record.attempt == 1 and int(str(record.payload["send_window"])) < int(
        str(current.payload["send_window"])
    ):
        from control_plane.storage.postgres import OutboxWithIdempotencyRequest

        record_store.enqueue_outbox_delivery_with_idempotency(
            OutboxWithIdempotencyRequest(delivery=current)
        )
        return result("delivered", action="superseded_discord_summary")
    matching = summary_incidents(record_store, policy)
    if not matching:
        return result("delivered", action="skipped_cleared_summary")
    try:
        secret = record_store.read_secret_record(destination.discord_webhook_secret)
    except FileNotFoundError:
        return result("pending", error="summary_destination_unavailable")
    incidents = tuple(
        value
        for value in matching
        if secrets._scope_matches_record(
            secret, context_name=value.context, instance_name=value.instance
        )
    )
    if not incidents:
        return result("delivered", action="skipped_out_of_scope")
    record = record.model_copy(
        update={
            "payload": {
                **record.payload,
                "incident_states": summary_states(incidents),
                "webhook_reference": destination.discord_webhook_secret,
            }
        }
    )
    previous = record_store.read_latest_discord_summary(
        policy_id=policy.policy_id,
        destination_id=destination.destination_id,
        exclude_delivery_id=record.delivery_id,
    )
    if summary_covers(previous, record):
        return result("delivered", action="skipped_unchanged_summary")
    webhook = drivers.resolve_secret(destination.discord_webhook_secret, incidents[0])
    if secret.status != "configured" or not webhook:
        return result("pending", error="summary_destination_unavailable")
    if public_discord_url_error(webhook):
        return result("failed", error="summary_destination_invalid")
    groups = Counter(
        (
            value.product,
            "previews" if value.instance.startswith("preview-") else value.instance,
            value.check_kind,
            value.failure_code,
            value.severity,
        )
        for value in incidents
    )
    lines = [f"Launchplane: {len(incidents)} conditions remain open. Details are in Product Ops."]
    for (product, lane, kind, code, severity), count in sorted(groups.items()):
        line = f"- {product}/{lane}: {count} {severity} {kind} {code}"
        if sum(len(value) + 1 for value in lines) + len(line) > 1800:
            lines.append("Additional conditions are listed in Product Ops.")
            break
        lines.append(line)
    try:
        mark_provider_started(record, record.delivery_id, "discord")
    except DiscordNotificationSuperseded as error:
        if error.refresh_required:
            return result("pending", error="summary_snapshot_changed")
        return result("delivered", action="skipped_unchanged_summary")
    try:
        drivers.discord_sender(
            webhook, {"content": "\n".join(lines), "allowed_mentions": {"parse": []}}
        )
    except DiscordNotificationNotSentError:
        return result("pending", error="discord_not_sent")
    except DiscordNotificationError as error:
        if error.status_code == 429:
            return result("pending", error="discord_rate_limited")
        if 400 <= error.status_code < 500:
            return result("failed", error="discord_send_rejected")
        return result("failed", error="delivery_outcome_unknown", started=True)
    except Exception:
        return result("failed", error="delivery_outcome_unknown", started=True)
    return result("delivered", action="posted_discord_summary")
