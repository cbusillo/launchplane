import unittest

from control_plane.production_backup_migration import (
    LegacyProductionBackupMigrationRequest,
    build_legacy_production_backup_authority_envelope,
)


def _request(*, mode: str = "dry_run", reviewed_digest: str = "") -> dict[str, object]:
    return {
        "mode": mode,
        "product": "example-product",
        "context": "example-product",
        "instance": "prod",
        "promotion_action": "verireel_prod_promotion.execute",
        "source_target_id": "example-prod-guest",
        "destination_target_id": "example-independent-backup",
        "runtime_environment_updated_at": "2026-09-03T01:00:00Z",
        "effective_at": "2026-09-03T02:00:00Z",
        "review_after": "2027-09-03T02:00:00Z",
        "snapshot_max_evidence_age_seconds": 3600,
        "independent_backup_max_evidence_age_seconds": 86400,
        "source": "operator-reviewed-migration",
        "reason": "issue-2306",
        "reviewed_authority_digest": reviewed_digest,
    }


class ProductionBackupMigrationTests(unittest.TestCase):
    def test_migration_refuses_without_reading_or_writing_legacy_authority(self) -> None:
        from unittest.mock import Mock

        for mode in ("dry_run", "apply"):
            with self.subTest(mode=mode):
                store = Mock()
                request = LegacyProductionBackupMigrationRequest.model_validate(
                    _request(mode=mode, reviewed_digest="reviewed" if mode == "apply" else "")
                )
                with self.assertRaisesRegex(ValueError, "/v1/production-backup-authority/apply"):
                    build_legacy_production_backup_authority_envelope(
                        record_store=store, request=request
                    )
                self.assertEqual(store.mock_calls, [])

    def test_apply_requires_reviewed_dry_run_digest(self) -> None:
        with self.assertRaisesRegex(ValueError, "reviewed_authority_digest"):
            LegacyProductionBackupMigrationRequest.model_validate(_request(mode="apply"))


if __name__ == "__main__":
    unittest.main()
