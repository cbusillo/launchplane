from collections import Counter
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.outbox_delivery import (
    OutboxDeliveryRecord,
    RETIRED_DISCORD_SUMMARY_AGGREGATE_TYPE,
)
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressIncidentRecord,
    PublicIngressNotificationPolicyRecord,
)


class OutboxInspectionStore(Protocol):
    def list_outbox_delivery_records(
        self, *, states: tuple[str, ...] = (), limit: int | None = None, newest_first: bool = False
    ) -> tuple[OutboxDeliveryRecord, ...]: ...

    def list_public_ingress_notification_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[PublicIngressNotificationPolicyRecord, ...]: ...

    def read_public_ingress_incident_record(
        self, incident_id: str
    ) -> PublicIngressIncidentRecord: ...


class OutboxInspection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    complete: bool
    counts: dict[str, int]
    deliveries: tuple[dict[str, object], ...]
    notification_policies: tuple[dict[str, object], ...]


def _delivery_routing(
    store: OutboxInspectionStore,
    record: OutboxDeliveryRecord,
    incident_statuses: dict[str, str],
) -> dict[str, object]:
    routing_keys = {
        "repository",
        "workflow_id",
        "ref",
        "product",
        "context",
        "instance",
        "policy_id",
        "destination_id",
        "alert",
        "credential_context",
    }
    routing = {key: value for key, value in record.payload.items() if key in routing_keys}
    if record.kind == "github_workflow_dispatch":
        routing["inputs"] = record.payload.get("inputs", {})
    if record.kind == "public_ingress_notification":
        if record.aggregate_type == RETIRED_DISCORD_SUMMARY_AGGREGATE_TYPE:
            return {**routing, "send_window": record.payload.get("send_window")}
        attempt = record.payload.get("attempt")
        if isinstance(attempt, dict):
            routing.update(
                (key, value)
                for key, value in attempt.items()
                if key in {"policy_id", "incident_id", "event"}
            )
        routing["existing_issue_url"] = record.payload.get("existing_issue_url", "")
        if record.aggregate_id not in incident_statuses:
            try:
                incident = store.read_public_ingress_incident_record(record.aggregate_id)
            except FileNotFoundError:
                incident_statuses[record.aggregate_id] = "unknown"
            else:
                incident_statuses[record.aggregate_id] = incident.status
        routing["incident_status"] = incident_statuses[record.aggregate_id]
    return routing


def inspect_outbox(store: OutboxInspectionStore, *, limit: int) -> OutboxInspection:
    records = store.list_outbox_delivery_records(
        states=("pending", "running", "reconcile_required"), limit=limit + 1
    )
    failures = store.list_outbox_delivery_records(
        states=("failed",), limit=limit + 1, newest_first=True
    )
    selected = (*records[:limit], *failures[:limit])
    policies = store.list_public_ingress_notification_policy_records(
        status="enabled", limit=limit + 1
    )
    destination_keys = {
        "destination_id",
        "kind",
        "status",
        "email_to",
        "github_repository",
        "github_issue_number",
        "discord_webhook_secret",
    }
    deliveries: list[dict[str, object]] = []
    incident_statuses: dict[str, str] = {}
    for record in selected:
        destination = record.payload.get("destination")
        deliveries.append(
            {
                "delivery_id": record.delivery_id,
                "kind": record.kind,
                "state": record.state,
                "aggregate_type": record.aggregate_type,
                "aggregate_id": record.aggregate_id,
                "created_at": record.created_at,
                "attempt": record.attempt,
                "provider_started": bool(record.provider_operation_key),
                "error_code": record.error_code,
                "routing": _delivery_routing(store, record, incident_statuses),
                "queued_destination": {
                    key: value for key, value in destination.items() if key in destination_keys
                }
                if isinstance(destination, dict)
                else {},
            }
        )
    return OutboxInspection(
        complete=len(records) <= limit and len(failures) <= limit and len(policies) <= limit,
        counts=dict(Counter(f"{record.kind}:{record.state}" for record in selected)),
        deliveries=tuple(deliveries),
        notification_policies=tuple(
            {
                "policy_id": policy.policy_id,
                "product": policy.product,
                "context": policy.context,
                "instance": policy.instance,
                "purpose": policy.purpose,
                "destinations": [
                    {
                        key: value
                        for key, value in destination.model_dump(mode="json").items()
                        if key in destination_keys
                    }
                    for destination in policy.destinations
                    if destination.status == "enabled"
                ],
            }
            for policy in policies[:limit]
        ),
    )
