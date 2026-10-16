"""Reviewed same-key custody supersession; old effects remain permanently retained."""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, or_

from control_plane.contracts.production_recipient import (
    ProductionRecipientRequest,
    ProductionRecipientRecord,
)
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    parse_launchplane_mutation_timestamp,
)
from control_plane.integration_allowances import canonical_sha256
from control_plane.production_recipient import build_recipient, custody, authority_snapshot
from control_plane.production_recipient_fence import RETIRE_EXECUTION_PROGRAM
from control_plane.production_recipient_host import run_recipient_program
from control_plane.storage.postgres import (
    PostgresRecordStore,
    LaunchplaneProductionRecipientRow,
    LaunchplaneIdempotencyRow,
)

from control_plane.production_recipient_evidence import EXECUTION_FENCE_MARKER

CONTINUATION_ROUTE = "/v1/products/{product}/production-recipient/continuation"


class RecipientContinuationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str = Field(min_length=1)
    original_key: str = Field(min_length=1)
    expected_custody_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    replacement: ProductionRecipientRequest
    mode: Literal["dry-run", "apply", "readback"] = "dry-run"
    reason: str = Field(min_length=1)
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")


class RecipientContinuationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    plan_sha256: str
    replacement_plan_sha256: str
    retained_namespace: str
    replacement_namespace: str
    retain_original_key: Literal[True] = True
    bootstrap_eligible: Literal[False] = False


def reservations(
    store: PostgresRecordStore, record: ProductionRecipientRecord
) -> list[LaunchplaneIdempotencyRecord]:
    with store._session_factory() as session:
        return sorted(
            [
                LaunchplaneIdempotencyRecord.model_validate(row.payload)
                for row in session.scalars(
                    select(LaunchplaneIdempotencyRow).where(
                        or_(
                            LaunchplaneIdempotencyRow.provider_target_key
                            == "production-recipient:" + record.product + ":" + record.context,
                            LaunchplaneIdempotencyRow.record_id.in_(
                                [
                                    row["record_id"]
                                    for row in record.writer_custody
                                    if row.get("record_id")
                                ]
                                + [
                                    value["record_id"]
                                    for key, value in record.execution.items()
                                    if key.startswith("reservation:")
                                ]
                                + (
                                    [record.execution["original_reservation"]["record_id"]]
                                    if record.execution.get("original_reservation")
                                    else []
                                )
                            ),
                        )
                    )
                ).all()
            ],
            key=lambda row: row.record_id,
        )


def continuation_plan(
    root: Path,
    store: PostgresRecordStore,
    product: str,
    request: RecipientContinuationRequest,
    actor: str,
    scope: str,
) -> tuple[ProductionRecipientRecord, ProductionRecipientRecord, dict[str, Any]]:
    current = custody(store, product, request.context)
    normalized = request.model_dump(mode="json", exclude={"mode", "reviewed_plan_sha256"})
    if (
        current is not None
        and (current.actor, current.scope, current.original_key)
        == (actor, scope, request.original_key)
        and current.continuation.get("request") == normalized
    ):
        previous = ProductionRecipientRecord.model_validate(
            {**current.prior_attempts[-1], "prior_attempts": current.prior_attempts[:-1]}
        )
        return previous, current, dict(current.continuation["result"])
    if (
        current is None
        or (current.actor, current.scope, current.original_key)
        != (actor, scope, request.original_key)
        or canonical_sha256(current.model_dump(mode="json")) != request.expected_custody_sha256
        or request.replacement.context != request.context
        or request.replacement.mode != "dry-run"
        or request.replacement.reviewed_plan_sha256
        or (current.publication and current.phase != "rolled_back")
        or (current.execution and current.phase not in {"rolled_back", "preflight_refused"})
    ):
        raise ValueError("Review exact original-key custody before continuation.")
    replacement = build_recipient(
        root,
        store,
        product,
        request.replacement,
        actor,
        scope,
        request.original_key,
        retained_supersession=True,
    )
    observed = reservations(store, current)
    result = {
        "product": product,
        "context": request.context,
        "plan_sha256": canonical_sha256(
            {
                "custody": current.model_dump(mode="json"),
                "replacement": replacement.model_dump(mode="json"),
                "reason": request.reason,
                "reservations": [r.model_dump(mode="json") for r in observed],
            }
        ),
        "replacement_plan_sha256": replacement.plan_sha256,
        "retained_namespace": current.namespace,
        "replacement_namespace": replacement.namespace,
        "retain_original_key": True,
        "bootstrap_eligible": False,
    }
    return current, replacement, result


def supersede_recipient(
    root: Path,
    store: PostgresRecordStore,
    product: str,
    request: RecipientContinuationRequest,
    actor: str,
    scope: str,
    checkpoint: Callable[[str], None],
) -> dict[str, Any]:
    current, replacement, result = continuation_plan(root, store, product, request, actor, scope)
    if result["plan_sha256"] != request.reviewed_plan_sha256:
        raise ValueError("Review the exact current continuation.")
    if replacement.continuation.get("reviewed_plan_sha256") == request.reviewed_plan_sha256:
        return result
    observed = reservations(store, current)
    with store._session_factory() as admission:
        now = store._database_mutation_timestamp(admission)
    if any(
        row.state != "completed"
        and parse_launchplane_mutation_timestamp(
            row.lease_expires_at, field_name="lease_expires_at"
        )
        > parse_launchplane_mutation_timestamp(now, field_name="observed_at")
        for row in observed
    ):
        raise ValueError("The original execution lease is still active.")
    from control_plane.production_recipient_writer_control import retained_writer_receipt

    writer_entries = {
        entry["record_id"]: entry for entry in current.writer_custody if entry.get("record_id")
    }
    writer_receipts = {}
    for reservation in observed:
        if reservation.state != "completed" and reservation.record_id in writer_entries:
            writer_receipts[reservation.record_id] = retained_writer_receipt(
                root, current, writer_entries[reservation.record_id], checkpoint
            )
    # A completed operation's witness is retained. An uncertain operation must
    # first be expired, then fenced. Time alone never proves provider absence.
    host_possible = current.phase not in {
        "prepared",
        "create_project",
        "project_ready",
        "create_environment",
        "environment_ready",
        "create_recipient",
        "recipient_ready",
    }
    host_fenced = False
    uncertain_host = host_possible and any(row.state != "completed" for row in observed)
    if uncertain_host or (host_possible and current.execution_fence_version >= 1):
        if not current.target_id or current.execution_fence_version < 1:
            raise ValueError(
                "Legacy host execution requires a terminal witnessed recovery before supersession."
            )
        hosts = [
            {
                "target_id": current.target_id,
                "server_id": current.request.destination_server_id,
                "app_name": current.app_name,
            },
            *current.execution.get("writer_hosts", []),
            *(entry["host"] for entry in current.writer_custody if entry.get("host")),
        ]
        seen = set()
        for host in hosts:
            if (host["target_id"], host["server_id"]) in seen:
                continue
            seen.add((host["target_id"], host["server_id"]))
            projected = current.model_copy(
                update={
                    "target_id": host["target_id"],
                    "app_name": host["app_name"],
                    "request": current.request.model_copy(
                        update={"destination_server_id": host["server_id"]}
                    ),
                }
            )
            checkpoint("fence_retained_recipient")
            evidence = run_recipient_program(
                root,
                projected,
                {"namespace": current.namespace, "binding": current.plan_sha256},
                RETIRE_EXECUTION_PROGRAM,
                checkpoint,
                retire_execution=True,
            )
            if evidence.get(EXECUTION_FENCE_MARKER) != current.plan_sha256:
                raise ValueError("Late host execution was not fenced.")
        host_fenced = True
    with store._session_factory() as session:
        store._begin_serialized_write(session)
        store._lock_product_authority_bundle_write(session)
        row = session.get(
            LaunchplaneProductionRecipientRow, (product, request.context), with_for_update=True
        )
        if row is None or ProductionRecipientRecord.model_validate(row.payload) != current:
            raise ValueError("Custody changed while fencing the retained namespace.")
        from control_plane.production_recipient import same_recipient_authority

        if not same_recipient_authority(
            authority_snapshot(store, product, request.context), replacement.authority_snapshot
        ):
            raise ValueError("Authority changed during continuation; review it again.")
        locked = session.scalars(
            select(LaunchplaneIdempotencyRow)
            .where(
                or_(
                    LaunchplaneIdempotencyRow.provider_target_key
                    == "production-recipient:" + product + ":" + request.context,
                    LaunchplaneIdempotencyRow.record_id.in_([row.record_id for row in observed]),
                )
            )
            .with_for_update()
        ).all()
        actual = [LaunchplaneIdempotencyRecord.model_validate(r.payload) for r in locked]
        if sorted(r.model_dump_json() for r in actual) != sorted(
            r.model_dump_json() for r in observed
        ):
            raise ValueError("Original execution changed during continuation.")
        now = store._database_mutation_timestamp(session)
        for reservation in actual:
            if reservation.state != "completed" and parse_launchplane_mutation_timestamp(
                reservation.lease_expires_at, field_name="lease_expires_at"
            ) > parse_launchplane_mutation_timestamp(now, field_name="observed_at"):
                raise ValueError("The original execution lease is still active.")
        checkpoint("commit_retained_custody")
        for reservation_row, reservation in zip(locked, actual, strict=True):
            if reservation.state == "completed":
                continue
            if reservation.record_id in writer_receipts:
                from control_plane.provider_operations import provider_operation_response_payload

                terminal = store._updated_idempotency_record(
                    reservation,
                    state="completed",
                    updated_at=now,
                    recorded_at=now,
                    response_status_code=202,
                    response_trace_id="recipient-custody-continuation",
                    response_payload=provider_operation_response_payload(
                        trace_id="recipient-custody-continuation",
                        records={},
                        result=writer_receipts[reservation.record_id].model_dump(mode="json"),
                    ),
                )
                store._sync_idempotency_row(reservation_row, terminal)
                continue
            # Fence service workers atomically with custody replacement. A late
            # create may still materialize in the retained namespace; it cannot
            # pass its next lease checkpoint or write into the new generation.
            settled = store._updated_idempotency_record(
                reservation,
                state="completed",
                updated_at=now,
                recorded_at=now,
                response_status_code=202,
                response_trace_id="recipient-custody-continuation",
                response_payload={
                    "status": "retained_uncertain",
                    "namespace": current.namespace,
                    "original_key_retained": True,
                },
            )
            store._sync_idempotency_row(reservation_row, settled)
        replacement = replacement.model_copy(
            update={
                "continuation": {
                    "reviewed_plan_sha256": request.reviewed_plan_sha256,
                    "original_key": current.original_key,
                    "retained_reservations": [r.model_dump(mode="json") for r in actual],
                    "terminal_writer_receipts": {
                        key: value.model_dump(mode="json") for key, value in writer_receipts.items()
                    },
                    "retained_namespace": current.namespace,
                    "host_execution_fenced": host_fenced,
                    "continued_at": now,
                    "request": request.model_dump(
                        mode="json", exclude={"mode", "reviewed_plan_sha256"}
                    ),
                    "result": result,
                }
            }
        )
        row.payload = replacement.model_dump(mode="json")
        store._after_product_authority_bundle_step("continue_production_recipient")
        session.commit()
    return result
