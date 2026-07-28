from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, TypeAlias

from control_plane.contracts.durable_operation_authorization import DurableOperationCancellation
from control_plane.contracts.odoo_prod_backup_restore_operation import (
    OdooProdBackupRestoreOperationRecord,
)
from control_plane.contracts.odoo_prod_retained_volume_backup_import_operation import (
    OdooProdRetainedVolumeBackupImportOperationRecord,
)
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)


OdooStableLaneOperationKind = Literal[
    "stable_bootstrap",
    "target_replacement",
    "prod_backup_restore",
    "retained_volume_backup_import",
]
ODOO_STABLE_LANE_BLOCKING_STATUSES: tuple[str, ...] = (
    "pending",
    "running",
    "reconciliation_required",
)
OdooStableLaneOperationRecord: TypeAlias = (
    OdooStableBootstrapOperationRecord
    | OdooStableTargetReplacementOperationRecord
    | OdooProdBackupRestoreOperationRecord
    | OdooProdRetainedVolumeBackupImportOperationRecord
)


@dataclass(frozen=True)
class OdooStableLaneOperationOwner:
    operation_kind: OdooStableLaneOperationKind
    operation_id: str


class OdooStableLaneOperationConflictError(RuntimeError):
    def __init__(self, owner: OdooStableLaneOperationOwner) -> None:
        super().__init__(
            "Another Odoo stable-lane operation is already active for this "
            "product/context/instance."
        )
        self.owner = owner


def odoo_stable_lane_cancellation_is_allowed(
    *,
    current_status: str,
    reconciliation_required_at: str,
    cancellation: DurableOperationCancellation | None,
) -> bool:
    if current_status == "pending":
        return cancellation is not None and cancellation.reconciliation_attestation is None
    if current_status != "reconciliation_required" or cancellation is None:
        return False
    attestation = cancellation.reconciliation_attestation
    if attestation is None:
        return False
    try:
        inspected_at = _parse_utc_timestamp(attestation.provider_inspected_at)
        required_at = _parse_utc_timestamp(reconciliation_required_at)
        cancelled_at = _parse_utc_timestamp(cancellation.cancelled_at)
    except ValueError:
        return False
    return required_at <= inspected_at <= cancelled_at


def _parse_utc_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Odoo stable-lane timestamps require a timezone.")
    return parsed.astimezone(timezone.utc)


def odoo_stable_lane_operation_priority(
    *,
    status: str,
    created_at: str,
    operation_kind: OdooStableLaneOperationKind,
    operation_id: str,
) -> tuple[int, str, int, str]:
    status_priority = {
        "reconciliation_required": 0,
        "running": 1,
        "pending": 2,
    }
    kind_priority = {
        "stable_bootstrap": 0,
        "target_replacement": 1,
        "prod_backup_restore": 2,
        "retained_volume_backup_import": 3,
    }
    return (
        status_priority[status],
        created_at,
        kind_priority[operation_kind],
        operation_id,
    )
