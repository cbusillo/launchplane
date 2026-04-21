import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.idempotency_record import HarborIdempotencyRecord
from control_plane.contracts.idempotency_record import build_harbor_idempotency_record_id
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationRecord,
    PreviewPullRequestSummary,
)
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    PromotionRecord,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.secret_record import SecretAuditEvent, SecretBinding, SecretRecord, SecretVersion
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def _deployment_record(*, record_id: str, started_at: str, finished_at: str) -> DeploymentRecord:
    return DeploymentRecord(
        record_id=record_id,
        artifact_identity=ArtifactIdentityReference(artifact_id="artifact-20260420-a1b2c3d4"),
        context="opw",
        instance="testing",
        source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
        resolved_target=ResolvedTargetEvidence(
            target_type="compose",
            target_id="compose-123",
            target_name="opw-testing",
        ),
        deploy=DeploymentEvidence(
            target_name="opw-testing",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            deployment_id="dokploy-1",
            status="pass",
            started_at=started_at,
            finished_at=finished_at,
        ),
    )


def _promotion_record(*, record_id: str) -> PromotionRecord:
    return PromotionRecord(
        record_id=record_id,
        artifact_identity=ArtifactIdentityReference(artifact_id="artifact-20260420-a1b2c3d4"),
        deployment_record_id="deployment-20260420T153000Z-opw-testing",
        backup_record_id="backup-opw-prod-20260420T160000Z",
        context="opw",
        from_instance="testing",
        to_instance="prod",
        deploy=DeploymentEvidence(
            target_name="opw-prod",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            deployment_id="dokploy-2",
            status="pass",
            started_at="2026-04-20T16:05:00Z",
            finished_at="2026-04-20T16:07:00Z",
        ),
    )


def _inventory_record() -> EnvironmentInventory:
    return EnvironmentInventory(
        context="opw",
        instance="testing",
        artifact_identity=ArtifactIdentityReference(artifact_id="artifact-20260420-a1b2c3d4"),
        source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
        deploy=DeploymentEvidence(
            target_name="opw-testing",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            deployment_id="dokploy-1",
            status="pass",
            started_at="2026-04-20T15:30:00Z",
            finished_at="2026-04-20T15:32:00Z",
        ),
        updated_at="2026-04-20T15:33:00Z",
        deployment_record_id="deployment-20260420T153000Z-opw-testing",
    )


def _preview_record(*, preview_id: str, updated_at: str, pr_number: int) -> PreviewRecord:
    return PreviewRecord(
        preview_id=preview_id,
        context="verireel-testing",
        anchor_repo="verireel",
        anchor_pr_number=pr_number,
        anchor_pr_url=f"https://github.com/every/verireel/pull/{pr_number}",
        preview_label=f"verireel/pr-{pr_number}",
        canonical_url=f"https://pr-{pr_number}.ver-preview.shinycomputers.com",
        state="active",
        created_at="2026-04-20T10:00:00Z",
        updated_at=updated_at,
        eligible_at=updated_at,
    )


def _preview_generation_record(*, generation_id: str, preview_id: str) -> PreviewGenerationRecord:
    return PreviewGenerationRecord(
        generation_id=generation_id,
        preview_id=preview_id,
        sequence=1,
        state="ready",
        requested_reason="external_preview_refresh",
        requested_at="2026-04-20T10:01:00Z",
        ready_at="2026-04-20T10:05:00Z",
        finished_at="2026-04-20T10:05:00Z",
        resolved_manifest_fingerprint="preview-manifest-123",
        artifact_id="ghcr.io/every/verireel-app:pr-123",
        anchor_summary=PreviewPullRequestSummary(
            repo="verireel",
            pr_number=123,
            head_sha="6b3c9d7e8f901234567890abcdef1234567890ab",
            pr_url="https://github.com/every/verireel/pull/123",
        ),
        deploy_status="pass",
        verify_status="pass",
        overall_health_status="pass",
    )


def _backup_gate_record() -> BackupGateRecord:
    return BackupGateRecord(
        record_id="backup-opw-prod-20260420T160000Z",
        context="opw",
        instance="prod",
        created_at="2026-04-20T16:00:00Z",
        source="prod-gate",
        status="pass",
        evidence={"snapshot": "opw-predeploy-20260420-160000"},
    )


def _release_tuple_record() -> ReleaseTupleRecord:
    return ReleaseTupleRecord(
        tuple_id="opw-testing-artifact-20260420-a1b2c3d4",
        context="opw",
        channel="testing",
        artifact_id="artifact-20260420-a1b2c3d4",
        repo_shas={"tenant-opw": "a1b2c3d4", "shared-addons": "abcdef1"},
        deployment_record_id="deployment-20260420T153000Z-opw-testing",
        provenance="ship",
        minted_at="2026-04-20T15:33:00Z",
    )


def _secret_record(*, secret_id: str, updated_at: str, current_version_id: str) -> SecretRecord:
    return SecretRecord(
        secret_id=secret_id,
        scope="context_instance",
        integration="dokploy",
        name="api_token",
        context="opw",
        instance="testing",
        description="Dokploy API token",
        current_version_id=current_version_id,
        created_at="2026-04-20T18:00:00Z",
        updated_at=updated_at,
        updated_by="harbor-bootstrap",
    )


def _secret_version(*, version_id: str, secret_id: str, created_at: str) -> SecretVersion:
    return SecretVersion(
        version_id=version_id,
        secret_id=secret_id,
        created_at=created_at,
        created_by="harbor-bootstrap",
        ciphertext="gAAAAABo-bootstrap-ciphertext",
    )


def _secret_binding(*, binding_id: str, secret_id: str, updated_at: str) -> SecretBinding:
    return SecretBinding(
        binding_id=binding_id,
        secret_id=secret_id,
        integration="dokploy",
        binding_key="DOKPLOY_TOKEN",
        context="opw",
        instance="testing",
        created_at="2026-04-20T18:00:00Z",
        updated_at=updated_at,
    )


def _secret_audit_event(*, event_id: str, secret_id: str, recorded_at: str) -> SecretAuditEvent:
    return SecretAuditEvent(
        event_id=event_id,
        secret_id=secret_id,
        event_type="imported",
        recorded_at=recorded_at,
        actor="harbor-bootstrap",
        detail="Imported existing Dokploy secret",
        metadata={"source": "dokploy.env"},
    )


class PostgresRecordStoreTests(unittest.TestCase):
    def test_idempotency_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "harbor.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            record = HarborIdempotencyRecord(
                record_id=build_harbor_idempotency_record_id(
                    scope="every/verireel|workflow|repo:every/verireel:pull_request",
                    route_path="/v1/evidence/previews/generations",
                    idempotency_key="preview-generation:verireel:verireel-testing:verireel:35:abcdef",
                ),
                scope="every/verireel|workflow|repo:every/verireel:pull_request",
                route_path="/v1/evidence/previews/generations",
                idempotency_key="preview-generation:verireel:verireel-testing:verireel:35:abcdef",
                request_fingerprint="fingerprint-123",
                response_status_code=202,
                response_trace_id="harbor_req_123",
                recorded_at="2026-04-21T01:00:00Z",
                response_payload={"status": "accepted", "records": {"preview_id": "preview-35"}},
            )

            store.write_idempotency_record(record)
            loaded = store.read_idempotency_record(
                scope=record.scope,
                route_path=record.route_path,
                idempotency_key=record.idempotency_key,
            )

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.request_fingerprint, "fingerprint-123")
            self.assertEqual(loaded.response_payload["records"]["preview_id"], "preview-35")

    def test_storage_import_core_records_command_reports_counts(self) -> None:
        runner = CliRunner()
        postgres_store = Mock()
        postgres_store.import_core_records_from_filesystem.return_value = {
            "backup_gates": 0,
            "deployments": 1,
            "promotions": 0,
            "inventory": 1,
            "preview_records": 0,
            "preview_generations": 0,
            "release_tuples": 0,
        }

        with TemporaryDirectory() as temporary_directory_name:
            with patch("control_plane.cli.PostgresRecordStore", return_value=postgres_store) as store_class:
                result = runner.invoke(
                    main,
                    [
                        "storage",
                        "import-core-records",
                        "--state-dir",
                        temporary_directory_name,
                        "--database-url",
                        "postgresql://harbor:test@db/harbor",
                    ],
                )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        store_class.assert_called_once_with(database_url="postgresql://harbor:test@db/harbor")
        postgres_store.ensure_schema.assert_called_once_with()
        postgres_store.import_core_records_from_filesystem.assert_called_once()
        self.assertIn('"deployments": 1', result.output)

    def test_write_and_read_deployment_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "harbor.sqlite3")
            )

            store.ensure_schema()
            store.write_deployment_record(
                _deployment_record(
                    record_id="deployment-20260420T153000Z-opw-testing",
                    started_at="2026-04-20T15:30:00Z",
                    finished_at="2026-04-20T15:32:00Z",
                )
            )
            loaded_record = store.read_deployment_record("deployment-20260420T153000Z-opw-testing")
            store.close()

        self.assertEqual(store.backend_name, "postgres")
        self.assertEqual(loaded_record.context, "opw")
        self.assertEqual(loaded_record.resolved_target.target_id, "compose-123")

    def test_list_preview_records_filters_and_limits(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "harbor.sqlite3")
            )
            store.ensure_schema()

            store.write_preview_record(
                _preview_record(
                    preview_id="preview-verireel-testing-verireel-pr-101",
                    updated_at="2026-04-20T10:01:00Z",
                    pr_number=101,
                )
            )
            store.write_preview_record(
                _preview_record(
                    preview_id="preview-verireel-testing-verireel-pr-102",
                    updated_at="2026-04-20T10:03:00Z",
                    pr_number=102,
                )
            )
            store.write_preview_record(
                _preview_record(
                    preview_id="preview-verireel-testing-verireel-pr-103",
                    updated_at="2026-04-20T10:02:00Z",
                    pr_number=103,
                )
            )

            listed_records = store.list_preview_records(
                context_name="verireel-testing",
                anchor_repo="verireel",
                limit=2,
            )
            store.close()

        self.assertEqual(
            [record.preview_id for record in listed_records],
            [
                "preview-verireel-testing-verireel-pr-102",
                "preview-verireel-testing-verireel-pr-103",
            ],
        )

    def test_secret_records_round_trip_and_find_latest(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "harbor.sqlite3")
            )
            store.ensure_schema()

            older_record = _secret_record(
                secret_id="secret-dokploy-token",
                updated_at="2026-04-20T18:05:00Z",
                current_version_id="secret-version-0001",
            )
            newer_record = _secret_record(
                secret_id="secret-dokploy-token",
                updated_at="2026-04-20T18:07:00Z",
                current_version_id="secret-version-0002",
            )
            older_version = _secret_version(
                version_id="secret-version-0001",
                secret_id=older_record.secret_id,
                created_at="2026-04-20T18:05:00Z",
            )
            newer_version = _secret_version(
                version_id="secret-version-0002",
                secret_id=newer_record.secret_id,
                created_at="2026-04-20T18:07:00Z",
            )
            binding = _secret_binding(
                binding_id="binding-dokploy-token",
                secret_id=newer_record.secret_id,
                updated_at="2026-04-20T18:07:00Z",
            )
            audit_event = _secret_audit_event(
                event_id="audit-secret-import-0001",
                secret_id=newer_record.secret_id,
                recorded_at="2026-04-20T18:07:30Z",
            )

            store.write_secret_record(older_record)
            store.write_secret_version(older_version)
            store.write_secret_record(newer_record)
            store.write_secret_version(newer_version)
            store.write_secret_binding(binding)
            store.write_secret_audit_event(audit_event)

            found_record = store.find_secret_record(
                scope="context_instance",
                integration="dokploy",
                name="api_token",
                context="opw",
                instance="testing",
            )
            listed_records = store.list_secret_records(integration="dokploy", context_name="opw", instance_name="testing")
            listed_versions = store.list_secret_versions(secret_id=newer_record.secret_id)
            listed_bindings = store.list_secret_bindings(
                integration="dokploy",
                context_name="opw",
                instance_name="testing",
            )
            listed_events = store.list_secret_audit_events(secret_id=newer_record.secret_id)
            self.assertIsNotNone(found_record)
            assert found_record is not None
            self.assertEqual(found_record.secret_id, newer_record.secret_id)
            self.assertEqual(store.read_secret_record(newer_record.secret_id).current_version_id, "secret-version-0002")
            self.assertEqual(store.read_secret_version("secret-version-0002").secret_id, newer_record.secret_id)
            self.assertEqual([record.secret_id for record in listed_records], [newer_record.secret_id])
            self.assertEqual([version.version_id for version in listed_versions], ["secret-version-0002", "secret-version-0001"])
            self.assertEqual([item.binding_id for item in listed_bindings], ["binding-dokploy-token"])
            self.assertEqual([item.event_id for item in listed_events], ["audit-secret-import-0001"])
            store.close()

    def test_import_core_records_from_filesystem(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "harbor.sqlite3")
            )
            store.ensure_schema()
            filesystem_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            filesystem_store.write_backup_gate_record(_backup_gate_record())
            filesystem_store.write_deployment_record(
                _deployment_record(
                    record_id="deployment-20260420T153000Z-opw-testing",
                    started_at="2026-04-20T15:30:00Z",
                    finished_at="2026-04-20T15:32:00Z",
                )
            )
            filesystem_store.write_promotion_record(
                _promotion_record(record_id="promotion-20260420T160500Z-opw-testing-to-prod")
            )
            filesystem_store.write_environment_inventory(_inventory_record())
            filesystem_store.write_preview_record(
                _preview_record(
                    preview_id="preview-verireel-testing-verireel-pr-123",
                    updated_at="2026-04-20T10:05:00Z",
                    pr_number=123,
                )
            )
            filesystem_store.write_preview_generation_record(
                _preview_generation_record(
                    generation_id="preview-verireel-testing-verireel-pr-123-generation-0001",
                    preview_id="preview-verireel-testing-verireel-pr-123",
                )
            )
            filesystem_store.write_release_tuple_record(_release_tuple_record())

            counts = store.import_core_records_from_filesystem(filesystem_store)
            self.assertEqual(
                counts,
                {
                    "backup_gates": 1,
                    "deployments": 1,
                    "promotions": 1,
                    "inventory": 1,
                    "preview_records": 1,
                    "preview_generations": 1,
                    "release_tuples": 1,
                },
            )
            self.assertEqual(
                store.read_promotion_record("promotion-20260420T160500Z-opw-testing-to-prod").to_instance,
                "prod",
            )
            self.assertEqual(
                store.read_preview_generation_record(
                    "preview-verireel-testing-verireel-pr-123-generation-0001"
                ).state,
                "ready",
            )
            store.close()
