from collections import Counter
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.outbox_delivery import OutboxDeliveryRecord
from control_plane.contracts.public_ingress_monitoring import PublicIngressNotificationPolicyRecord


class OutboxInspectionStore(Protocol):
    def list_outbox_delivery_records(
        self, *, states: tuple[str, ...] = (), limit: int | None = None
    ) -> tuple[OutboxDeliveryRecord, ...]: ...

    def list_public_ingress_notification_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[PublicIngressNotificationPolicyRecord, ...]: ...


class OutboxInspection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    complete: bool
    counts: dict[str, int]
    deliveries: tuple[dict[str, object], ...]
    notification_policies: tuple[dict[str, object], ...]


def inspect_outbox(store: OutboxInspectionStore, *, limit: int) -> OutboxInspection:
    records = store.list_outbox_delivery_records(
        states=("pending", "running", "reconcile_required"), limit=limit + 1
    )
    policies = store.list_public_ingress_notification_policy_records(
        status="enabled", limit=limit + 1
    )
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
    }
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
    for record in records[:limit]:
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
                "routing": {
                    key: value for key, value in record.payload.items() if key in routing_keys
                },
                "queued_destination": {
                    key: value for key, value in destination.items() if key in destination_keys
                }
                if isinstance(destination, dict)
                else {},
            }
        )
    return OutboxInspection(
        complete=len(records) <= limit and len(policies) <= limit,
        counts=dict(Counter(f"{record.kind}:{record.state}" for record in records[:limit])),
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
