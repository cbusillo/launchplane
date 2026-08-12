import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.deploy_target import DeployTargetCategory, ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord, DokployTargetType
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.provider_target_backfill import backfill_provider_targets


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
    updated_at: str = "2026-04-21T18:32:00Z",
    source_label: str = "test:provider-target",
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
        updated_at=updated_at,
        source_label=source_label,
    )


class ProviderTargetBackfillTests(unittest.TestCase):
    def test_dry_run_reports_would_create_without_writing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_dokploy_target_record(_dokploy_target_record())
            store.write_dokploy_target_id_record(_dokploy_target_id_record())

            result = backfill_provider_targets(store)
            physical_records = store.list_physical_provider_target_records()
            store.close()

        self.assertTrue(result.ok)
        self.assertFalse(result.applied)
        self.assertEqual(result.counts, {"would-create": 1})
        self.assertIsNotNone(result.items[0].projected_record)
        assert result.items[0].projected_record is not None
        self.assertEqual(result.items[0].projected_record.target_id, "app-syo-prod")
        self.assertEqual(physical_records, ())

    def test_apply_creates_missing_rows_and_second_apply_skips_existing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_dokploy_target_record(_dokploy_target_record())
            store.write_dokploy_target_id_record(_dokploy_target_id_record())

            first_result = backfill_provider_targets(store, apply=True)
            first_physical = store.read_provider_target_record(
                context_name="syo", instance_name="prod"
            )
            second_result = backfill_provider_targets(store, apply=True)
            second_physical = store.read_provider_target_record(
                context_name="syo", instance_name="prod"
            )
            store.close()

        self.assertTrue(first_result.ok)
        self.assertEqual(first_result.counts, {"created": 1})
        self.assertEqual(first_physical.target_id, "app-syo-prod")
        self.assertTrue(second_result.ok)
        self.assertEqual(second_result.counts, {"skipped-exists": 1})
        self.assertEqual(second_physical, first_physical)

    def test_apply_reports_cross_route_identity_conflict(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_provider_target_record(
                _provider_target_record(context="legacy", instance="prod")
            )
            store.write_dokploy_target_record(_dokploy_target_record(context="canonical"))
            store.write_dokploy_target_id_record(_dokploy_target_id_record(context="canonical"))

            result = backfill_provider_targets(
                store,
                apply=True,
                context_name="canonical",
            )
            physical_records = store.list_physical_provider_target_records()
            store.close()

        self.assertFalse(result.ok)
        self.assertEqual(result.counts, {"skipped-conflict": 1})
        self.assertIn("bound to another route", result.items[0].detail)
        self.assertEqual(len(physical_records), 1)

    def test_apply_does_not_churn_matching_physical_row(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_dokploy_target_record(_dokploy_target_record())
            store.write_dokploy_target_id_record(_dokploy_target_id_record())
            existing_record = _provider_target_record(
                updated_at="2026-04-01T00:00:00Z",
                source_label="test:existing-row",
            )
            store.write_provider_target_record(existing_record)

            result = backfill_provider_targets(store, apply=True)
            physical_record = store.read_provider_target_record(
                context_name="syo", instance_name="prod"
            )
            store.close()

        self.assertTrue(result.ok)
        self.assertEqual(result.counts, {"skipped-exists": 1})
        self.assertEqual(physical_record.updated_at, "2026-04-01T00:00:00Z")
        self.assertEqual(physical_record.source_label, "test:existing-row")

    def test_reports_incomplete_pairs_without_writing(self) -> None:
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

            result = backfill_provider_targets(store, apply=True)
            physical_records = store.list_physical_provider_target_records()
            store.close()

        self.assertTrue(result.ok)
        self.assertEqual(result.warning_count, 2)
        self.assertEqual(result.counts, {"skipped-incomplete": 2})
        self.assertEqual(physical_records, ())

    def test_reports_conflict_and_never_overwrites(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_dokploy_target_record(_dokploy_target_record())
            store.write_dokploy_target_id_record(_dokploy_target_id_record())
            stale_record = _provider_target_record(target_id="stale-app-syo-prod")
            store.write_provider_target_record(stale_record)

            result = backfill_provider_targets(store, apply=True)
            physical_record = store.read_provider_target_record(
                context_name="syo", instance_name="prod"
            )
            store.close()

        self.assertFalse(result.ok)
        self.assertEqual(result.counts, {"skipped-conflict": 1})
        self.assertEqual(physical_record.target_id, "stale-app-syo-prod")

    def test_apply_refuses_all_writes_when_any_route_is_blocked(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_dokploy_target_record(_dokploy_target_record(context="create-me"))
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(context="create-me", target_id="app-create-me-prod")
            )
            store.write_dokploy_target_record(_dokploy_target_record(context="conflict"))
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(context="conflict", target_id="app-conflict-prod")
            )
            store.write_provider_target_record(
                _provider_target_record(context="conflict", target_id="stale-conflict-prod")
            )

            result = backfill_provider_targets(store, apply=True)
            physical_records = store.list_physical_provider_target_records()
            store.close()

        self.assertFalse(result.ok)
        self.assertEqual(result.counts, {"skipped-conflict": 1, "would-create": 1})
        self.assertEqual(
            [(record.context, record.target_id) for record in physical_records],
            [("conflict", "stale-conflict-prod")],
        )

    def test_existing_dokploy_physical_row_with_missing_source_record_blocks(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_provider_target_record(_provider_target_record())

            result = backfill_provider_targets(store, apply=True)
            store.close()

        self.assertFalse(result.ok)
        self.assertEqual(result.blocked_count, 1)
        self.assertEqual(result.counts, {"skipped-incomplete": 1})

    def test_reports_unsupported_provider_row(self) -> None:
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

            result = backfill_provider_targets(store, apply=True)
            physical_records = store.list_physical_provider_target_records()
            store.close()

        self.assertTrue(result.ok)
        self.assertEqual(result.counts, {"unsupported-provider": 1})
        self.assertEqual(physical_records[0].provider_id, "future-provider")

    def test_filters_scope_backfill_and_block_zero_matches(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_dokploy_target_record(_dokploy_target_record(context="syo"))
            store.write_dokploy_target_id_record(_dokploy_target_id_record(context="syo"))
            store.write_dokploy_target_record(_dokploy_target_record(context="verireel"))
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(context="verireel", target_id="app-verireel-prod")
            )

            scoped_result = backfill_provider_targets(
                store, apply=True, context_name="syo", provider_id="dokploy"
            )
            zero_match_result = backfill_provider_targets(
                store, apply=True, context_name="missing-context"
            )
            physical_records = store.list_physical_provider_target_records()
            store.close()

        self.assertTrue(scoped_result.ok)
        self.assertEqual(scoped_result.counts, {"created": 1})
        self.assertFalse(zero_match_result.ok)
        self.assertEqual(zero_match_result.counts, {"no_matching_routes": 1})
        self.assertEqual(
            [(record.context, record.target_id) for record in physical_records],
            [("syo", "app-syo-prod")],
        )

    def test_instance_filter_scopes_backfill(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_dokploy_target_record(_dokploy_target_record(instance="prod"))
            store.write_dokploy_target_id_record(_dokploy_target_id_record(instance="prod"))
            store.write_dokploy_target_record(
                _dokploy_target_record(instance="testing", target_name="syo-testing")
            )
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(instance="testing", target_id="app-syo-testing")
            )

            result = backfill_provider_targets(store, apply=True, instance_name="testing")
            physical_records = store.list_physical_provider_target_records()
            store.close()

        self.assertTrue(result.ok)
        self.assertEqual(result.counts, {"created": 1})
        self.assertEqual(
            [(record.instance, record.target_id) for record in physical_records],
            [("testing", "app-syo-testing")],
        )

    def test_cli_dry_run_emits_json_without_writing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            database_url = _sqlite_database_url(database_path)
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_dokploy_target_record(_dokploy_target_record())
            store.write_dokploy_target_id_record(_dokploy_target_id_record())
            store.close()

            dry_run = CliRunner().invoke(
                main,
                [
                    "storage",
                    "provider-target-backfill",
                    "--database-url",
                    database_url,
                ],
            )
            store = PostgresRecordStore(database_url=database_url)
            physical_records = store.list_physical_provider_target_records()
            store.close()

        self.assertEqual(dry_run.exit_code, 0, dry_run.output)
        dry_run_payload = json.loads(dry_run.output)
        self.assertFalse(dry_run_payload["applied"])
        self.assertEqual(dry_run_payload["counts"], {"would-create": 1})
        self.assertEqual(physical_records, ())

    def test_cli_rejects_apply_option_before_writing(self) -> None:
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
                    "provider-target-backfill",
                    "--database-url",
                    database_url,
                    "--apply",
                ],
            )
            store = PostgresRecordStore(database_url=database_url)
            physical_records = store.list_physical_provider_target_records()
            store.close()

        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("No such option", result.output)
        self.assertIn("--apply", result.output)
        self.assertEqual(physical_records, ())

    def test_cli_help_describes_report_only_mode(self) -> None:
        result = CliRunner().invoke(
            main,
            ["storage", "provider-target-backfill", "--help"],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("--apply", result.output)
        self.assertIn("--provider-id", result.output)

    def test_cli_dry_run_exits_nonzero_on_conflicts(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            database_url = _sqlite_database_url(database_path)
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_dokploy_target_record(_dokploy_target_record())
            store.write_dokploy_target_id_record(_dokploy_target_id_record())
            store.write_provider_target_record(
                _provider_target_record(target_id="stale-app-syo-prod")
            )
            store.close()

            result = CliRunner().invoke(
                main,
                [
                    "storage",
                    "provider-target-backfill",
                    "--database-url",
                    database_url,
                ],
            )

        self.assertEqual(result.exit_code, 1, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["counts"], {"skipped-conflict": 1})

    def test_cli_initializes_fresh_database_schema(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )

            result = CliRunner().invoke(
                main,
                [
                    "storage",
                    "provider-target-backfill",
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
