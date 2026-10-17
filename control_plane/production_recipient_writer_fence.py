"""Fence one uncertain writer dispatch that never persisted its original host witness."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    parse_launchplane_mutation_timestamp,
)
from control_plane.contracts.production_recipient import ProductionRecipientRecord
from control_plane.integration_allowances import canonical_sha256
from control_plane.production_recipient import custody
from control_plane.production_recipient_cutover import (
    WRITER_RECEIPT_PROGRAM,
    cutover_reconciliation_key,
    writer_action,
)
from control_plane.production_recipient_evidence import WRITER_ACTION_MARKER, WRITER_BINDING_MARKER
from control_plane.production_recipient_host import run_recipient_program
from control_plane.production_recipient_inverse import inverse_reconciliation_key
from control_plane.provider_operations import build_provider_operation_key
from control_plane.storage.postgres import (
    LaunchplaneIdempotencyRow,
    LaunchplaneProductionRecipientRow,
    PostgresRecordStore,
)

WRITER_FENCE_ROUTE = "/v1/products/{product}/production-recipient/writer-fence"


class RecipientWriterFenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str = Field(min_length=1)
    preparation_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    operation_id: str = Field(min_length=1)
    mode: Literal["dry-run", "apply", "readback"] = "dry-run"
    reason: str = Field(min_length=1)
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")


class RecipientWriterFencePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    plan_sha256: str
    operation_id: str
    operation: Literal["testing_writer", "cutover", "inverse"]
    witness_binding: str
    fenced: bool = False
    writer_effect: Literal["none"] = "none"


@dataclass(frozen=True)
class FencedWriter:
    operation: Literal["testing_writer", "cutover", "inverse"]
    operation_id: str
    attempt: str
    binding: str
    host: dict[str, str]
    old: bool
    projected: ProductionRecipientRecord

    def disposition(
        self, record: ProductionRecipientRecord, fence: dict[str, Any]
    ) -> ProductionRecipientRecord:
        # Each disposition is the flow's own state for "no writer was touched".
        if self.operation == "testing_writer":
            return record.model_copy(
                update={
                    "writer_custody": [
                        {**row, "fenced": fence}
                        if row.get("record_id") == self.operation_id
                        else row
                        for row in record.writer_custody
                    ]
                }
            )
        if self.operation == "cutover":
            return record.model_copy(
                update={
                    "phase": "preflight_refused",
                    "execution": {**record.execution, self.attempt + ":old_pause_fenced": fence},
                }
            )
        return record.model_copy(
            update={
                "phase": "published",
                "execution": {
                    **record.execution,
                    "inverse_pause_fenced:" + self.attempt: fence,
                    "inverse_aborted:" + self.attempt: fence["fenced_at"],
                },
            }
        )


def recorded_fence(
    record: ProductionRecipientRecord, operation_id: str
) -> tuple[Literal["testing_writer", "cutover", "inverse"], dict[str, Any]] | None:
    fences: list[tuple[Literal["testing_writer", "cutover", "inverse"], dict[str, Any]]] = [
        ("testing_writer", row["fenced"]) for row in record.writer_custody if row.get("fenced")
    ]
    for key, value in record.execution.items():
        if key.startswith("inverse_pause_fenced:"):
            fences.append(("inverse", value))
        elif key.endswith(":old_pause_fenced"):
            fences.append(("cutover", value))
    matches = [fence for fence in fences if fence[1]["operation_id"] == operation_id]
    if len(matches) > 1:
        raise ValueError("The writer operation has ambiguous fences.")
    return matches[0] if matches else None


def read_reservation(store: PostgresRecordStore, operation_id: str) -> LaunchplaneIdempotencyRecord:
    with store._session_factory() as session:
        row = session.scalars(
            select(LaunchplaneIdempotencyRow).where(
                LaunchplaneIdempotencyRow.record_id == operation_id
            )
        ).one_or_none()
    if row is None:
        raise ValueError("Name the exact original writer operation.")
    return LaunchplaneIdempotencyRecord.model_validate(row.payload)


def fenced_writer(
    record: ProductionRecipientRecord, reservation: LaunchplaneIdempotencyRecord
) -> FencedWriter:
    key = build_provider_operation_key(
        scope=reservation.scope,
        route_path=reservation.route_path,
        idempotency_key=reservation.idempotency_key,
        request_fingerprint=reservation.request_fingerprint,
        reconciliation_key=reservation.reconciliation_key,
    )
    attempt = canonical_sha256(key)
    service = record.request.application_service
    entries = [
        entry for entry in record.writer_custody if entry.get("record_id") == reservation.record_id
    ]
    if entries:
        entry = entries[0]
        request = entry["request"]
        if (
            len(entries) != 1
            or entry["operation"] != key
            or reservation.reconciliation_key
            != "production-recipient-writer:"
            + request["action"]
            + ":"
            + request["preparation_plan_sha256"]
            + ":"
            + request["reviewed_plan_sha256"]
        ):
            raise ValueError("The writer operation belongs to another request.")
        host = dict(entry["host"])
        return FencedWriter(
            "testing_writer",
            reservation.record_id,
            attempt,
            entry["witness_binding"],
            host,
            False,
            record.model_copy(
                update={
                    "target_id": host["target_id"],
                    "app_name": host["app_name"],
                    "request": record.request.model_copy(
                        update={"destination_server_id": host["server_id"]}
                    ),
                }
            ),
        )
    if reservation.reconciliation_key == cutover_reconciliation_key(record.plan_sha256):
        if (
            record.phase != "bootstrapping"
            or not record.execution.get(attempt + ":old_pause_dispatched_at")
            or record.execution.get(attempt + ":rollback_completed_at")
            or len(record.execution.get("writer_hosts", [])) != 1
        ):
            raise ValueError("Only an uncertain original writer pause can be fenced.")
        return FencedWriter(
            "cutover",
            reservation.record_id,
            attempt,
            canonical_sha256({"preparation": record.plan_sha256, "operation": key}),
            {**record.execution["writer_hosts"][0], "service": service},
            True,
            record,
        )
    if reservation.reconciliation_key == inverse_reconciliation_key(record.plan_sha256):
        if record.phase != "reverting" or not record.execution.get(
            "inverse_pause_dispatched:" + attempt
        ):
            raise ValueError("Only an uncertain replacement writer pause can be fenced.")
        return FencedWriter(
            "inverse",
            reservation.record_id,
            attempt,
            canonical_sha256(
                {"publication": record.publication["published_sha256"], "operation": key}
            ),
            {
                "target_id": record.target_id,
                "server_id": record.request.destination_server_id,
                "app_name": record.app_name,
                "service": service,
            },
            False,
            record,
        )
    raise ValueError("The operation is not a recipient writer dispatch.")


def require_expired(store: PostgresRecordStore, reservation: LaunchplaneIdempotencyRecord) -> None:
    with store._session_factory() as session:
        now = store._database_mutation_timestamp(session)
    if reservation.state == "completed" or parse_launchplane_mutation_timestamp(
        reservation.lease_expires_at or now, field_name="lease_expires_at"
    ) > parse_launchplane_mutation_timestamp(now, field_name="observed_at"):
        raise ValueError("The original writer operation is completed or still leased.")


def fence_plan(
    store: PostgresRecordStore, product: str, request: RecipientWriterFenceRequest
) -> tuple[ProductionRecipientRecord, LaunchplaneIdempotencyRecord, FencedWriter, str]:
    record = custody(store, product, request.context)
    if record is None or record.plan_sha256 != request.preparation_plan_sha256:
        raise ValueError("Retain the original recipient generation.")
    reservation = read_reservation(store, request.operation_id)
    if recorded_fence(record, reservation.record_id) is not None:
        raise ValueError("This writer operation already has a recorded fence.")
    writer = fenced_writer(record, reservation)
    digest = canonical_sha256(
        {
            "request": request.model_dump(mode="json", exclude={"mode", "reviewed_plan_sha256"}),
            "custody": record.model_dump(mode="json"),
            "reservation": reservation.model_dump(mode="json"),
            "operation": writer.operation,
            "binding": writer.binding,
            "host": writer.host,
        }
    )
    return record, reservation, writer, digest


def retained_fence(
    root: Path,
    store: PostgresRecordStore,
    product: str,
    request: RecipientWriterFenceRequest,
) -> RecipientWriterFencePlan:
    record = custody(store, product, request.context)
    if record is None or record.plan_sha256 != request.preparation_plan_sha256:
        raise ValueError("Retain the original recipient generation.")
    reservation = read_reservation(store, request.operation_id)
    retained = recorded_fence(record, reservation.record_id)
    if retained is None:
        raise ValueError("No retained writer fence is recorded for this operation.")
    operation, fence = retained
    host = fence["host"]
    evidence = run_recipient_program(
        root,
        record.model_copy(
            update={
                "target_id": host["target_id"],
                "app_name": host["app_name"],
                "request": record.request.model_copy(
                    update={"destination_server_id": host["server_id"]}
                ),
            }
        ),
        {
            "project": host["app_name"],
            "service": host["service"],
            "binding": fence["witness_binding"],
            "action": "receipt_fenced",
            "inspect": True,
        },
        WRITER_RECEIPT_PROGRAM,
        lambda _phase: None,
        custodied_read=True,
    )
    if (
        evidence.get(WRITER_ACTION_MARKER) != "receipt_fenced"
        or evidence.get(WRITER_BINDING_MARKER) != fence["witness_binding"]
    ):
        raise ValueError("The retained host fence is unverified.")
    return RecipientWriterFencePlan(
        product=product,
        context=request.context,
        plan_sha256=fence["plan_sha256"],
        operation_id=reservation.record_id,
        operation=operation,
        witness_binding=fence["witness_binding"],
        fenced=True,
    )


def fence_writer_operation(
    root: Path,
    store: PostgresRecordStore,
    product: str,
    request: RecipientWriterFenceRequest,
    checkpoint: Callable[[str], None],
) -> RecipientWriterFencePlan:
    current = custody(store, product, request.context)
    recorded = recorded_fence(current, request.operation_id) if current is not None else None
    if request.mode == "readback" or request.mode == "apply" and recorded is not None:
        retained = retained_fence(root, store, product, request)
        if request.mode == "apply" and retained.plan_sha256 != request.reviewed_plan_sha256:
            raise ValueError("Another review fenced this writer operation.")
        return retained
    record, reservation, writer, digest = fence_plan(store, product, request)
    plan = RecipientWriterFencePlan(
        product=product,
        context=request.context,
        plan_sha256=digest,
        operation_id=reservation.record_id,
        operation=writer.operation,
        witness_binding=writer.binding,
    )
    require_expired(store, reservation)
    if request.mode != "apply":
        return plan
    if digest != request.reviewed_plan_sha256:
        raise ValueError("Review the exact current writer operation before fencing it.")

    def fence_checkpoint(phase: str) -> None:
        checkpoint(phase)
        current = fence_plan(store, product, request)
        if current[3] != digest:
            raise ValueError("The uncertain writer operation changed during review.")
        require_expired(store, current[1])

    # The host fence only refuses more: it succeeds solely when the original
    # binding never persisted a witness, and makes any late dispatch refuse.
    writer_action(
        root,
        writer.projected,
        old=writer.old,
        action="fence",
        checkpoint=fence_checkpoint,
        binding=writer.binding,
    )
    with store._session_factory() as session:
        store._begin_serialized_write(session)
        store._lock_product_authority_bundle_write(session)
        row = session.get(
            LaunchplaneProductionRecipientRow, (product, request.context), with_for_update=True
        )
        if row is None or ProductionRecipientRecord.model_validate(row.payload) != record:
            raise ValueError("Custody changed while fencing the writer operation.")
        locked = session.scalars(
            select(LaunchplaneIdempotencyRow)
            .where(LaunchplaneIdempotencyRow.record_id == reservation.record_id)
            .with_for_update()
        ).one()
        now = store._database_mutation_timestamp(session)
        if LaunchplaneIdempotencyRecord.model_validate(
            locked.payload
        ) != reservation or parse_launchplane_mutation_timestamp(
            reservation.lease_expires_at or now, field_name="lease_expires_at"
        ) > parse_launchplane_mutation_timestamp(now, field_name="observed_at"):
            raise ValueError("The original writer operation changed while fencing it.")
        checkpoint("commit_writer_fence")
        row.payload = writer.disposition(
            record,
            {
                "plan_sha256": digest,
                "operation_id": reservation.record_id,
                "witness_binding": writer.binding,
                "host": writer.host,
                "reason": request.reason,
                "fenced_at": now,
            },
        ).model_dump(mode="json")
        store._after_product_authority_bundle_step("fence_production_recipient_writer")
        session.commit()
    return plan.model_copy(update={"fenced": True})
