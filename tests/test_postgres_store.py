import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from click.testing import CliRunner
from sqlalchemy.exc import SQLAlchemyError

from control_plane.cli import main
from control_plane.contracts.artifact_identity import (
    ArtifactIdentityManifest,
    ArtifactImageReference,
)
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.idempotency_record import build_launchplane_idempotency_record_id
from control_plane.contracts.odoo_instance_override_record import OdooConfigParameterOverride
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.odoo_instance_override_record import OdooOverrideValue
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationRecord,
    PreviewPullRequestSummary,
)
from control_plane.contracts.preview_inventory_scan_record import PreviewInventoryScanRecord
from control_plane.contracts.preview_lifecycle_cleanup_record import (
    PreviewLifecycleCleanupRecord,
    PreviewLifecycleCleanupResult,
)
from control_plane.contracts.preview_lifecycle_plan_record import (
    PreviewLifecycleDesiredPreview,
    PreviewLifecyclePlanRecord,
)
from control_plane.contracts.preview_pr_feedback_record import PreviewPrFeedbackRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    PromotionRecord,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import (
    SecretAuditEvent,
    SecretBinding,
    SecretRecord,
    SecretVersion,
)
from control_plane.service_auth import GitHubHumanIdentity, LaunchplaneAuthzPolicy
from control_plane.service_human_auth import LaunchplaneHumanSession
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore

REPO_ROOT = Path(__file__).resolve().parents[1]


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def _alembic_config(database_url: str) -> AlembicConfig:
    config = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


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


def _artifact_manifest() -> ArtifactIdentityManifest:
    return ArtifactIdentityManifest(
        artifact_id="artifact-20260420-a1b2c3d4",
        source_commit="a1b2c3d4",
        enterprise_base_digest="sha256:enterprisebase123",
        image=ArtifactImageReference(
            repository="ghcr.io/cbusillo/odoo-tenant-opw",
            digest="sha256:image123",
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


def _dokploy_target_id_record(
    *, context: str = "opw", instance: str = "prod", target_id: str = "compose-123"
) -> DokployTargetIdRecord:
    return DokployTargetIdRecord(
        context=context,
        instance=instance,
        target_id=target_id,
        updated_at="2026-04-21T18:30:00Z",
        source_label="import:test",
    )


def _dokploy_target_record(*, context: str = "opw", instance: str = "prod") -> DokployTargetRecord:
    return DokployTargetRecord(
        context=context,
        instance=instance,
        target_type="compose",
        target_name=f"{context}-{instance}",
        source_git_ref="origin/main",
        source_type="git",
        custom_git_url="git@github.com:every/odoo-opw.git",
        custom_git_branch=instance,
        compose_path="./docker-compose.yml",
        domains=(f"https://{instance}.example.com",),
        updated_at="2026-04-21T18:30:00Z",
        source_label="import:test",
    )


def _runtime_environment_record(
    *,
    scope: str = "instance",
    context: str = "opw",
    instance: str = "local",
    env: dict[str, str | int | float | bool] | None = None,
) -> RuntimeEnvironmentRecord:
    return RuntimeEnvironmentRecord(
        scope=scope,
        context=context if scope != "global" else "",
        instance=instance if scope == "instance" else "",
        env=env or {"ODOO_DB_PASSWORD": "local-secret"},
        updated_at="2026-04-21T18:30:00Z",
        source_label="import:test",
    )


def _odoo_instance_override_record(
    *, context: str = "opw", instance: str = "prod"
) -> OdooInstanceOverrideRecord:
    return OdooInstanceOverrideRecord(
        context=context,
        instance=instance,
        config_parameters=(
            OdooConfigParameterOverride(
                key="web.base.url",
                value=OdooOverrideValue(
                    source="literal", value=f"https://{context}-{instance}.example.com"
                ),
            ),
        ),
        updated_at="2026-04-21T18:30:00Z",
        source_label="test",
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
        updated_by="launchplane-bootstrap",
    )


def _secret_version(*, version_id: str, secret_id: str, created_at: str) -> SecretVersion:
    return SecretVersion(
        version_id=version_id,
        secret_id=secret_id,
        created_at=created_at,
        created_by="launchplane-bootstrap",
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
        actor="launchplane-bootstrap",
        detail="Imported existing Dokploy secret",
        metadata={"source": "dokploy.env"},
    )


def _human_session(*, session_id: str = "session-1") -> LaunchplaneHumanSession:
    created_at = datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc)
    return LaunchplaneHumanSession(
        session_id=session_id,
        created_at=created_at,
        expires_at=created_at + timedelta(hours=12),
        identity=GitHubHumanIdentity(
            login="alice",
            github_id=123,
            name="Alice Operator",
            email="alice@example.com",
            organizations=frozenset({"shinycomputers"}),
            teams=frozenset({"shinycomputers/launchplane-admins"}),
            role="admin",
        ),
    )


class PostgresRecordStoreTests(unittest.TestCase):
    def test_alembic_baseline_creates_schema_used_by_record_store(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            alembic_command.upgrade(_alembic_config(database_url), "head")

            store = PostgresRecordStore(database_url=database_url)
            manifest = _artifact_manifest()
            store.write_artifact_manifest(manifest)
            loaded = store.read_artifact_manifest(manifest.artifact_id)
            store.close()

        self.assertEqual(loaded.artifact_id, manifest.artifact_id)
        self.assertEqual(loaded.image.digest, "sha256:image123")

    def test_alembic_baseline_downgrades_to_empty_schema(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            database_url = _sqlite_database_url(database_path)
            config = _alembic_config(database_url)

            alembic_command.upgrade(config, "head")
            alembic_command.downgrade(config, "base")

            store = PostgresRecordStore(database_url=database_url)
            with self.assertRaises(SQLAlchemyError):
                store.list_artifact_manifests()
            store.close()

    def test_artifact_manifests_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            manifest = _artifact_manifest()
            store.write_artifact_manifest(manifest)
            loaded = store.read_artifact_manifest(manifest.artifact_id)
            listed = store.list_artifact_manifests()

            self.assertEqual(loaded.artifact_id, manifest.artifact_id)
            self.assertEqual(loaded.image.repository, "ghcr.io/cbusillo/odoo-tenant-opw")
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0].image.digest, "sha256:image123")

    def test_artifacts_cli_uses_database_store_when_configured(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            input_path = Path(temporary_directory_name) / "artifact.json"
            input_path.write_text(
                _artifact_manifest().model_dump_json(),
                encoding="utf-8",
            )
            runner = CliRunner()

            write_result = runner.invoke(
                main,
                [
                    "artifacts",
                    "write",
                    "--database-url",
                    database_url,
                    "--input-file",
                    str(input_path),
                ],
            )
            self.assertEqual(write_result.exit_code, 0, write_result.output)

            show_result = runner.invoke(
                main,
                [
                    "artifacts",
                    "show",
                    "--database-url",
                    database_url,
                    "--artifact-id",
                    "artifact-20260420-a1b2c3d4",
                ],
            )
            self.assertEqual(show_result.exit_code, 0, show_result.output)
            self.assertIn("ghcr.io/cbusillo/odoo-tenant-opw", show_result.output)

    def test_dokploy_target_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            record = _dokploy_target_record()
            store.write_dokploy_target_record(record)
            loaded = store.read_dokploy_target_record(context_name="opw", instance_name="prod")
            listed = store.list_dokploy_target_records()

            self.assertEqual(loaded.target_name, "opw-prod")
            self.assertEqual(loaded.compose_path, "./docker-compose.yml")
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0].context, "opw")

    def test_idempotency_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            record = LaunchplaneIdempotencyRecord(
                record_id=build_launchplane_idempotency_record_id(
                    scope="every/verireel|workflow|repo:every/verireel:pull_request",
                    route_path="/v1/evidence/previews/generations",
                    request_token="preview-generation:verireel:verireel-testing:verireel:35:abcdef",
                ),
                scope="every/verireel|workflow|repo:every/verireel:pull_request",
                route_path="/v1/evidence/previews/generations",
                idempotency_key="preview-generation:verireel:verireel-testing:verireel:35:abcdef",
                request_fingerprint="fingerprint-123",
                response_status_code=202,
                response_trace_id="launchplane_req_123",
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

    def test_human_sessions_round_trip_and_delete(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            session = _human_session()
            store.write_session(session)
            loaded = store.read_session(session.session_id)
            store.delete_session(session.session_id)
            deleted = store.read_session(session.session_id)

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.identity.login, "alice")
        self.assertEqual(loaded.identity.role, "admin")
        self.assertEqual(loaded.identity.teams, frozenset({"shinycomputers/launchplane-admins"}))
        self.assertIsNone(deleted)

    def test_expired_human_session_reads_as_missing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()
            created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
            expired_session = LaunchplaneHumanSession(
                session_id="expired-session",
                created_at=created_at,
                expires_at=created_at + timedelta(minutes=1),
                identity=_human_session().identity,
            )

            store.write_session(expired_session)
            loaded = store.read_session(expired_session.session_id)

        self.assertIsNone(loaded)

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
            with patch(
                "control_plane.cli.PostgresRecordStore", return_value=postgres_store
            ) as store_class:
                result = runner.invoke(
                    main,
                    [
                        "storage",
                        "import-core-records",
                        "--state-dir",
                        temporary_directory_name,
                        "--database-url",
                        "postgresql://launchplane:test@db/launchplane",
                    ],
                )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        store_class.assert_called_once_with(
            database_url="postgresql://launchplane:test@db/launchplane"
        )
        postgres_store.ensure_schema.assert_called_once_with()
        postgres_store.import_core_records_from_filesystem.assert_called_once()
        self.assertIn('"deployments": 1', result.output)

    def test_write_and_read_deployment_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
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
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
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

    def test_preview_summaries_include_latest_generation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()

            preview = _preview_record(
                preview_id="preview-verireel-testing-verireel-pr-123",
                updated_at="2026-04-20T10:05:00Z",
                pr_number=123,
            )
            store.write_preview_record(preview)
            store.write_preview_generation_record(
                _preview_generation_record(
                    generation_id="preview-verireel-testing-verireel-pr-123-generation-0001",
                    preview_id=preview.preview_id,
                )
            )
            store.write_preview_generation_record(
                _preview_generation_record(
                    generation_id="preview-verireel-testing-verireel-pr-123-generation-0002",
                    preview_id=preview.preview_id,
                ).model_copy(
                    update={
                        "sequence": 2,
                        "requested_at": "2026-04-20T10:06:00Z",
                        "ready_at": "2026-04-20T10:08:00Z",
                        "finished_at": "2026-04-20T10:08:00Z",
                        "artifact_id": "artifact-verireel-pr-123-bbbbbbbb",
                    }
                )
            )

            summary = store.read_preview_summary(preview_id=preview.preview_id)
            listed_summaries = store.list_preview_summaries(
                context_name="verireel-testing",
                anchor_repo="verireel",
                generation_limit=1,
            )
            store.close()

        self.assertEqual(summary.preview.preview_id, preview.preview_id)
        self.assertEqual(
            summary.latest_generation.generation_id,
            "preview-verireel-testing-verireel-pr-123-generation-0002",
        )
        self.assertEqual(len(summary.recent_generations), 2)
        self.assertEqual(len(listed_summaries), 1)
        self.assertEqual(len(listed_summaries[0].recent_generations), 1)
        self.assertEqual(listed_summaries[0].latest_generation.sequence, 2)

    def test_preview_inventory_scan_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_preview_inventory_scan_record(
                PreviewInventoryScanRecord(
                    scan_id="preview-inventory-scan-verireel-testing-20260420T100500Z",
                    context="verireel-testing",
                    scanned_at="2026-04-20T10:05:00Z",
                    source="verireel-preview-inventory",
                    status="pass",
                    preview_count=2,
                    preview_slugs=("pr-123", "pr-124"),
                )
            )
            store.write_preview_inventory_scan_record(
                PreviewInventoryScanRecord(
                    scan_id="preview-inventory-scan-verireel-testing-20260420T100600Z",
                    context="verireel-testing",
                    scanned_at="2026-04-20T10:06:00Z",
                    source="verireel-preview-inventory",
                    status="pass",
                    preview_count=0,
                    preview_slugs=(),
                )
            )
            listed_records = store.list_preview_inventory_scan_records(
                context_name="verireel-testing",
                limit=1,
            )
            store.close()

        self.assertEqual(len(listed_records), 1)
        self.assertEqual(
            listed_records[0].scan_id,
            "preview-inventory-scan-verireel-testing-20260420T100600Z",
        )
        self.assertEqual(listed_records[0].preview_count, 0)

    def test_authz_policy_records_round_trip(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["launchplane_service_deploy.execute"],
                    }
                ]
            }
        )
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_authz_policy_record(
                LaunchplaneAuthzPolicyRecord(
                    record_id="launchplane-authz-policy-20260420T100500Z-test",
                    status="active",
                    source="test",
                    updated_at="2026-04-20T10:05:00Z",
                    policy=policy,
                )
            )
            listed_records = store.list_authz_policy_records(status="active", limit=1)
            store.close()

        self.assertEqual(len(listed_records), 1)
        self.assertEqual(listed_records[0].record_id, "launchplane-authz-policy-20260420T100500Z-test")
        self.assertEqual(listed_records[0].policy.github_actions[0].repository, "cbusillo/launchplane")

    def test_preview_lifecycle_plan_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_preview_lifecycle_plan_record(
                PreviewLifecyclePlanRecord(
                    plan_id="preview-lifecycle-plan-verireel-testing-20260420T100500Z",
                    product="verireel",
                    context="verireel-testing",
                    planned_at="2026-04-20T10:05:00Z",
                    source="preview-janitor",
                    status="pass",
                    inventory_scan_id="preview-inventory-scan-verireel-testing-20260420T100500Z",
                    desired_previews=(PreviewLifecycleDesiredPreview(preview_slug="pr-123"),),
                    desired_slugs=("pr-123",),
                    actual_slugs=("pr-122", "pr-123"),
                    keep_slugs=("pr-123",),
                    orphaned_slugs=("pr-122",),
                )
            )
            listed_records = store.list_preview_lifecycle_plan_records(
                context_name="verireel-testing",
                limit=1,
            )
            store.close()

        self.assertEqual(len(listed_records), 1)
        self.assertEqual(
            listed_records[0].plan_id,
            "preview-lifecycle-plan-verireel-testing-20260420T100500Z",
        )
        self.assertEqual(listed_records[0].orphaned_slugs, ("pr-122",))

    def test_preview_desired_state_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_preview_desired_state_record(
                PreviewDesiredStateRecord(
                    desired_state_id="preview-desired-state-verireel-testing-20260420T100500Z",
                    product="verireel",
                    context="verireel-testing",
                    source="launchplane-preview-lifecycle",
                    discovered_at="2026-04-20T10:05:00Z",
                    repository="every/verireel",
                    label="preview",
                    anchor_repo="verireel",
                    status="pass",
                    desired_count=1,
                    desired_previews=(PreviewLifecycleDesiredPreview(preview_slug="pr-123"),),
                )
            )
            listed_records = store.list_preview_desired_state_records(
                context_name="verireel-testing",
                limit=1,
            )
            store.close()

        self.assertEqual(len(listed_records), 1)
        self.assertEqual(
            listed_records[0].desired_state_id,
            "preview-desired-state-verireel-testing-20260420T100500Z",
        )
        self.assertEqual(listed_records[0].desired_count, 1)
        self.assertEqual(listed_records[0].desired_previews[0].preview_slug, "pr-123")

    def test_preview_lifecycle_cleanup_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_preview_lifecycle_cleanup_record(
                PreviewLifecycleCleanupRecord(
                    cleanup_id="preview-lifecycle-cleanup-verireel-testing-20260420T100500Z",
                    product="verireel",
                    context="verireel-testing",
                    plan_id="preview-lifecycle-plan-verireel-testing-20260420T100500Z",
                    inventory_scan_id="preview-inventory-scan-verireel-testing-20260420T100500Z",
                    requested_at="2026-04-20T10:05:00Z",
                    source="preview-janitor",
                    apply=True,
                    status="pass",
                    planned_slugs=("pr-122",),
                    destroyed_slugs=("pr-122",),
                    results=(
                        PreviewLifecycleCleanupResult(
                            preview_slug="pr-122",
                            anchor_repo="verireel",
                            anchor_pr_number=122,
                            status="destroyed",
                            application_name="ver-preview-pr-122-app",
                            application_id="app-122",
                            preview_url="https://pr-122.preview.example",
                        ),
                    ),
                )
            )
            listed_records = store.list_preview_lifecycle_cleanup_records(
                context_name="verireel-testing",
                limit=1,
            )
            store.close()

        self.assertEqual(len(listed_records), 1)
        self.assertEqual(
            listed_records[0].cleanup_id,
            "preview-lifecycle-cleanup-verireel-testing-20260420T100500Z",
        )
        self.assertEqual(
            listed_records[0].plan_id,
            "preview-lifecycle-plan-verireel-testing-20260420T100500Z",
        )
        self.assertEqual(listed_records[0].destroyed_slugs, ("pr-122",))
        self.assertEqual(listed_records[0].results[0].application_id, "app-122")

    def test_preview_pr_feedback_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_preview_pr_feedback_record(
                PreviewPrFeedbackRecord(
                    feedback_id="preview-pr-feedback-verireel-testing-pr-123-20260420T100800Z",
                    product="verireel",
                    context="verireel-testing",
                    source="preview-control-plane",
                    requested_at="2026-04-20T10:08:00Z",
                    repository="every/verireel",
                    anchor_repo="verireel",
                    anchor_pr_number=123,
                    anchor_pr_url="https://github.com/every/verireel/pull/123",
                    status="ready",
                    marker="<!-- verireel-preview-control -->",
                    comment_markdown="<!-- verireel-preview-control -->\nPreview ready.",
                    preview_url="https://pr-123.preview.example",
                    immutable_image_reference="ghcr.io/every/verireel:pr-123-a1b2c3d4",
                    refresh_image_reference="ghcr.io/every/verireel:preview-pr-123",
                    revision="a1b2c3d4",
                    run_url="https://github.com/every/verireel/actions/runs/123",
                    delivery_status="delivered",
                    delivery_action="updated_comment",
                    comment_id=456,
                    comment_url="https://github.com/every/verireel/pull/123#issuecomment-456",
                )
            )
            listed_records = store.list_preview_pr_feedback_records(
                context_name="verireel-testing",
                limit=1,
            )
            store.close()

        self.assertEqual(len(listed_records), 1)
        self.assertEqual(
            listed_records[0].feedback_id,
            "preview-pr-feedback-verireel-testing-pr-123-20260420T100800Z",
        )
        self.assertEqual(listed_records[0].delivery_status, "delivered")
        self.assertEqual(listed_records[0].comment_id, 456)

    def test_write_and_list_dokploy_target_id_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(context="opw", instance="prod", target_id="compose-123")
            )
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(context="cm", instance="testing", target_id="compose-456")
            )
            loaded_record = store.read_dokploy_target_id_record(
                context_name="opw", instance_name="prod"
            )
            listed_records = store.list_dokploy_target_id_records()
            store.close()

        self.assertEqual(loaded_record.target_id, "compose-123")
        self.assertEqual(
            [(record.context, record.instance) for record in listed_records],
            [("cm", "testing"), ("opw", "prod")],
        )

    def test_write_and_list_runtime_environment_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_runtime_environment_record(
                _runtime_environment_record(
                    scope="global", env={"ODOO_MASTER_PASSWORD": "shared-master"}
                )
            )
            store.write_runtime_environment_record(
                _runtime_environment_record(
                    scope="instance",
                    context="opw",
                    instance="local",
                    env={"ODOO_DB_PASSWORD": "local-secret"},
                )
            )
            listed_records = store.list_runtime_environment_records()
            store.close()

        self.assertEqual(
            [(record.scope, record.context, record.instance) for record in listed_records],
            [("global", "", ""), ("instance", "opw", "local")],
        )

    def test_read_lane_summary_uses_repository_queries_for_gui_state(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_environment_inventory(_inventory_record())
            store.write_deployment_record(
                _deployment_record(
                    record_id="deployment-20260420T153000Z-opw-testing",
                    started_at="2026-04-20T15:30:00Z",
                    finished_at="2026-04-20T15:32:00Z",
                )
            )
            store.write_release_tuple_record(_release_tuple_record())
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(context="opw", instance="testing")
            )
            store.write_dokploy_target_record(
                _dokploy_target_record(context="opw", instance="testing")
            )
            store.write_runtime_environment_record(
                _runtime_environment_record(
                    scope="global",
                    env={"ODOO_MASTER_PASSWORD": "shared-master"},
                )
            )
            store.write_runtime_environment_record(
                _runtime_environment_record(
                    scope="context",
                    context="opw",
                    env={"ODOO_DB_USER": "opw"},
                )
            )
            store.write_runtime_environment_record(
                _runtime_environment_record(
                    scope="instance",
                    context="opw",
                    instance="testing",
                    env={"ODOO_DB_NAME": "opw-testing"},
                )
            )
            store.write_odoo_instance_override_record(
                _odoo_instance_override_record(context="opw", instance="testing")
            )
            store.write_secret_binding(
                _secret_binding(
                    binding_id="binding-dokploy-token",
                    secret_id="secret-dokploy-token",
                    updated_at="2026-04-20T18:07:00Z",
                )
            )

            summary = store.read_lane_summary(context_name="opw", instance_name="testing")
            store.close()

        self.assertEqual(summary.context, "opw")
        self.assertEqual(summary.instance, "testing")
        self.assertEqual(
            summary.inventory.artifact_identity.artifact_id, "artifact-20260420-a1b2c3d4"
        )
        self.assertEqual(summary.release_tuple.channel, "testing")
        self.assertEqual(
            summary.latest_deployment.record_id, "deployment-20260420T153000Z-opw-testing"
        )
        self.assertIsNone(summary.latest_promotion)
        self.assertIsNone(summary.latest_backup_gate)
        self.assertEqual(summary.dokploy_target_id.target_id, "compose-123")
        self.assertEqual(summary.dokploy_target.target_name, "opw-testing")
        self.assertEqual(
            [
                (record.scope, record.context, record.instance)
                for record in summary.runtime_environment_records
            ],
            [("global", "", ""), ("context", "opw", ""), ("instance", "opw", "testing")],
        )
        self.assertEqual(summary.odoo_instance_override.config_parameters[0].key, "web.base.url")
        self.assertEqual(summary.secret_bindings[0].binding_key, "DOKPLOY_TOKEN")

    def test_write_read_and_list_odoo_instance_override_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_odoo_instance_override_record(
                _odoo_instance_override_record(context="opw", instance="prod")
            )
            store.write_odoo_instance_override_record(
                _odoo_instance_override_record(context="cm", instance="testing")
            )
            loaded_record = store.read_odoo_instance_override_record(
                context_name="opw", instance_name="prod"
            )
            listed_records = store.list_odoo_instance_override_records()
            store.close()

        self.assertEqual(loaded_record.config_parameters[0].key, "web.base.url")
        self.assertEqual(
            [(record.context, record.instance) for record in listed_records],
            [("cm", "testing"), ("opw", "prod")],
        )

    def test_secret_records_round_trip_and_find_latest(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
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
            listed_records = store.list_secret_records(
                integration="dokploy", context_name="opw", instance_name="testing"
            )
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
            self.assertEqual(
                store.read_secret_record(newer_record.secret_id).current_version_id,
                "secret-version-0002",
            )
            self.assertEqual(
                store.read_secret_version("secret-version-0002").secret_id, newer_record.secret_id
            )
            self.assertEqual(
                [record.secret_id for record in listed_records], [newer_record.secret_id]
            )
            self.assertEqual(
                [version.version_id for version in listed_versions],
                ["secret-version-0002", "secret-version-0001"],
            )
            self.assertEqual(
                [item.binding_id for item in listed_bindings], ["binding-dokploy-token"]
            )
            self.assertEqual(
                [item.event_id for item in listed_events], ["audit-secret-import-0001"]
            )
            store.close()

    def test_import_core_records_from_filesystem(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            filesystem_store = FilesystemRecordStore(
                state_dir=Path(temporary_directory_name) / "state"
            )
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
            filesystem_store.write_artifact_manifest(_artifact_manifest())
            filesystem_store.write_odoo_instance_override_record(_odoo_instance_override_record())
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
            filesystem_store.write_preview_inventory_scan_record(
                PreviewInventoryScanRecord(
                    scan_id="preview-inventory-scan-verireel-testing-20260420T100500Z",
                    context="verireel-testing",
                    scanned_at="2026-04-20T10:05:00Z",
                    source="verireel-preview-inventory",
                    status="pass",
                    preview_count=1,
                    preview_slugs=("pr-123",),
                )
            )
            filesystem_store.write_preview_desired_state_record(
                PreviewDesiredStateRecord(
                    desired_state_id="preview-desired-state-verireel-testing-20260420T100550Z",
                    product="verireel",
                    context="verireel-testing",
                    source="launchplane-preview-lifecycle",
                    discovered_at="2026-04-20T10:05:50Z",
                    repository="every/verireel",
                    label="preview",
                    anchor_repo="verireel",
                    status="pass",
                    desired_count=1,
                    desired_previews=(PreviewLifecycleDesiredPreview(preview_slug="pr-123"),),
                )
            )
            filesystem_store.write_preview_lifecycle_plan_record(
                PreviewLifecyclePlanRecord(
                    plan_id="preview-lifecycle-plan-verireel-testing-20260420T100600Z",
                    product="verireel",
                    context="verireel-testing",
                    planned_at="2026-04-20T10:06:00Z",
                    source="preview-janitor",
                    status="pass",
                    inventory_scan_id="preview-inventory-scan-verireel-testing-20260420T100500Z",
                    desired_previews=(PreviewLifecycleDesiredPreview(preview_slug="pr-123"),),
                    desired_slugs=("pr-123",),
                    actual_slugs=("pr-123",),
                    keep_slugs=("pr-123",),
                )
            )
            filesystem_store.write_preview_lifecycle_cleanup_record(
                PreviewLifecycleCleanupRecord(
                    cleanup_id="preview-lifecycle-cleanup-verireel-testing-20260420T100700Z",
                    product="verireel",
                    context="verireel-testing",
                    plan_id="preview-lifecycle-plan-verireel-testing-20260420T100600Z",
                    inventory_scan_id="preview-inventory-scan-verireel-testing-20260420T100500Z",
                    requested_at="2026-04-20T10:07:00Z",
                    source="preview-janitor",
                    apply=False,
                    status="report_only",
                )
            )
            filesystem_store.write_preview_pr_feedback_record(
                PreviewPrFeedbackRecord(
                    feedback_id="preview-pr-feedback-verireel-testing-pr-123-20260420T100800Z",
                    product="verireel",
                    context="verireel-testing",
                    source="preview-control-plane",
                    requested_at="2026-04-20T10:08:00Z",
                    repository="every/verireel",
                    anchor_repo="verireel",
                    anchor_pr_number=123,
                    anchor_pr_url="https://github.com/every/verireel/pull/123",
                    status="ready",
                    marker="<!-- verireel-preview-control -->",
                    comment_markdown="<!-- verireel-preview-control -->\nPreview ready.",
                    delivery_status="skipped",
                    error_message="Launchplane runtime records do not expose GITHUB_TOKEN for this context",
                )
            )
            filesystem_store.write_release_tuple_record(_release_tuple_record())

            counts = store.import_core_records_from_filesystem(filesystem_store)
            self.assertEqual(
                counts,
                {
                    "artifacts": 1,
                    "authz_policies": 0,
                    "backup_gates": 1,
                    "deployments": 1,
                    "promotions": 1,
                    "inventory": 1,
                    "odoo_instance_overrides": 1,
                    "preview_records": 1,
                    "preview_generations": 1,
                    "preview_desired_states": 1,
                    "preview_inventory_scans": 1,
                    "preview_lifecycle_cleanups": 1,
                    "preview_lifecycle_plans": 1,
                    "preview_pr_feedback": 1,
                    "release_tuples": 1,
                },
            )
            self.assertEqual(
                store.read_promotion_record(
                    "promotion-20260420T160500Z-opw-testing-to-prod"
                ).to_instance,
                "prod",
            )
            self.assertEqual(
                store.read_preview_generation_record(
                    "preview-verireel-testing-verireel-pr-123-generation-0001"
                ).state,
                "ready",
            )
            self.assertEqual(
                store.list_preview_inventory_scan_records(
                    context_name="verireel-testing",
                    limit=1,
                )[0].scan_id,
                "preview-inventory-scan-verireel-testing-20260420T100500Z",
            )
            self.assertEqual(
                store.list_preview_desired_state_records(
                    context_name="verireel-testing",
                    limit=1,
                )[0].desired_state_id,
                "preview-desired-state-verireel-testing-20260420T100550Z",
            )
            self.assertEqual(
                store.list_preview_lifecycle_plan_records(
                    context_name="verireel-testing",
                    limit=1,
                )[0].plan_id,
                "preview-lifecycle-plan-verireel-testing-20260420T100600Z",
            )
            self.assertEqual(
                store.list_preview_lifecycle_cleanup_records(
                    context_name="verireel-testing",
                    limit=1,
                )[0].cleanup_id,
                "preview-lifecycle-cleanup-verireel-testing-20260420T100700Z",
            )
            self.assertEqual(
                store.list_preview_pr_feedback_records(
                    context_name="verireel-testing",
                    limit=1,
                )[0].feedback_id,
                "preview-pr-feedback-verireel-testing-pr-123-20260420T100800Z",
            )
            store.close()
