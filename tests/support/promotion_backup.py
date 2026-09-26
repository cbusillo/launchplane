from contextlib import AbstractContextManager, nullcontext
from typing import cast
import unittest
from unittest.mock import patch

from control_plane.contracts.promotion_record import BackupGateEvidence, PromotionRecord
from control_plane.workflows.production_promotion_backup import (
    ProductionPromotionBackupGuard,
    ProductionPromotionBackupStore,
)


def _stub_guard(**kwargs: object) -> AbstractContextManager[ProductionPromotionBackupGuard]:
    store = cast(ProductionPromotionBackupStore, kwargs["record_store"])
    store.write_promotion_record(cast(PromotionRecord, kwargs["pending_promotion"]))
    return nullcontext(
        ProductionPromotionBackupGuard(lambda _phase: None, {"source_lock_status": "held"})
    )


def stub_verified_promotion_backup(test: unittest.TestCase, module: str) -> None:
    """Isolate existing deployment tests; the backup gate has store-backed tests."""
    evidence = BackupGateEvidence(
        status="pass",
        evidence={"backup_record_id": "infrastructure-example", "policy_revision": "1"},
    )
    test.enterContext(patch(f"{module}.require_production_promotion_backup", return_value=evidence))
    if module.endswith("odoo_prod_promotion_run"):
        return
    test.enterContext(
        patch(
            f"{module}.production_promotion_backup_guard",
            side_effect=_stub_guard,
        )
    )
