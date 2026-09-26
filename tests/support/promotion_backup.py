from contextlib import nullcontext
import unittest
from unittest.mock import patch

from control_plane.contracts.promotion_record import BackupGateEvidence


def stub_verified_promotion_backup(test: unittest.TestCase, module: str) -> None:
    """Isolate existing deployment tests; the backup gate has store-backed tests."""
    evidence = BackupGateEvidence(
        required=True,
        status="pass",
        evidence={"backup_record_id": "infrastructure-example", "policy_revision": "1"},
    )
    test.enterContext(patch(f"{module}.require_production_promotion_backup", return_value=evidence))
    if module.endswith("odoo_prod_promotion_run"):
        return
    test.enterContext(
        patch(
            f"{module}.production_promotion_backup_guard",
            side_effect=lambda **_kwargs: nullcontext(lambda _phase: None),
        )
    )
