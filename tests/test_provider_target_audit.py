import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.deploy_target import DeployTargetCategory
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord, DokployTargetType
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.provider_target_audit import audit_provider_targets


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite:///{database_path}"


def _dokploy_target_record(
    *,
    context: str = "syo",
    instance: str = "prod",
    target_type: str = "application",
    target_name: str = "",
    project_name: str = "syo-project",
) -> DokployTargetRecord:
    return DokployTargetRecord(
        context=context,
        instance=instance,
        project_name=project_name,
        target_type=cast(DokployTargetType, target_type),
        target_name=target_name or f"{context}-{instance}",
        source_git_ref="origin/main",
        source_type="git",
        custom_git_url="git@github.com:example/product.git",
        custom_git_branch=instance,
        compose_path="./docker-compose.yml",
        domains=(f"https://{instance}.example.com",),
        updated_at="2026-04-21T18:30:00Z",
        source_label="test:dokploy-target",
    )


def _dokploy_target_id_record(
    *,
    context: str = "syo",
    instance: str = "prod",
    target_id: str = "app-syo-prod",
) -> DokployTargetIdRecord:
    return DokployTargetIdRecord(
        context=context,
        instance=instance,
        target_id=target_id,
        updated_at="2026-04-21T18:31:00Z",
        source_label="test:dokploy-target-id",
    )


def _provider_target_record(
    *,
    context: str = "syo",
    instance: str = "prod",
    provider_id: str = "dokploy",
    target_category: str = "application",
    target_id: str = "app-syo-prod",
    display_name: str = "",
    provider_target_type: str = "application",
    provider_evidence: dict[str, str] | None = None,
) -> ProviderTargetRecord:
    return ProviderTargetRecord(
        context=context,
        instance=instance,
        provider_id=provider_id,
        target_category=cast(DeployTargetCategory, target_category),
        target_id=target_id,
        display_name=display_name or f"{context}-{instance}",
        provider_target_type=provider_target_type,
        provider_evidence=provider_evidence
        if provider_evidence is not None
        else {"project_name": "syo-project"},
        updated_at="2026-04-21T18:32:00Z",
        source_label="test:provider-target",
    )


class ProviderTargetAuditTests(unittest.TestCase):
    def test_audit_reports_complete_matching_physical_row(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_dokploy_target_record(_dokploy_target_record())
            store.write_dokploy_target_id_record(_dokploy_target_id_record())
            store.write_provider_target_record(_provider_target_record())

            result = audit_provider_targets(store)
            store.close()

        self.assertTrue(result.ok)
        self.assertEqual(result.counts, {"complete": 1})
        self.assertEqual(result.findings[0].status, "complete")

    def test_audit_reports_missing_provider_target(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_dokploy_target_record(_dokploy_target_record())
            store.write_dokploy_target_id_record(_dokploy_target_id_record())

            result = audit_provider_targets(store)
            store.close()

        self.assertFalse(result.ok)
        self.assertEqual(result.counts, {"missing_provider_target": 1})
        self.assertEqual(result.blocked_count, 1)

    def test_audit_reports_missing_halves(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_dokploy_target_record(
                _dokploy_target_record(context="target-only", instance="prod")
            )
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(context="id-only", instance="prod")
            )

            result = audit_provider_targets(store)
            store.close()

        self.assertFalse(result.ok)
        self.assertEqual(
            result.counts,
            {"missing_dokploy_target": 1, "missing_target_id": 1},
        )

    def test_audit_reports_identity_and_field_mismatches(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_dokploy_target_record(_dokploy_target_record())
            store.write_dokploy_target_id_record(_dokploy_target_id_record())
            store.write_provider_target_record(
                _provider_target_record(
                    target_category="compose",
                    target_id="stale-app-syo-prod",
                    display_name="stale-display-name",
                    provider_target_type="compose",
                    provider_evidence={"project_name": "stale-project"},
                )
            )

            result = audit_provider_targets(store)
            statuses = {finding.status for finding in result.findings}
            store.close()

        self.assertFalse(result.ok)
        self.assertEqual(
            statuses,
            {
                "identity_mismatch",
                "category_mismatch",
                "display_name_mismatch",
                "provider_target_type_mismatch",
                "provider_evidence_mismatch",
            },
        )

    def test_audit_warns_for_future_provider_row(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_provider_target_record(
                _provider_target_record(provider_id="future-provider")
            )

            result = audit_provider_targets(store)
            filtered = audit_provider_targets(store, provider_id="dokploy")
            store.close()

        self.assertTrue(result.ok)
        self.assertEqual(result.warning_count, 1)
        self.assertEqual(result.counts, {"future_provider": 1})
        self.assertFalse(filtered.ok)
        self.assertEqual(filtered.counts, {"no_matching_routes": 1})

    def test_audit_warns_for_future_provider_row_with_dokploy_pair(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_provider_target_record(
                _provider_target_record(provider_id="future-provider")
            )
            store.write_dokploy_target_record(_dokploy_target_record())
            store.write_dokploy_target_id_record(_dokploy_target_id_record())

            result = audit_provider_targets(store)
            store.close()

        self.assertTrue(result.ok)
        self.assertEqual(result.warning_count, 1)
        self.assertEqual(result.counts, {"future_provider": 1})

    def test_audit_blocks_zero_match_filters(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_dokploy_target_record(_dokploy_target_record())
            store.write_dokploy_target_id_record(_dokploy_target_id_record())

            result = audit_provider_targets(store, context_name="missing-context")
            store.close()

        self.assertFalse(result.ok)
        self.assertEqual(result.inspected_route_count, 0)
        self.assertEqual(result.counts, {"no_matching_routes": 1})

    def test_cli_emits_json_and_fails_on_blockers(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            database_url = _sqlite_database_url(database_path)
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_dokploy_target_record(_dokploy_target_record())
            store.write_dokploy_target_id_record(_dokploy_target_id_record())
            store.close()

            result = CliRunner().invoke(
                main,
                [
                    "storage",
                    "provider-target-audit",
                    "--database-url",
                    database_url,
                ],
            )

        self.assertEqual(result.exit_code, 1, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["inspected_route_count"], 1)
        self.assertEqual(payload["counts"], {"missing_provider_target": 1})

    def test_cli_initializes_fresh_database_schema(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )

            result = CliRunner().invoke(
                main,
                [
                    "storage",
                    "provider-target-audit",
                    "--database-url",
                    database_url,
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["inspected_route_count"], 0)


if __name__ == "__main__":
    unittest.main()
