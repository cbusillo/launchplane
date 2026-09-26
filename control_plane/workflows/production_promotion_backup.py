from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
import json
from typing import Protocol, cast

import click

from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.production_backup_authority import (
    ProxmoxGuestBackupDestinationReference,
)
from control_plane.contracts.promotion_record import BackupGateEvidence
from control_plane.contracts.verireel_prod_backup_gate_operation import (
    VeriReelProdBackupGateOperationRecord,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.production_backup_gate import resolve_production_backup_binding


ODOO_PROMOTION_BACKUP_ACTION = "odoo_prod_promotion_run.execute"
GENERIC_WEB_PROMOTION_BACKUP_ACTION = "generic_web_prod_promotion.execute"


class ProductionPromotionBackupStore(Protocol):
    def read_backup_gate_record(self, record_id: str) -> BackupGateRecord: ...

    def list_verireel_prod_backup_gate_operation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        backup_record_id: str = "",
        statuses: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> tuple[VeriReelProdBackupGateOperationRecord, ...]: ...


def require_production_promotion_backup(
    *,
    record_store: object,
    product: str,
    context: str,
    instance: str,
    promotion_action: str,
    backup_record_id: str,
) -> BackupGateEvidence:
    """Require worker-completed evidence for the exact current policy and targets."""
    operation = _read_operation(
        record_store, product, context, instance, promotion_action, backup_record_id
    )
    assert operation.binding is not None and operation.result is not None
    binding = operation.binding
    try:
        current = resolve_production_backup_binding(record_store, binding.request)
        store = cast(ProductionPromotionBackupStore, record_store)
        record = store.read_backup_gate_record(backup_record_id)
    except (AttributeError, FileNotFoundError, TypeError, ValueError) as error:
        raise click.ClickException(
            "Production backup authority or evidence is unavailable."
        ) from error
    if current != binding:
        raise click.ClickException("Production backup authority changed after capture.")
    evidence = record.evidence
    if (
        record.source != "launchplane-production-backup-gate"
        or not record.required
        or record.status != "pass"
        or (record.context, record.instance) != (context, instance)
        or evidence != operation.result.evidence
        or evidence.get("capture_status") != "verified"
        or not evidence.get("snapshot_name")
        or not evidence.get("independent_backup_id")
    ):
        raise click.ClickException(
            "Production backup capture evidence is incomplete or mismatched."
        )
    expected = {
        "product": product,
        "context": context,
        "instance": instance,
        "promotion_action": promotion_action,
        "policy_record_id": binding.policy.record_id,
        "policy_revision": str(binding.policy.policy_revision),
        "policy_digest": binding.policy.policy_digest,
        "source_target_record_id": binding.source_target.record_id,
        "source_target_digest": binding.source_target.target_digest,
        "destination_target_record_id": binding.destination_target.record_id,
        "destination_target_digest": binding.destination_target.target_digest,
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise click.ClickException(
            "Production backup evidence does not match its authority binding."
        )
    now = datetime.now(UTC)
    for field, maximum_age in (
        ("snapshot_finished_at", binding.policy.fast_snapshot.max_evidence_age_seconds),
        (
            "independent_backup_finished_at",
            binding.policy.independent_backup.max_evidence_age_seconds,
        ),
    ):
        try:
            timestamp = datetime.fromisoformat(evidence[field].replace("Z", "+00:00"))
            age = (now - timestamp).total_seconds()
        except (KeyError, TypeError, ValueError) as error:
            raise click.ClickException(
                "Production backup evidence has an invalid timestamp."
            ) from error
        if age < 0 or age > maximum_age:
            raise click.ClickException("Production backup evidence is stale or future-dated.")
    # A later capture may have pruned this snapshot, including a capture whose
    # terminal result was lost after retention. Require a fresh capture instead.
    for other in store.list_verireel_prod_backup_gate_operation_records():
        if (
            other.operation_id != operation.operation_id
            and other.binding is not None
            and _source_key(other) == _source_key(operation)
            and (
                other.progress_evidence.get("capture_status") == "verified"
                or (
                    other.result is not None
                    and other.result.evidence.get("capture_status") == "verified"
                )
            )
            and other.started_at
            and other.started_at >= operation.started_at
        ):
            raise click.ClickException(
                "Production backup evidence was superseded by another capture."
            )
    return BackupGateEvidence(
        required=True,
        status="pass",
        evidence={
            **evidence,
            "backup_record_id": record.record_id,
            "backup_operation_id": operation.operation_id,
        },
    )


@contextmanager
def production_promotion_backup_guard(
    *,
    record_store: object,
    product: str,
    context: str,
    instance: str,
    promotion_action: str,
    backup_record_id: str,
) -> Iterator[Callable[[str], None]]:
    """Validate before effects; retain the backup lock through the promotion."""
    operation = _read_operation(
        record_store, product, context, instance, promotion_action, backup_record_id
    )
    if not isinstance(record_store, PostgresRecordStore):
        raise click.ClickException("Production promotion backup locking requires database storage.")
    with record_store.production_backup_source_lock(_source_key(operation)) as check_lock:
        if check_lock is None:
            raise click.ClickException("Production backup source is busy.")

        effects_started = False

        def require_lock() -> None:
            try:
                check_lock()
            except Exception as error:
                raise click.ClickException(
                    "Production promotion backup source lock was lost."
                ) from error

        def require_evidence() -> None:
            require_production_promotion_backup(
                record_store=record_store,
                product=product,
                context=context,
                instance=instance,
                promotion_action=promotion_action,
                backup_record_id=backup_record_id,
            )

        def checkpoint(_phase: str) -> None:
            nonlocal effects_started
            require_lock()
            if not effects_started:
                require_evidence()
                effects_started = True

        require_lock()
        require_evidence()
        yield checkpoint
        require_lock()


def _read_operation(
    record_store: object,
    product: str,
    context: str,
    instance: str,
    promotion_action: str,
    backup_record_id: str,
) -> VeriReelProdBackupGateOperationRecord:
    if not backup_record_id.strip():
        raise click.ClickException("Production promotion requires infrastructure backup evidence.")
    store = cast(ProductionPromotionBackupStore, record_store)
    try:
        operations = store.list_verireel_prod_backup_gate_operation_records(
            product=product,
            context_name=context,
            instance_name=instance,
            backup_record_id=backup_record_id,
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise click.ClickException(
            "Production backup operation evidence is unavailable."
        ) from error
    if len(operations) != 1:
        raise click.ClickException(
            "Production promotion requires one exact backup capture operation."
        )
    operation = operations[0]
    if (
        operation.binding is None
        or operation.status != "pass"
        or operation.result is None
        or operation.result.backup_status != "pass"
        or not operation.started_at
        or (operation.product, operation.context, operation.instance, operation.backup_record_id)
        != (product, context, instance, backup_record_id)
        or operation.binding.policy.promotion_action != promotion_action
    ):
        raise click.ClickException("Production backup operation has not passed for this promotion.")
    return operation


def _source_key(operation: VeriReelProdBackupGateOperationRecord) -> str:
    assert operation.binding is not None
    source = operation.binding.source_target.destination
    assert isinstance(source, ProxmoxGuestBackupDestinationReference)
    return json.dumps([source.host.lower(), source.guest_kind, source.guest_id])
