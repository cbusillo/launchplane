from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
import hashlib
import json
from typing import TYPE_CHECKING

from control_plane.contracts.outbox_delivery import (
    OutboxDeliveryRecord,
    build_outbox_dedupe_key,
    build_outbox_delivery_id,
)
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressIncidentRecord,
    PublicIngressIncidentEvent,
    PublicIngressNotificationDestination,
    PublicIngressNotificationPolicyRecord,
)
from control_plane.notifications import DiscordNotificationError, public_discord_url_error

if TYPE_CHECKING:
    from control_plane.workflows.public_ingress_monitor import (
        PublicIngressMonitorStore,
        PublicIngressNotificationDriverSet,
        PublicIngressNotificationOutboxStore,
    )

SUMMARY_AGGREGATE_TYPE = "public_ingress_discord_summary"
SUMMARY_SETTLE_SECONDS = 300


def discord_incident_needs_summary(
    incident: PublicIngressIncidentRecord, event: PublicIngressIncidentEvent
) -> bool:
    return incident.severity == "warning" or event == "reminder"


def summary_incidents(
    record_store: PublicIngressMonitorStore,
    policy: PublicIngressNotificationPolicyRecord,
    replacements: tuple[PublicIngressIncidentRecord, ...] = (),
) -> tuple[PublicIngressIncidentRecord, ...]:
    incidents = {
        value.incident_id: value
        for value in record_store.list_public_ingress_incident_records(status="open")
    }
    incidents.update((value.incident_id, value) for value in replacements)
    return tuple(
        sorted(
            (
                value
                for value in incidents.values()
                if value.status == "open" and policy.matches(value)
            ),
            key=lambda value: value.incident_id,
        )
    )


def build_discord_incident_summary_delivery(
    *,
    record_store: PublicIngressMonitorStore,
    policy: PublicIngressNotificationPolicyRecord,
    destination: PublicIngressNotificationDestination,
    incidents: tuple[PublicIngressIncidentRecord, ...],
    created_at: str,
) -> OutboxDeliveryRecord | None:
    current = summary_incidents(record_store, policy, incidents)
    if not current:
        return None
    digest = hashlib.sha256(
        json.dumps(
            [
                (
                    value.incident_id,
                    value.material_fingerprint_sha256,
                    value.failure_code,
                    value.severity,
                )
                for value in current
            ],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    key = build_outbox_dedupe_key(
        kind="public_ingress_notification",
        parts=(SUMMARY_AGGREGATE_TYPE, policy.policy_id, destination.destination_id, digest),
    )
    ready_at = (
        (
            datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            + timedelta(seconds=SUMMARY_SETTLE_SECONDS)
        )
        .isoformat()
        .replace("+00:00", "Z")
    )
    return OutboxDeliveryRecord(
        delivery_id=build_outbox_delivery_id(kind="public_ingress_notification", dedupe_key=key),
        kind="public_ingress_notification",
        aggregate_type=SUMMARY_AGGREGATE_TYPE,
        aggregate_id=policy.policy_id,
        dedupe_key=key,
        created_at=created_at,
        updated_at=created_at,
        next_attempt_at=ready_at,
        payload={
            "policy_id": policy.policy_id,
            "destination_id": destination.destination_id,
            "summary_digest": digest,
        },
    )


def deliver_discord_incident_summary(
    *,
    record: OutboxDeliveryRecord,
    record_store: PublicIngressNotificationOutboxStore,
    drivers: PublicIngressNotificationDriverSet,
    mark_provider_started: Callable[[str, str], None],
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
                "provider_operation_key": record.delivery_id if started else "",
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
    incidents = summary_incidents(record_store, policy)
    current = build_discord_incident_summary_delivery(
        record_store=record_store,
        policy=policy,
        destination=destination,
        incidents=incidents,
        created_at=record.updated_at,
    )
    if current is None:
        return result("delivered", action="skipped_cleared_summary")
    if current.delivery_id != record.delivery_id:
        from control_plane.storage.postgres import OutboxWithIdempotencyRequest

        record_store.enqueue_outbox_delivery_with_idempotency(
            OutboxWithIdempotencyRequest(delivery=current)
        )
        return result("delivered", action="superseded_discord_summary")
    webhook = drivers._resolve_secret(destination.discord_webhook_secret, incidents[0])
    if not webhook:
        return result("pending", error="summary_destination_unavailable")
    if public_discord_url_error(webhook):
        return result("failed", error="summary_destination_invalid")
    if any(
        drivers._resolve_secret(destination.discord_webhook_secret, value) != webhook
        for value in incidents[1:]
    ):
        return result("failed", error="summary_destination_scope_mismatch")
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
    mark_provider_started(record.delivery_id, "discord")
    try:
        drivers.discord_sender(
            webhook, {"content": "\n".join(lines), "allowed_mentions": {"parse": []}}
        )
    except DiscordNotificationError as error:
        if error.status_code == 429:
            return result("pending", error="discord_rate_limited")
        if 400 <= error.status_code < 500:
            return result("failed", error="discord_send_rejected")
        return result("failed", error="delivery_outcome_unknown", started=True)
    except Exception:
        return result("failed", error="delivery_outcome_unknown", started=True)
    return result("delivered", action="posted_discord_summary")
