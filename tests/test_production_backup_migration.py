from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.production_backup_authority import (
    plan_production_backup_authority_write,
)
from control_plane.production_backup_migration import (
    LegacyProductionBackupMigrationRequest,
    build_legacy_production_backup_authority_envelope,
)
from control_plane.storage.filesystem import FilesystemRecordStore


def _runtime_record(*, backup_mode: str = "both") -> RuntimeEnvironmentRecord:
    return RuntimeEnvironmentRecord(
        scope="instance",
        context="example-product",
        instance="prod",
        env={
            "VERIREEL_PROD_PROXMOX_HOST": "proxmox.example.invalid",
            "VERIREEL_PROD_PROXMOX_USER": "backup-operator",
            "VERIREEL_PROD_PROXMOX_SSH_PRIVATE_KEY": "private-secret",
            "VERIREEL_PROD_PROXMOX_SSH_KNOWN_HOSTS": "known-host-secret",
            "VERIREEL_PROD_CT_ID": "101",
            "VERIREEL_PROD_BACKUP_MODE": backup_mode,
            "VERIREEL_PROD_BACKUP_STORAGE": "pbs-production",
            "VERIREEL_PROD_SNAPSHOT_PREFIX": "example-predeploy",
            "VERIREEL_PROD_SNAPSHOT_KEEP": 5,
        },
        updated_at="2026-09-03T01:00:00Z",
        source_label="legacy-live-record",
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
    def test_migration_builds_redacted_dual_backup_authority_and_preserves_legacy_gate(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = FilesystemRecordStore(Path(temporary_directory))
            runtime_record = _runtime_record()
            store.write_runtime_environment_record(runtime_record)
            request = LegacyProductionBackupMigrationRequest.model_validate(_request())
            envelope = build_legacy_production_backup_authority_envelope(
                record_store=store,
                request=request,
            )
            serialized = envelope.model_dump_json()
            self.assertNotIn("private-secret", serialized)
            self.assertNotIn("known-host-secret", serialized)
            self.assertIn("pbs-production", serialized)
            plan = plan_production_backup_authority_write(
                record_store=store,
                envelope=envelope,
            )
            response_json = plan.result.model_dump_json()
            self.assertNotIn("proxmox.example.invalid", response_json)
            self.assertNotIn("pbs-production", response_json)

            apply_request = LegacyProductionBackupMigrationRequest.model_validate(
                _request(mode="apply", reviewed_digest=plan.result.authority_digest)
            )
            apply_envelope = build_legacy_production_backup_authority_envelope(
                record_store=store,
                request=apply_request,
            )
            result = store.apply_production_backup_authority(apply_envelope)
            self.assertEqual(result.status, "applied")
            self.assertEqual(
                store.list_runtime_environment_records(
                    scope="instance",
                    context_name="example-product",
                    instance_name="prod",
                ),
                (runtime_record,),
            )

    def test_migration_rejects_partial_or_changed_legacy_authority(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = FilesystemRecordStore(Path(temporary_directory))
            store.write_runtime_environment_record(_runtime_record(backup_mode="snapshot"))
            with self.assertRaisesRegex(ValueError, "requires both snapshot"):
                build_legacy_production_backup_authority_envelope(
                    record_store=store,
                    request=LegacyProductionBackupMigrationRequest.model_validate(_request()),
                )
            with self.assertRaisesRegex(ValueError, "revision changed"):
                build_legacy_production_backup_authority_envelope(
                    record_store=store,
                    request=LegacyProductionBackupMigrationRequest.model_validate(
                        _request() | {"runtime_environment_updated_at": "2026-09-03T00:00:00Z"}
                    ),
                )

    def test_apply_requires_reviewed_dry_run_digest(self) -> None:
        with self.assertRaisesRegex(ValueError, "reviewed_authority_digest"):
            LegacyProductionBackupMigrationRequest.model_validate(_request(mode="apply"))


if __name__ == "__main__":
    unittest.main()
