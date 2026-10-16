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
    profile = store.read_product_profile_record(record.product)
    lanes = {
        (lane.context, lane.instance)
        for lane in profile.lanes
        if lane.instance in {"prod", "testing"}
    }
    if any((row.context, row.instance) not in lanes for row in rows):
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
    for entry in record.writer_custody:
        retained = entry["provider"]
        targets.add((retained["context"], retained["instance"], retained["target_id"]))
        keys.add(
            generic_web_provider_target_key(
                provider_id=retained["provider_id"],
                context=retained["context"],
                instance=retained["instance"],
                target_type=retained.get("provider_target_type") or retained["target_category"],
                target_id=retained["target_id"],
            )
        )
    consumers = [
        *record.activation["writer_bindings"],
        *(
            row.model_dump(mode="json")
            for row in store.list_secret_bindings()
            if row.secret_id == record.activation["writer_secret_id"]
        ),
    ]
    for binding in consumers:
        try:
            target = store.read_provider_target_record(
                context_name=binding["context"], instance_name=binding["instance"]
            )
        except FileNotFoundError:
            continue
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
    from control_plane.production_recipient_writer_control import WRITER_ROUTE

    if reservation.route_path == WRITER_ROUTE.format(
        product=record.product
    ) and reservation.reconciliation_key.startswith(
        "production-recipient-writer:pause:" + record.plan_sha256 + ":"
    ):
        return False
    from control_plane.production_recipient_cutover import cutover_reconciliation_key
    from control_plane.production_recipient_inverse import inverse_reconciliation_key

    if reservation.reconciliation_key in {
        cutover_reconciliation_key(record.plan_sha256),
        inverse_reconciliation_key(record.plan_sha256),
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


def binding_identities(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (
            {key: value for key, value in row.items() if key not in {"created_at", "updated_at"}}
            for row in rows
        ),
        key=lambda row: row["binding_id"],
    )


def require_budget_admission(
    store: PostgresRecordStore, session: Any, record: ProductionRecipientRecord
) -> None:
    if not record.activation:
        raise ValueError("A reviewed API budget is required before bootstrap.")
    if binding_identities(writer_bindings(store, record)) != binding_identities(
        record.activation["writer_bindings"]
    ):
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
