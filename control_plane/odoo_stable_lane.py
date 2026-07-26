from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

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
