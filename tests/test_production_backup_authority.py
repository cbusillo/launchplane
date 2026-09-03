from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest

from pydantic import ValidationError

from control_plane.contracts.product_environment_read_model import (
    build_product_environment_config_status,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.production_backup_authority import (
    ProductionBackupPolicyRecord,
    ProductionBackupTargetRecord,
    ProxmoxGuestBackupDestinationReference,
    ProxmoxStorageBackupDestinationReference,
)
from control_plane.drivers.registry import build_driver_context_view
from control_plane.product_operational_readiness_service import (
    ProductOperationalReadinessStore,
    build_product_operational_readiness_service_result,
)
from control_plane.production_backup_authority import (
    ProductionBackupAuthorityConflictError,
    ProductionBackupAuthorityWriteEnvelope,
    plan_production_backup_authority_write,
    resolve_production_backup_authority,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.auth import _identity


def _source_target(
    *,
    revision: int = 1,
    status: str = "active",
    supersedes_record_id: str | None = None,
    review_after: str = "2027-09-03T00:00:00Z",
) -> ProductionBackupTargetRecord:
    return ProductionBackupTargetRecord.model_validate(
        {
            "target_id": "example-prod-guest",
            "target_revision": revision,
            "status": status,
            "destination": {
                "destination_kind": "proxmox_guest",
                "host": "proxmox.example.invalid",
                "username": "backup-operator",
                "guest_kind": "lxc",
                "guest_id": "101",
            },
            "effective_at": f"2026-09-0{revision}T00:00:00Z",
            "review_after": review_after,
            "source": "test",
            "reason": "issue-2306",
            "supersedes_record_id": supersedes_record_id,
        }
    )


def _destination_target(
    *, review_after: str = "2027-09-03T00:00:00Z"
) -> ProductionBackupTargetRecord:
    return ProductionBackupTargetRecord(
        target_id="example-independent-backup",
        target_revision=1,
        destination=ProxmoxStorageBackupDestinationReference(
            host="proxmox.example.invalid",
            username="backup-operator",
            storage_id="pbs-production",
        ),
        effective_at="2026-09-01T00:00:00Z",
        review_after=review_after,
        source="test",
        reason="issue-2306",
    )


def _policy(
    *,
    revision: int = 1,
    status: str = "active",
    supersedes_record_id: str | None = None,
    review_after: str = "2027-09-03T00:00:00Z",
) -> ProductionBackupPolicyRecord:
    return ProductionBackupPolicyRecord.model_validate(
        {
            "product": "example-product",
            "context": "example-product",
            "instance": "prod",
            "promotion_action": "verireel_prod_promotion.execute",
            "policy_revision": revision,
            "status": status,
            "fast_snapshot": {
                "source_target_id": "example-prod-guest",
                "snapshot_prefix": "example-predeploy",
                "retention_count": 5,
                "max_evidence_age_seconds": 3600,
            },
            "independent_backup": {
                "source_target_id": "example-prod-guest",
                "destination_target_id": "example-independent-backup",
                "max_evidence_age_seconds": 86400,
            },
            "effective_at": f"2026-09-0{revision}T00:00:00Z",
            "review_after": review_after,
            "source": "test",
            "reason": "issue-2306",
            "supersedes_record_id": supersedes_record_id,
        }
    )


def _dry_run_envelope() -> ProductionBackupAuthorityWriteEnvelope:
    return ProductionBackupAuthorityWriteEnvelope(
        mode="dry_run",
        targets=(_source_target(), _destination_target()),
        policy=_policy(),
    )


def _profile() -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord.model_validate(
        {
            "product": "example-product",
            "display_name": "Example Product",
            "repository": "example/example-product",
            "driver_id": "verireel",
            "image": {"repository": "ghcr.io/example/example-product"},
            "lanes": [
                {
                    "instance": "prod",
                    "context": "example-product",
                    "base_url": "https://example.invalid",
                    "health_url": "https://example.invalid/health",
                }
            ],
            "preview": {"enabled": False},
            "expected_config": {},
            "updated_at": "2026-09-01T00:00:00Z",
            "source": "test",
        }
    )


class ProductionBackupAuthorityContractTests(unittest.TestCase):
    def test_target_contract_rejects_secret_material(self) -> None:
        with self.assertRaises(ValidationError):
            ProxmoxGuestBackupDestinationReference.model_validate(
                {
                    "host": "proxmox.example.invalid",
                    "username": "backup-operator",
                    "guest_kind": "lxc",
                    "guest_id": "101",
                    "ssh_private_key": "secret",
                }
            )

    def test_resolution_reports_ready_stale_retired_and_invalid(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = FilesystemRecordStore(Path(temporary_directory))
            store.write_production_backup_target_record(_source_target())
            store.write_production_backup_target_record(_destination_target())
            store.write_production_backup_policy_record(_policy())
            ready = resolve_production_backup_authority(
                record_store=store,
                product="example-product",
                context="example-product",
                instance="prod",
                promotion_action="verireel_prod_promotion.execute",
                generated_at="2026-09-03T00:00:00Z",
            )
            self.assertEqual(ready.state, "ready")
            self.assertNotIn("proxmox.example.invalid", ready.model_dump_json())
            self.assertNotIn("pbs-production", ready.model_dump_json())

            stale = resolve_production_backup_authority(
                record_store=store,
                product="example-product",
                context="example-product",
                instance="prod",
                promotion_action="verireel_prod_promotion.execute",
                generated_at="2028-09-03T00:00:00Z",
            )
            self.assertEqual(stale.state, "stale")

            wrong_action = resolve_production_backup_authority(
                record_store=store,
                product="example-product",
                context="example-product",
                instance="prod",
                promotion_action="different_promotion.execute",
                generated_at="2026-09-03T00:00:00Z",
            )
            self.assertEqual(wrong_action.state, "missing")

            isolated = FilesystemRecordStore(Path(temporary_directory) / "retired")
            isolated.write_production_backup_target_record(_source_target())
            isolated.write_production_backup_target_record(_destination_target())
            active_policy = _policy()
            isolated.write_production_backup_policy_record(active_policy)
            isolated.write_production_backup_policy_record(
                _policy(
                    revision=2,
                    status="retired",
                    supersedes_record_id=active_policy.record_id,
                )
            )
            retired = resolve_production_backup_authority(
                record_store=isolated,
                product="example-product",
                context="example-product",
                instance="prod",
                promotion_action="verireel_prod_promotion.execute",
                generated_at="2026-09-03T00:00:00Z",
            )
            self.assertEqual(retired.state, "retired")

            invalid_store = FilesystemRecordStore(Path(temporary_directory) / "invalid")
            invalid_store.write_production_backup_target_record(_source_target())
            invalid_store.write_production_backup_target_record(
                _destination_target().model_copy(
                    update={
                        "destination": ProxmoxStorageBackupDestinationReference(
                            host="different.example.invalid",
                            username="backup-operator",
                            storage_id="pbs-production",
                        )
                    }
                )
            )
            with self.assertRaises(ValueError):
                invalid_store.write_production_backup_policy_record(_policy())

            class InvalidHistoryStore:
                def list_production_backup_target_records(
                    self,
                    *,
                    target_id: str = "",
                    status: str = "",
                    limit: int | None = None,
                ) -> tuple[ProductionBackupTargetRecord, ...]:
                    del status, limit
                    if target_id == "example-prod-guest":
                        return (_source_target(), _source_target())
                    return (_destination_target(),)

                def list_production_backup_policy_records(
                    self,
                    *,
                    product: str = "",
                    context_name: str = "",
                    instance_name: str = "",
                    promotion_action: str = "",
                    status: str = "",
                    limit: int | None = None,
                ) -> tuple[ProductionBackupPolicyRecord, ...]:
                    del product, context_name, instance_name, promotion_action, status, limit
                    return (_policy(),)

            invalid = resolve_production_backup_authority(
                record_store=InvalidHistoryStore(),
                product="example-product",
                context="example-product",
                instance="prod",
                promotion_action="verireel_prod_promotion.execute",
                generated_at="2026-09-03T00:00:00Z",
            )
            self.assertEqual(invalid.state, "invalid")


class ProductionBackupAuthorityStorageTests(unittest.TestCase):
    def _exercise_store(self, store: FilesystemRecordStore | PostgresRecordStore) -> None:
        dry_run = _dry_run_envelope()
        plan = plan_production_backup_authority_write(
            record_store=store,
            envelope=dry_run,
        )
        apply_envelope = ProductionBackupAuthorityWriteEnvelope.model_validate(
            dry_run.model_dump(mode="json")
            | {
                "mode": "apply",
                "reviewed_authority_digest": plan.result.authority_digest,
            }
        )
        result = store.apply_production_backup_authority(apply_envelope)
        self.assertEqual(result.status, "applied")
        self.assertEqual(len(store.list_production_backup_target_records(status="active")), 2)
        self.assertEqual(len(store.list_production_backup_policy_records(status="active")), 1)

        current_source = store.list_production_backup_target_records(
            target_id="example-prod-guest"
        )[0]
        revised_source = _source_target(
            revision=2,
            supersedes_record_id=current_source.record_id,
        )
        self.assertEqual(store.write_production_backup_target_record(revised_source), "written")
        source_history = store.list_production_backup_target_records(target_id="example-prod-guest")
        self.assertEqual(
            tuple(record.status for record in source_history), ("active", "superseded")
        )
        with self.assertRaisesRegex(ValueError, "append contiguously"):
            store.write_production_backup_target_record(
                _source_target(
                    revision=4,
                    supersedes_record_id=revised_source.record_id,
                )
            )

    def test_filesystem_store_revision_and_bundle_parity(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            self._exercise_store(FilesystemRecordStore(Path(temporary_directory)))

    def test_postgres_store_revision_and_bundle_parity(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "launchplane.sqlite3"
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{database_path.as_posix()}"
            )
            try:
                store.ensure_schema()
                self._exercise_store(store)
            finally:
                store.close()

    def test_replay_requires_matching_lifecycle_status(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = FilesystemRecordStore(Path(temporary_directory))
            source = _source_target()
            destination = _destination_target()
            policy = _policy()
            store.write_production_backup_target_record(source)
            store.write_production_backup_target_record(destination)
            store.write_production_backup_policy_record(policy)
            retired_policy = _policy(
                revision=2,
                status="retired",
                supersedes_record_id=policy.record_id,
            )
            store.write_production_backup_policy_record(retired_policy)
            with self.assertRaisesRegex(
                ProductionBackupAuthorityConflictError,
                "different payload",
            ):
                store.write_production_backup_policy_record(
                    retired_policy.model_copy(update={"status": "active"})
                )

            retired_source = _source_target(
                revision=2,
                status="retired",
                supersedes_record_id=source.record_id,
            )
            store.write_production_backup_target_record(retired_source)
            with self.assertRaisesRegex(
                ProductionBackupAuthorityConflictError,
                "different payload",
            ):
                store.write_production_backup_target_record(
                    retired_source.model_copy(update={"status": "active"})
                )

    def test_reviewed_digest_binds_existing_policy_targets(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = FilesystemRecordStore(Path(temporary_directory))
            initial = _dry_run_envelope()
            initial_plan = plan_production_backup_authority_write(
                record_store=store,
                envelope=initial,
            )
            store.apply_production_backup_authority(
                ProductionBackupAuthorityWriteEnvelope.model_validate(
                    initial.model_dump(mode="json")
                    | {
                        "mode": "apply",
                        "reviewed_authority_digest": initial_plan.result.authority_digest,
                    }
                )
            )
            current_policy = store.list_production_backup_policy_records()[0]
            policy_only_dry_run = ProductionBackupAuthorityWriteEnvelope(
                mode="dry_run",
                policy=_policy(
                    revision=2,
                    supersedes_record_id=current_policy.record_id,
                ),
                expected_current_policy_record_id=current_policy.record_id,
            )
            reviewed = plan_production_backup_authority_write(
                record_store=store,
                envelope=policy_only_dry_run,
            )
            current_source = store.list_production_backup_target_records(
                target_id="example-prod-guest"
            )[0]
            store.write_production_backup_target_record(
                _source_target(
                    revision=2,
                    supersedes_record_id=current_source.record_id,
                )
            )
            with self.assertRaisesRegex(
                ProductionBackupAuthorityConflictError,
                "Reviewed production backup authority digest",
            ):
                store.apply_production_backup_authority(
                    ProductionBackupAuthorityWriteEnvelope.model_validate(
                        policy_only_dry_run.model_dump(mode="json")
                        | {
                            "mode": "apply",
                            "reviewed_authority_digest": reviewed.result.authority_digest,
                        }
                    )
                )


class ProductionBackupAuthorityProjectionTests(unittest.TestCase):
    def test_product_config_driver_and_readiness_project_bounded_authority(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "launchplane.sqlite3"
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{database_path.as_posix()}"
            )
            store.ensure_schema()
            self.addCleanup(store.close)
            store.write_product_profile_record(_profile())
            dry_run = _dry_run_envelope()
            dry_result = store.apply_production_backup_authority(dry_run)
            store.apply_production_backup_authority(
                ProductionBackupAuthorityWriteEnvelope.model_validate(
                    dry_run.model_dump(mode="json")
                    | {
                        "mode": "apply",
                        "reviewed_authority_digest": dry_result.authority_digest,
                    }
                )
            )

            config_status = build_product_environment_config_status(
                record_store=store,
                product="example-product",
                environment="prod",
                action_allowed=lambda action, product, context, instances: (
                    action == "production_backup_authority.read"
                ),
            )
            self.assertEqual(config_status.production_backup_authorities[0].state, "ready")
            self.assertNotIn(
                "production-backup-target-example-prod-guest-r1",
                config_status.model_dump_json(),
            )
            denied_config_status = build_product_environment_config_status(
                record_store=store,
                product="example-product",
                environment="prod",
                action_allowed=lambda action, product, context, instances: False,
            )
            self.assertEqual(denied_config_status.production_backup_authorities, ())

            driver_view = build_driver_context_view(
                record_store=store,
                context_name="example-product",
                instance_name="prod",
                action_allowed=lambda action, product, context, instances: (
                    action == "production_backup_authority.read"
                ),
            )
            self.assertEqual(
                driver_view.drivers[0].production_backup_authorities[0].state,
                "ready",
            )
            self.assertNotIn(
                "production-backup-target-example-prod-guest-r1",
                driver_view.model_dump_json(),
            )
            denied_driver_view = build_driver_context_view(
                record_store=store,
                context_name="example-product",
                instance_name="prod",
                action_allowed=lambda action, product, context, instances: False,
            )
            self.assertEqual(
                denied_driver_view.drivers[0].production_backup_authorities,
                (),
            )

            readiness = build_product_operational_readiness_service_result(
                record_store=cast(ProductOperationalReadinessStore, store),
                profile=_profile(),
                lane=_profile().lanes[0],
                identity=_identity(
                    repository="example/example-product",
                    workflow_ref="example/example-product/.github/workflows/promote.yml@refs/heads/main",
                    job_workflow_ref=(
                        "example/launchplane/.github/workflows/reusable-promote.yml@"
                        "0123456789abcdef0123456789abcdef01234567"
                    ),
                    event_name="workflow_dispatch",
                    environment="prod",
                    repository_id="1001",
                    repository_owner_id="1000",
                ),
                requested_action="verireel_prod_promotion.execute",
                requested_artifact_id="",
                expected_current_artifact_id="",
                generated_at="2026-09-03T00:00:00Z",
            )
            backup_dimension = next(
                dimension
                for dimension in readiness.dimensions
                if dimension.dimension == "production_backup_policy"
            )
            self.assertEqual(backup_dimension.state, "ready")


if __name__ == "__main__":
    unittest.main()
