"""A temporary single-writer API budget spanning a recipient and its lane peers."""

from typing import Any
import json

from sqlalchemy import select

from control_plane.contracts.production_recipient import ProductionRecipientRecord
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.storage.postgres import (
    PostgresRecordStore,
    LaunchplaneProductionRecipientRow,
    LaunchplaneIdempotencyRow,
)
from control_plane.workflows.generic_web_deploy_provider import generic_web_provider_target_key


def writer_bindings(
    store: PostgresRecordStore, record: ProductionRecipientRecord
) -> list[dict[str, Any]]:
    rows = [
        row
        for row in store.list_secret_bindings()
        if row.secret_id == record.activation["writer_secret_id"]
    ]
    if any(
        row.context != record.context or row.instance not in {"prod", "testing"} for row in rows
    ):
        raise ValueError(
            "API writer consumers outside the declared product lanes require coordination."
        )
    return sorted((row.model_dump(mode="json") for row in rows), key=lambda row: row["binding_id"])


def guarded_targets(
    store: PostgresRecordStore, record: ProductionRecipientRecord
) -> tuple[set[str], set[tuple[str, str, str]]]:
    keys = {
        "production-recipient:" + record.product + ":" + record.context,
        "testing-activation:" + record.product + ":" + record.context,
    }
    targets = {
        (record.context, "prod", record.target_id),
        (record.context, "prod", record.retained_target_id.target_id),
    }
    for target_id in (record.target_id, record.retained_target_id.target_id):
        if target_id:
            keys.add(
                generic_web_provider_target_key(
                    provider_id="dokploy",
                    context=record.context,
                    instance="prod",
                    target_type="compose",
                    target_id=target_id,
                )
            )
    for binding in writer_bindings(store, record):
        target = store.read_provider_target_record(
            context_name=binding["context"], instance_name=binding["instance"]
        )
        targets.add((binding["context"], binding["instance"], target.target_id))
        keys.add(
            generic_web_provider_target_key(
                provider_id=target.provider_id,
                context=target.context,
                instance=target.instance,
                target_type=target.provider_target_type or target.target_category,
                target_id=target.target_id,
            )
        )
    return keys, targets


def conflicts(
    store: PostgresRecordStore,
    record: ProductionRecipientRecord,
    reservation: LaunchplaneIdempotencyRecord,
) -> bool:
    if reservation.reconciliation_key in {
        "production-recipient-cutover:" + record.plan_sha256,
        "production-recipient-inverse:" + record.plan_sha256,
    }:
        return False
    keys, targets = guarded_targets(store, record)
    if reservation.provider_target_key in keys:
        return True
    # Service restarts bind the immutable container's target and lane in JSON.
    try:
        payload = json.loads(reservation.reconciliation_key)
    except (ValueError, TypeError):
        return False
    return (
        isinstance(payload, dict)
        and (payload.get("context"), payload.get("instance"), payload.get("target_id")) in targets
    )


def require_budget_admission(
    store: PostgresRecordStore, session: Any, record: ProductionRecipientRecord
) -> None:
    if not record.activation:
        raise ValueError("A reviewed API budget is required before bootstrap.")
    if writer_bindings(store, record) != record.activation["writer_bindings"]:
        raise ValueError("API writer consumers changed after review.")
    for row in session.scalars(
        select(LaunchplaneIdempotencyRow).where(LaunchplaneIdempotencyRow.state != "completed")
    ).all():
        reservation = LaunchplaneIdempotencyRecord.model_validate(row.payload)
        if conflicts(store, record, reservation):
            raise ValueError("A peer writer operation is still active or uncertain.")


def require_budget_checkpoint(
    store: PostgresRecordStore, session: Any, reservation: LaunchplaneIdempotencyRecord
) -> None:
    for row in session.scalars(select(LaunchplaneProductionRecipientRow)).all():
        record = ProductionRecipientRecord.model_validate(row.payload)
        if (
            record.phase in {"bootstrapping", "reverting", "inverse_published"}
            and record.activation
            and conflicts(store, record, reservation)
        ):
            raise ValueError(
                "The recipient release holds this API writer budget; resume after it settles."
            )
