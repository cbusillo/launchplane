from __future__ import annotations

from collections import Counter
from collections.abc import Callable
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
from control_plane.notifications import (
    DiscordNotificationError,
    DiscordNotificationNotSentError,
    public_discord_url_error,
)

if TYPE_CHECKING:
    from control_plane.workflows.public_ingress_monitor import (
        PublicIngressMonitorStore,
        PublicIngressNotificationDriverSet,
        PublicIngressNotificationOutboxStore,
    )

SUMMARY_AGGREGATE_TYPE = "public_ingress_discord_summary"


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
                if value.status == "open"
                and value.notification_state == "active"
                and policy.matches(value)
            ),
            key=lambda value: value.incident_id,
        )
    )


def _summary_states(incidents: tuple[PublicIngressIncidentRecord, ...]) -> set[str]:
    return {
        hashlib.sha256(
            json.dumps(
                [
                    value.incident_id,
                    value.material_fingerprint_sha256,
                    value.failure_code,
                    value.severity,
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        for value in incidents
    }


def _delivered_summary_states(
    record_store: PublicIngressMonitorStore, policy_id: str, destination_id: str
) -> set[str]:
    read = getattr(record_store, "list_outbox_delivery_records", None)
    if not callable(read):
        return set()
    return {
        str(state)
        for record in read(
            states=("delivered",),
            kind="public_ingress_notification",
            aggregate_type=SUMMARY_AGGREGATE_TYPE,
            aggregate_id=policy_id,
        )
        if record.action == "posted_discord_summary"
        and record.payload.get("destination_id") == destination_id
        for state in record.payload.get("summary_states", [])
    }


def build_discord_incident_summary_delivery(
    *,
    record_store: PublicIngressMonitorStore,
    policy: PublicIngressNotificationPolicyRecord,
    destination: PublicIngressNotificationDestination,
    incidents: tuple[PublicIngressIncidentRecord, ...],
    created_at: str,
) -> OutboxDeliveryRecord | None:
    states = _summary_states(incidents)
    if not states or states <= _delivered_summary_states(
        record_store, policy.policy_id, destination.destination_id
    ):
        return None
    digest = hashlib.sha256(json.dumps(sorted(states)).encode()).hexdigest()
    read_secret = getattr(record_store, "read_secret_record", None)
    version = ""
    if callable(read_secret):
        try:
            version = read_secret(destination.discord_webhook_secret).current_version_id
        except FileNotFoundError:
            pass
    routing = hashlib.sha256(
        json.dumps(
            [policy.updated_at, destination.model_dump(mode="json"), version], sort_keys=True
        ).encode()
    ).hexdigest()
    key = build_outbox_dedupe_key(
        kind="public_ingress_notification",
        parts=(
            SUMMARY_AGGREGATE_TYPE,
            policy.policy_id,
            destination.destination_id,
            routing,
            digest,
        ),
    )
    return OutboxDeliveryRecord(
        delivery_id=build_outbox_delivery_id(kind="public_ingress_notification", dedupe_key=key),
        kind="public_ingress_notification",
        aggregate_type=SUMMARY_AGGREGATE_TYPE,
        aggregate_id=policy.policy_id,
        dedupe_key=key,
        created_at=created_at,
        updated_at=created_at,
        next_attempt_at=created_at,
        payload={
            "policy_id": policy.policy_id,
            "destination_id": destination.destination_id,
            "summary_digest": digest,
            "summary_states": sorted(states),
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
    matching = summary_incidents(record_store, policy)
    if not matching:
        return result("delivered", action="skipped_cleared_summary")
    webhooks = [
        (value, drivers.resolve_secret(destination.discord_webhook_secret, value))
        for value in matching
    ]
    incidents = tuple(value for value, webhook in webhooks if webhook)
    if not incidents:
        return result("pending", error="summary_destination_unavailable")
    webhook = next(webhook for _, webhook in webhooks if webhook)
    if public_discord_url_error(webhook):
        return result("failed", error="summary_destination_invalid")
    current = build_discord_incident_summary_delivery(
        record_store=record_store,
        policy=policy,
        destination=destination,
        incidents=incidents,
        created_at=record.updated_at,
    )
    if current is None:
        return result("delivered", action="skipped_unchanged_summary")
    if current.delivery_id != record.delivery_id:
        from control_plane.storage.postgres import OutboxWithIdempotencyRequest

        record_store.enqueue_outbox_delivery_with_idempotency(
            OutboxWithIdempotencyRequest(delivery=current)
        )
        return result("delivered", action="superseded_discord_summary")
    if any(value and value != webhook for _, value in webhooks):
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
