import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.environment_inventory import EnvironmentInventory
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
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore


TABLE_PRIMARY_KEYS = {
    "harbor_backup_gates": ("record_id",),
    "harbor_deployments": ("record_id",),
    "harbor_promotions": ("record_id",),
    "harbor_inventory": ("context", "instance"),
    "harbor_preview_records": ("preview_id",),
    "harbor_preview_generations": ("generation_id",),
    "harbor_release_tuples": ("context", "channel"),
}


class _FakeCursor:
    def __init__(self, connection: "_FakeConnection") -> None:
        self.connection = connection
        self._rows: list[tuple[str]] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, query: str, params=None) -> "_FakeCursor":
        normalized_query = " ".join(query.split())
        bound_params = tuple(params or ())
        if normalized_query.startswith("CREATE TABLE") or normalized_query.startswith("CREATE INDEX"):
            self.connection.executed.append((normalized_query, bound_params))
            self._rows = []
            return self

        if normalized_query.startswith("INSERT INTO "):
            table_name = normalized_query.split()[2]
            column_fragment = normalized_query.split("(", 1)[1].split(")", 1)[0]
            columns = [column.strip() for column in column_fragment.split(",")]
            row = dict(zip(columns, bound_params, strict=True))
            primary_key_columns = TABLE_PRIMARY_KEYS[table_name]
            primary_key = tuple(row[column] for column in primary_key_columns)
            if len(primary_key) == 1:
                primary_key = primary_key[0]
            self.connection.tables.setdefault(table_name, {})[primary_key] = row
            self.connection.executed.append((normalized_query, bound_params))
            self._rows = []
            return self

        if normalized_query.startswith("SELECT payload::text FROM "):
            table_name = normalized_query.split("FROM ", 1)[1].split(" ", 1)[0]
            rows = list(self.connection.tables.get(table_name, {}).values())
            limit = None
            filter_params = list(bound_params)
            if " LIMIT %s" in normalized_query:
                limit = int(filter_params.pop())
            if " WHERE " in normalized_query:
                where_clause = normalized_query.split(" WHERE ", 1)[1].split(" ORDER BY ", 1)[0]
                predicates = [predicate.strip() for predicate in where_clause.split(" AND ")]
                for predicate, value in zip(predicates, filter_params, strict=True):
                    column_name = predicate.split("=", 1)[0].strip()
                    rows = [row for row in rows if row[column_name] == value]
            if " ORDER BY " in normalized_query:
                order_by_clause = normalized_query.split(" ORDER BY ", 1)[1].split(" LIMIT %s", 1)[0]
                order_clauses = [clause.strip() for clause in order_by_clause.split(",")]
                for clause in reversed(order_clauses):
                    column_name, direction = clause.rsplit(" ", 1)
                    rows.sort(key=lambda row: row[column_name.strip()], reverse=direction == "DESC")
            if limit is not None:
                rows = rows[:limit]
            self.connection.executed.append((normalized_query, bound_params))
            self._rows = [(str(row["payload"]),) for row in rows]
            return self

        raise AssertionError(f"Unexpected query in fake Postgres connection: {normalized_query}")

    def fetchone(self):
        if not self._rows:
            return None
        return self._rows[0]

    def fetchall(self):
        return list(self._rows)


class _FakeConnection:
    def __init__(self) -> None:
        self.tables: dict[str, dict[object, dict[str, object]]] = {}
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.commit_count = 0

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commit_count += 1


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


class PostgresRecordStoreTests(unittest.TestCase):
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
        connection = _FakeConnection()
        store = PostgresRecordStore(
            database_url="postgresql://harbor:test@db/harbor",
            connection_factory=lambda: connection,
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

        self.assertEqual(store.backend_name, "postgres")
        self.assertEqual(loaded_record.context, "opw")
        self.assertEqual(loaded_record.resolved_target.target_id, "compose-123")
        self.assertGreaterEqual(connection.commit_count, 2)

    def test_list_preview_records_filters_and_limits(self) -> None:
        connection = _FakeConnection()
        store = PostgresRecordStore(
            database_url="postgresql://harbor:test@db/harbor",
            connection_factory=lambda: connection,
        )

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

        self.assertEqual(
            [record.preview_id for record in listed_records],
            [
                "preview-verireel-testing-verireel-pr-102",
                "preview-verireel-testing-verireel-pr-103",
            ],
        )

    def test_import_core_records_from_filesystem(self) -> None:
        connection = _FakeConnection()
        store = PostgresRecordStore(
            database_url="postgresql://harbor:test@db/harbor",
            connection_factory=lambda: connection,
        )

        with TemporaryDirectory() as temporary_directory_name:
            filesystem_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            filesystem_store.write_backup_gate_record(_backup_gate_record())
            filesystem_store.write_deployment_record(
                _deployment_record(
                    record_id="deployment-20260420T153000Z-opw-testing",
                    started_at="2026-04-20T15:30:00Z",
                    finished_at="2026-04-20T15:32:00Z",
                )
            )
            filesystem_store.write_promotion_record(_promotion_record(record_id="promotion-20260420T160500Z-opw-testing-to-prod"))
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
