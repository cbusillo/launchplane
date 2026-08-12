import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from unittest.mock import patch

from click import Command
from click.testing import CliRunner, Result

from control_plane.cli import main
from control_plane import secrets as control_plane_secrets
from control_plane.contracts.deploy_target import DeployedTargetReference, ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.dokploy import JsonObject
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.dokploy_target_adoption import (
    adopt_dokploy_target,
    create_dokploy_application_target,
    create_dokploy_compose_target,
    normalize_dokploy_domain_host,
    repair_dokploy_target_domain_authority,
)
from tests.support.cli import _allow_direct_db_mutation_argument


CLI_MAIN = cast(Command, main)


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def _assert_direct_db_mutation_rejected(test_case: unittest.TestCase, result: Result) -> None:
    test_case.assertNotEqual(result.exit_code, 0)
    test_case.assertIn("Direct local DB mutation is restricted", result.output)
    test_case.assertIn("--allow-direct-db-mutation", result.output)


def _write_dokploy_managed_secrets(*, store: PostgresRecordStore) -> None:
    control_plane_secrets.write_secret_value(
        record_store=store,
        scope="global",
        integration=control_plane_secrets.DOKPLOY_SECRET_INTEGRATION,
        name="host",
        plaintext_value="https://dokploy.example.invalid",
        binding_key="DOKPLOY_HOST",
        actor="test",
    )
    control_plane_secrets.write_secret_value(
        record_store=store,
        scope="global",
        integration=control_plane_secrets.DOKPLOY_SECRET_INTEGRATION,
        name="token",
        plaintext_value="token",
        binding_key="DOKPLOY_TOKEN",
        actor="test",
    )


class DokployTargetAdoptionTests(unittest.TestCase):
    @staticmethod
    def _live_domains(*hosts: str) -> tuple[JsonObject, ...]:
        return tuple({"host": host} for host in hosts)

    def test_repair_domain_authority_dry_run_preserves_metadata(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            target_record = DokployTargetRecord(
                context="synthetic-context",
                instance="synthetic-instance",
                target_type="application",
                target_name="synthetic-target",
                source_type="docker",
                custom_git_url="synthetic-source",
                env={"KEEP": "exact"},
                domains=("https://old.synthetic.invalid/path",),
                updated_at="2026-08-12T00:00:00Z",
                source_label="synthetic-source",
            )
            target_id_record = DokployTargetIdRecord(
                context=target_record.context,
                instance=target_record.instance,
                target_id="application-synthetic",
                updated_at=target_record.updated_at,
                source_label="synthetic-source",
            )
            store.write_dokploy_target_record(target_record)
            store.write_dokploy_target_id_record(target_id_record)
            provider_target = ProviderTargetRecord.from_dokploy_records(
                target_record=target_record,
                target_id_record=target_id_record,
            )
            store.write_provider_target_record(provider_target)
            result = repair_dokploy_target_domain_authority(
                record_store=store,
                host="https://provider.synthetic.invalid",
                token="synthetic-token",
                context=target_record.context,
                instance=target_record.instance,
                target_type="application",
                target_id=target_id_record.target_id,
                domains=("new.synthetic.invalid",),
                expected_current_provider_target=provider_target.to_deployed_target_reference(),
                fetch_target_payload=lambda *_args: {"applicationId": target_id_record.target_id},
                fetch_target_domains=lambda *_args: self._live_domains("new.synthetic.invalid"),
            )
            persisted = store.read_dokploy_target_record(
                context_name=target_record.context,
                instance_name=target_record.instance,
            )
            store.close()

        self.assertFalse(result.applied)
        self.assertEqual(result.target_record.domains, ("new.synthetic.invalid",))
        self.assertEqual(result.target_record.source_type, target_record.source_type)
        self.assertEqual(result.target_record.env, target_record.env)
        self.assertEqual(persisted, target_record)

    def test_repair_domain_authority_apply_compare_writes_only_allowed_fields(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            target_record = DokployTargetRecord(
                context="synthetic-context",
                instance="synthetic-instance",
                target_type="compose",
                target_name="synthetic-compose",
                source_type="docker",
                compose_path="synthetic-compose.yml",
                env={"KEEP": "exact"},
                domains=("https://old.synthetic.invalid/path",),
                updated_at="2026-08-12T00:00:00Z",
                source_label="synthetic-source",
            )
            target_id_record = DokployTargetIdRecord(
                context=target_record.context,
                instance=target_record.instance,
                target_id="compose-synthetic",
                updated_at=target_record.updated_at,
                source_label="synthetic-source",
            )
            provider_target = ProviderTargetRecord.from_dokploy_records(
                target_record=target_record,
                target_id_record=target_id_record,
            )
            store.write_dokploy_target_record(target_record)
            store.write_dokploy_target_id_record(target_id_record)
            store.write_provider_target_record(provider_target)
            result = repair_dokploy_target_domain_authority(
                record_store=store,
                host="https://provider.synthetic.invalid",
                token="synthetic-token",
                context=target_record.context,
                instance=target_record.instance,
                target_type="compose",
                target_id=target_id_record.target_id,
                domains=("new.synthetic.invalid",),
                expected_current_provider_target=provider_target.to_deployed_target_reference(),
                updated_at="2026-08-12T01:00:00Z",
                apply=True,
                fetch_target_payload=lambda *_args: {"composeId": target_id_record.target_id},
                fetch_target_domains=lambda *_args: self._live_domains("new.synthetic.invalid"),
            )
            persisted = store.read_dokploy_target_record(
                context_name=target_record.context,
                instance_name=target_record.instance,
            )
            persisted_target_id = store.read_dokploy_target_id_record(
                context_name=target_record.context,
                instance_name=target_record.instance,
            )
            persisted_provider_target = store.read_provider_target_record(
                context_name=target_record.context,
                instance_name=target_record.instance,
            )
            store.close()

        self.assertTrue(result.applied)
        self.assertEqual(persisted.domains, ("new.synthetic.invalid",))
        self.assertEqual(persisted.source_type, target_record.source_type)
        self.assertEqual(persisted.compose_path, target_record.compose_path)
        self.assertEqual(persisted.env, target_record.env)
        self.assertEqual(
            persisted.source_label, "service:dokploy-targets:setup:repair-domain-authority"
        )
        self.assertEqual(
            persisted_provider_target,
            ProviderTargetRecord.from_dokploy_records(
                target_record=persisted,
                target_id_record=persisted_target_id,
            ),
        )
        self.assertEqual(result.provider_target_record, persisted_provider_target)

    def test_repair_domain_authority_rejects_non_exact_live_domains(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            target_record = DokployTargetRecord(
                context="synthetic-context",
                instance="synthetic-instance",
                target_type="application",
                target_name="synthetic-target",
                updated_at="2026-08-12T00:00:00Z",
            )
            target_id_record = DokployTargetIdRecord(
                context=target_record.context,
                instance=target_record.instance,
                target_id="application-synthetic",
                updated_at=target_record.updated_at,
            )
            provider_target = ProviderTargetRecord.from_dokploy_records(
                target_record=target_record,
                target_id_record=target_id_record,
            )
            store.write_dokploy_target_record(target_record)
            store.write_dokploy_target_id_record(target_id_record)
            store.write_provider_target_record(provider_target)
            with self.assertRaisesRegex(ValueError, "exactly match live provider domains"):
                repair_dokploy_target_domain_authority(
                    record_store=store,
                    host="synthetic-host",
                    token="synthetic-token",
                    context=target_record.context,
                    instance=target_record.instance,
                    target_type="application",
                    target_id=target_id_record.target_id,
                    domains=("one.synthetic.invalid",),
                    expected_current_provider_target=provider_target.to_deployed_target_reference(),
                    fetch_target_payload=lambda *_args: {
                        "applicationId": target_id_record.target_id
                    },
                    fetch_target_domains=lambda *_args: self._live_domains(
                        "one.synthetic.invalid", "two.synthetic.invalid"
                    ),
                )
            store.close()

    def test_repair_domain_authority_accepts_duplicate_live_hosts(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            target_record = DokployTargetRecord(
                context="synthetic-context",
                instance="synthetic-instance",
                target_type="application",
                target_name="synthetic-target",
                updated_at="2026-08-12T00:00:00Z",
            )
            target_id_record = DokployTargetIdRecord(
                context=target_record.context,
                instance=target_record.instance,
                target_id="application-synthetic",
                updated_at=target_record.updated_at,
            )
            provider_target = ProviderTargetRecord.from_dokploy_records(
                target_record=target_record,
                target_id_record=target_id_record,
            )
            store.write_dokploy_target_record(target_record)
            store.write_dokploy_target_id_record(target_id_record)
            store.write_provider_target_record(provider_target)
            result = repair_dokploy_target_domain_authority(
                record_store=store,
                host="synthetic-host",
                token="synthetic-token",
                context=target_record.context,
                instance=target_record.instance,
                target_type="application",
                target_id=target_id_record.target_id,
                domains=("one.synthetic.invalid",),
                expected_current_provider_target=provider_target.to_deployed_target_reference(),
                fetch_target_payload=lambda *_args: {"applicationId": target_id_record.target_id},
                fetch_target_domains=lambda *_args: self._live_domains(
                    "one.synthetic.invalid", "one.synthetic.invalid"
                ),
            )
            store.close()

        self.assertEqual(result.live_provider_domains, ("one.synthetic.invalid",))

    def test_normalize_dokploy_domain_host_rejects_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "bare DNS hosts"):
            normalize_dokploy_domain_host("https://synthetic.invalid/path")

    def test_adopt_application_target_dry_run_does_not_write_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            result = adopt_dokploy_target(
                record_store=store,
                host="https://dokploy.example.invalid",
                token="token",
                context="discord-blue",
                instance="prod",
                target_type="application",
                target_id="app-123",
                healthcheck_path="/health",
                domains=("discord-blue.internal",),
                updated_at="2026-05-04T22:30:00Z",
                apply=False,
                fetch_target_payload=lambda *_args: {
                    "name": "discord-blue-lxc",
                    "env": "DISCORD_TOKEN=secret",
                    "environment": {"project": {"name": "Discord Blue"}},
                },
            )
            target_records = store.list_dokploy_target_records()
            target_id_records = store.list_dokploy_target_id_records()
            store.close()

        self.assertFalse(result.applied)
        self.assertEqual(result.target_record.target_name, "discord-blue-lxc")
        self.assertEqual(result.target_record.project_name, "Discord Blue")
        self.assertEqual(result.target_record.domains, ("discord-blue.internal",))
        self.assertEqual(result.target_id_record.target_id, "app-123")
        self.assertIn("provider env exists", result.warnings[0])
        self.assertEqual(target_records, ())
        self.assertEqual(target_id_records, ())

    def test_adopt_compose_target_applies_source_metadata_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            result = adopt_dokploy_target(
                record_store=store,
                host="https://dokploy.example.invalid",
                token="token",
                context="odoo-tenant-cm",
                instance="testing",
                target_type="compose",
                target_id="compose-123",
                updated_at="2026-05-04T22:35:00Z",
                apply=True,
                fetch_target_payload=lambda *_args: {
                    "name": "cm-testing",
                    "sourceType": "git",
                    "customGitUrl": "git@github.com:cbusillo/odoo-tenant-cm.git",
                    "customGitBranch": "main",
                    "composePath": "docker-compose.yml",
                    "watchPaths": ["addons", "docker", ""],
                    "enableSubmodules": True,
                    "project": {"name": "CM"},
                },
            )
            target_record = store.read_dokploy_target_record(
                context_name="odoo-tenant-cm", instance_name="testing"
            )
            target_id_record = store.read_dokploy_target_id_record(
                context_name="odoo-tenant-cm", instance_name="testing"
            )
            provider_target_record = store.read_provider_target_record(
                context_name="odoo-tenant-cm", instance_name="testing"
            )
            store.close()

        self.assertTrue(result.applied)
        self.assertEqual(target_record.target_name, "cm-testing")
        self.assertEqual(target_record.source_type, "git")
        self.assertEqual(target_record.compose_path, "docker-compose.yml")
        self.assertEqual(target_record.watch_paths, ("addons", "docker"))
        self.assertTrue(target_record.enable_submodules)
        self.assertTrue(target_record.healthcheck_enabled)
        self.assertIsNone(target_record.healthcheck_timeout_seconds)
        self.assertEqual(target_record.healthcheck_path, "/web/health")
        self.assertEqual(target_id_record.target_id, "compose-123")
        self.assertEqual(provider_target_record.target_id, "compose-123")
        self.assertEqual(provider_target_record.provider_target_type, "compose")

    def test_adopt_target_blocks_conflicting_provider_target_before_writes(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            store.write_provider_target_record(
                ProviderTargetRecord(
                    context="discord-blue",
                    instance="prod",
                    provider_id="dokploy",
                    target_category="application",
                    target_id="stale-app-123",
                    display_name="discord-blue-lxc",
                    provider_target_type="application",
                    provider_evidence={"project_name": "Discord Blue"},
                    updated_at="2026-05-04T22:00:00Z",
                    source_label="test:stale-provider-target",
                )
            )

            with self.assertRaisesRegex(ValueError, "dual-write conflict"):
                adopt_dokploy_target(
                    record_store=store,
                    host="https://dokploy.example.invalid",
                    token="token",
                    context="discord-blue",
                    instance="prod",
                    target_type="application",
                    target_id="app-123",
                    project_name="Discord Blue",
                    target_name="discord-blue-lxc",
                    healthcheck_path="/health",
                    updated_at="2026-05-04T22:35:00Z",
                    apply=True,
                    fetch_target_payload=lambda *_args: {"name": "discord-blue-lxc"},
                )

            self.assertEqual(store.list_dokploy_target_records(), ())
            self.assertEqual(store.list_dokploy_target_id_records(), ())
            store.close()

    def test_adopt_target_blocks_cross_route_provider_target_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            store.write_provider_target_record(
                ProviderTargetRecord(
                    context="canonical-site",
                    instance="testing",
                    provider_id="dokploy",
                    target_category="application",
                    target_id="app-123",
                    display_name="canonical-site",
                    provider_target_type="application",
                    provider_evidence={"project_name": "Demo"},
                    updated_at="2026-05-04T22:00:00Z",
                    source_label="test:canonical",
                )
            )

            with self.assertRaisesRegex(ValueError, "already bound to another route"):
                adopt_dokploy_target(
                    record_store=store,
                    host="https://dokploy.example.invalid",
                    token="token",
                    context="legacy-site",
                    instance="testing",
                    target_type="application",
                    target_id="app-123",
                    project_name="Demo",
                    target_name="legacy-site",
                    healthcheck_path="/health",
                    apply=False,
                    fetch_target_payload=lambda *_args: {"name": "legacy-site"},
                )
            self.assertEqual(store.list_dokploy_target_records(), ())
            self.assertEqual(store.list_dokploy_target_id_records(), ())
            store.close()

    def test_adopt_target_replaces_provider_target_when_current_authority_matches_expectation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            stale_provider_target = ProviderTargetRecord(
                context="cm_website",
                instance="prod",
                provider_id="dokploy",
                target_category="compose",
                target_id="compose-cm-prod",
                display_name="cm-prod",
                provider_target_type="compose",
                provider_evidence={"project_name": "odoo"},
                updated_at="2026-06-14T03:53:56Z",
                source_label="test:stale-provider-target",
            )
            store.write_provider_target_record(stale_provider_target)

            result = adopt_dokploy_target(
                record_store=store,
                host="https://dokploy.example.invalid",
                token="token",
                context="cm_website",
                instance="prod",
                target_type="compose",
                target_id="compose-cm-website-prod",
                project_name="odoo",
                target_name="odoo-tenant-cm-website-prod",
                healthcheck_path="/web/health",
                domains=("cm-website-prod.shinycomputers.com",),
                updated_at="2026-06-14T12:05:00Z",
                expected_current_provider_target=(
                    stale_provider_target.to_deployed_target_reference()
                ),
                apply=True,
                fetch_target_payload=lambda *_args: {
                    "name": "odoo-tenant-cm-website-prod",
                    "environment": {"project": {"name": "odoo"}},
                },
            )
            target_record = store.read_dokploy_target_record(
                context_name="cm_website", instance_name="prod"
            )
            target_id_record = store.read_dokploy_target_id_record(
                context_name="cm_website", instance_name="prod"
            )
            provider_target_record = store.read_provider_target_record(
                context_name="cm_website", instance_name="prod"
            )
            store.close()

        self.assertTrue(result.applied)
        self.assertEqual(target_record.target_name, "odoo-tenant-cm-website-prod")
        self.assertEqual(target_id_record.target_id, "compose-cm-website-prod")
        self.assertEqual(provider_target_record.target_id, "compose-cm-website-prod")
        self.assertEqual(provider_target_record.display_name, "odoo-tenant-cm-website-prod")

    def test_adopt_target_rejects_provider_target_replacement_when_expectation_mismatches(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            store.write_provider_target_record(
                ProviderTargetRecord(
                    context="cm_website",
                    instance="prod",
                    provider_id="dokploy",
                    target_category="compose",
                    target_id="compose-cm-prod",
                    display_name="cm-prod",
                    provider_target_type="compose",
                    provider_evidence={"project_name": "odoo"},
                    updated_at="2026-06-14T03:53:56Z",
                    source_label="test:stale-provider-target",
                )
            )

            with self.assertRaisesRegex(ValueError, "expectation did not match"):
                adopt_dokploy_target(
                    record_store=store,
                    host="https://dokploy.example.invalid",
                    token="token",
                    context="cm_website",
                    instance="prod",
                    target_type="compose",
                    target_id="compose-cm-website-prod",
                    project_name="odoo",
                    target_name="odoo-tenant-cm-website-prod",
                    healthcheck_path="/web/health",
                    domains=("cm-website-prod.shinycomputers.com",),
                    updated_at="2026-06-14T12:05:00Z",
                    expected_current_provider_target=DeployedTargetReference(
                        provider_id="dokploy",
                        target_category="compose",
                        target_id="some-other-compose",
                        display_name="cm-prod",
                        provider_target_type="compose",
                        provider_evidence={"project_name": "odoo"},
                    ),
                    apply=True,
                    fetch_target_payload=lambda *_args: {
                        "name": "odoo-tenant-cm-website-prod",
                        "environment": {"project": {"name": "odoo"}},
                    },
                )

            self.assertEqual(store.list_dokploy_target_records(), ())
            self.assertEqual(store.list_dokploy_target_id_records(), ())
            provider_target_record = store.read_provider_target_record(
                context_name="cm_website", instance_name="prod"
            )
            store.close()

        self.assertEqual(provider_target_record.target_id, "compose-cm-prod")

    def test_adopt_target_preserves_live_healthcheck_settings(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            result = adopt_dokploy_target(
                record_store=store,
                host="https://dokploy.example.invalid",
                token="token",
                context="discord-blue",
                instance="prod",
                target_type="application",
                target_id="app-123",
                healthcheck_path="/health",
                updated_at="2026-05-04T22:36:00Z",
                apply=True,
                fetch_target_payload=lambda *_args: {
                    "name": "discord-blue-lxc",
                    "healthcheckEnabled": False,
                    "healthcheckTimeoutSeconds": 45,
                    "environment": {"project": {"name": "Discord Blue"}},
                },
            )
            target_record = store.read_dokploy_target_record(
                context_name="discord-blue", instance_name="prod"
            )
            store.close()

        self.assertTrue(result.applied)
        self.assertFalse(target_record.healthcheck_enabled)
        self.assertEqual(target_record.healthcheck_timeout_seconds, 45)

    def test_adopt_target_canonicalizes_route_keys_before_persisting(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            result = adopt_dokploy_target(
                record_store=store,
                host="https://dokploy.example.invalid",
                token="token",
                context="Discord-Blue",
                instance="PrOd",
                target_type="application",
                target_id="app-123",
                healthcheck_path="/health",
                updated_at="2026-05-04T22:37:00Z",
                apply=True,
                fetch_target_payload=lambda *_args: {
                    "name": "discord-blue-lxc",
                    "environment": {"project": {"name": "Discord Blue"}},
                },
            )
            target_record = store.read_dokploy_target_record(
                context_name="discord-blue", instance_name="prod"
            )
            target_id_record = store.read_dokploy_target_id_record(
                context_name="discord-blue", instance_name="prod"
            )
            store.close()

        self.assertTrue(result.applied)
        self.assertEqual(result.target_record.context, "discord-blue")
        self.assertEqual(result.target_record.instance, "prod")
        self.assertEqual(target_record.context, "discord-blue")
        self.assertEqual(target_record.instance, "prod")
        self.assertEqual(target_id_record.context, "discord-blue")
        self.assertEqual(target_id_record.instance, "prod")

    def test_adopt_compose_target_copies_live_git_provider_fields(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            result = adopt_dokploy_target(
                record_store=store,
                host="https://dokploy.example.invalid",
                token="token",
                context="odoo-tenant-cm",
                instance="testing",
                target_type="compose",
                target_id="compose-123",
                source_git_ref="",
                updated_at="2026-05-04T22:38:00Z",
                apply=True,
                fetch_target_payload=lambda *_args: {
                    "name": "cm-testing",
                    "sourceType": "git",
                    "sourceGitRef": "origin/release",
                    "gitBranch": "release",
                    "customGitUrl": "git@github.com:cbusillo/odoo-tenant-cm.git",
                    "customGitBranch": "release",
                    "composePath": "docker-compose.yml",
                    "watchPaths": ["addons", "docker"],
                    "enableSubmodules": False,
                    "project": {"name": "CM"},
                },
            )
            target_record = store.read_dokploy_target_record(
                context_name="odoo-tenant-cm", instance_name="testing"
            )
            store.close()

        self.assertTrue(result.applied)
        self.assertEqual(target_record.source_git_ref, "origin/release")
        self.assertEqual(target_record.git_branch, "release")
        self.assertEqual(target_record.custom_git_branch, "release")
        self.assertEqual(target_record.watch_paths, ("addons", "docker"))
        self.assertFalse(target_record.enable_submodules)
        self.assertEqual(result.provider_fields["source_git_ref"], "origin/release")

    def test_adopt_compose_target_uses_branch_when_git_branch_is_absent(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            result = adopt_dokploy_target(
                record_store=store,
                host="https://dokploy.example.invalid",
                token="token",
                context="odoo-tenant-cm",
                instance="testing",
                target_type="compose",
                target_id="compose-123",
                source_git_ref="",
                updated_at="2026-05-05T19:30:00Z",
                apply=True,
                fetch_target_payload=lambda *_args: {
                    "name": "cm-testing",
                    "sourceType": "git",
                    "branch": "feature/adopted-branch",
                    "composePath": "docker-compose.yml",
                    "project": {"name": "CM"},
                },
            )
            target_record = store.read_dokploy_target_record(
                context_name="odoo-tenant-cm", instance_name="testing"
            )
            store.close()

        self.assertTrue(result.applied)
        self.assertEqual(target_record.source_git_ref, "feature/adopted-branch")
        self.assertEqual(target_record.git_branch, "feature/adopted-branch")
        self.assertEqual(result.provider_fields["branch"], "feature/adopted-branch")

    def test_cli_adopt_uses_live_source_ref_by_default(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            database_url = _sqlite_database_url(temporary_directory / "db.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            with patch.dict(
                "os.environ",
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                },
                clear=True,
            ):
                _write_dokploy_managed_secrets(store=store)
            store.close()

            with patch(
                "control_plane.cli.dokploy_api.fetch_dokploy_target_payload",
                return_value={
                    "name": "discord-blue-lxc",
                    "sourceGitRef": "origin/feature-branch",
                    "environment": {"project": {"name": "Discord Blue"}},
                },
            ):
                with patch.dict(
                    "os.environ",
                    {
                        "LAUNCHPLANE_DATABASE_URL": database_url,
                        control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                    },
                    clear=True,
                ):
                    result = CliRunner().invoke(
                        CLI_MAIN,
                        [
                            "dokploy-targets",
                            "adopt",
                            "--database-url",
                            database_url,
                            "--context",
                            "discord-blue",
                            "--instance",
                            "prod",
                            "--target-type",
                            "application",
                            "--target-id",
                            "app-123",
                            "--healthcheck-path",
                            "/health",
                        ],
                    )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["record"]["source_git_ref"], "origin/feature-branch")

    def test_cli_adopt_defaults_to_dry_run(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            database_url = _sqlite_database_url(temporary_directory / "db.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            with patch.dict(
                "os.environ",
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                },
                clear=True,
            ):
                _write_dokploy_managed_secrets(store=store)
            store.close()

            with (
                patch(
                    "control_plane.cli.dokploy_api.fetch_dokploy_target_payload",
                    return_value={
                        "name": "discord-blue-lxc",
                        "environment": {"project": {"name": "Discord Blue"}},
                    },
                ),
                patch.object(
                    PostgresRecordStore,
                    "ensure_schema",
                    side_effect=AssertionError("dry-run must not ensure schema"),
                ),
            ):
                with patch.dict(
                    "os.environ",
                    {
                        "LAUNCHPLANE_DATABASE_URL": database_url,
                        control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                    },
                    clear=True,
                ):
                    result = CliRunner().invoke(
                        CLI_MAIN,
                        [
                            "dokploy-targets",
                            "adopt",
                            "--database-url",
                            database_url,
                            "--context",
                            "discord-blue",
                            "--instance",
                            "prod",
                            "--target-type",
                            "application",
                            "--target-id",
                            "app-123",
                            "--healthcheck-path",
                            "/health",
                        ],
                    )

            with patch.dict(
                "os.environ",
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                },
                clear=True,
            ):
                store = PostgresRecordStore(database_url=database_url)
                store.ensure_schema()
                records = store.list_dokploy_target_records()
                store.close()

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["mode"], "dry_run")
        self.assertFalse(payload["applied"])
        self.assertEqual(payload["record"]["target_id"], "app-123")
        self.assertEqual(records, ())

    def test_cli_adopt_apply_writes_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            database_url = _sqlite_database_url(temporary_directory / "db.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            with patch.dict(
                "os.environ",
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                },
                clear=True,
            ):
                _write_dokploy_managed_secrets(store=store)
            store.close()

            def fetch_payload(**_kwargs: object) -> JsonObject:
                return {
                    "name": "discord-blue-lxc",
                    "environment": {"project": {"name": "Discord Blue"}},
                }

            with patch(
                "control_plane.cli.dokploy_api.fetch_dokploy_target_payload",
                side_effect=fetch_payload,
            ):
                with patch.dict(
                    "os.environ",
                    {
                        "LAUNCHPLANE_DATABASE_URL": database_url,
                        control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                    },
                    clear=True,
                ):
                    result = CliRunner().invoke(
                        CLI_MAIN,
                        [
                            "dokploy-targets",
                            "adopt",
                            "--database-url",
                            database_url,
                            "--context",
                            "discord-blue",
                            "--instance",
                            "prod",
                            "--target-type",
                            "application",
                            "--target-id",
                            "app-123",
                            "--target-name",
                            "discord-blue-prod",
                            "--project-name",
                            "Discord Blue",
                            "--source-label",
                            "test:adopt",
                            "--updated-at",
                            "2026-05-04T22:45:00Z",
                            "--apply",
                            *_allow_direct_db_mutation_argument(),
                        ],
                    )

            self.assertEqual(result.exit_code, 0, result.output)

            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            target_record = store.read_dokploy_target_record(
                context_name="discord-blue", instance_name="prod"
            )
            target_id_record = store.read_dokploy_target_id_record(
                context_name="discord-blue", instance_name="prod"
            )
            store.close()

        payload = json.loads(result.output)
        self.assertEqual(payload["mode"], "apply")
        self.assertTrue(payload["applied"])
        self.assertEqual(target_record.target_name, "discord-blue-prod")
        self.assertEqual(target_record.source_label, "test:adopt")
        self.assertEqual(target_id_record.target_id, "app-123")

    def test_cli_adopt_apply_requires_direct_db_acknowledgement(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            database_url = _sqlite_database_url(temporary_directory / "db.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            with patch.dict(
                "os.environ",
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                },
                clear=True,
            ):
                _write_dokploy_managed_secrets(store=store)
            store.close()

            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "dokploy-targets",
                    "adopt",
                    "--database-url",
                    database_url,
                    "--context",
                    "discord-blue",
                    "--instance",
                    "prod",
                    "--target-type",
                    "application",
                    "--target-id",
                    "app-123",
                    "--apply",
                ],
            )

        _assert_direct_db_mutation_rejected(self, result)

    def test_create_application_target_dry_run_plans_provider_requests(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            requests: list[tuple[str, JsonObject]] = []

            def mutate_provider(
                _host: str, _token: str, path: str, payload: JsonObject
            ) -> JsonObject:
                requests.append((path, payload))
                return {}

            result = create_dokploy_application_target(
                record_store=store,
                host="https://dokploy.example.invalid",
                token="token",
                context="discord-blue",
                instance="prod",
                target_name="discord-blue-prod",
                project_name="Discord Blue",
                environment_name="prod",
                server_id="server-123",
                healthcheck_path="/health",
                updated_at="2026-05-04T23:00:00Z",
                apply=False,
                mutate_provider=mutate_provider,
                fetch_target_payload=lambda *_args: {},
            )
            target_records = store.list_dokploy_target_records()
            store.close()

        self.assertFalse(result.applied)
        self.assertEqual(requests, [])
        self.assertEqual(
            [item["path"] for item in result.provider_requests],
            [
                "/api/project.create",
                "/api/environment.create",
                "/api/application.create",
            ],
        )
        self.assertEqual(result.target_record.target_name, "discord-blue-prod")
        self.assertEqual(result.target_id_record.target_id, "planned-application-id")
        self.assertEqual(target_records, ())

    def test_create_application_target_applies_provider_and_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            requests: list[tuple[str, JsonObject]] = []

            def mutate_provider(
                _host: str, _token: str, path: str, payload: JsonObject
            ) -> JsonObject:
                requests.append((path, payload))
                if path == "/api/project.create":
                    return {"projectId": "project-123"}
                if path == "/api/environment.create":
                    self.assertEqual(payload["projectId"], "project-123")
                    return {"environmentId": "env-123"}
                if path == "/api/application.create":
                    self.assertEqual(payload["environmentId"], "env-123")
                    return {"applicationId": "app-123"}
                raise AssertionError(path)

            result = create_dokploy_application_target(
                record_store=store,
                host="https://dokploy.example.invalid",
                token="token",
                context="discord-blue",
                instance="prod",
                target_name="discord-blue-prod",
                project_name="Discord Blue",
                environment_name="prod",
                server_id="server-123",
                healthcheck_path="/health",
                updated_at="2026-05-04T23:05:00Z",
                apply=True,
                mutate_provider=mutate_provider,
                fetch_target_payload=lambda *_args: {
                    "name": "discord-blue-prod",
                    "environment": {"project": {"name": "Discord Blue"}},
                },
            )
            target_record = store.read_dokploy_target_record(
                context_name="discord-blue", instance_name="prod"
            )
            target_id_record = store.read_dokploy_target_id_record(
                context_name="discord-blue", instance_name="prod"
            )
            store.close()

        self.assertTrue(result.applied)
        self.assertEqual(
            [path for path, _payload in requests],
            [
                "/api/project.create",
                "/api/environment.create",
                "/api/application.create",
            ],
        )
        self.assertEqual(result.project_id, "project-123")
        self.assertEqual(result.environment_id, "env-123")
        self.assertEqual(target_record.target_name, "discord-blue-prod")
        self.assertEqual(target_record.healthcheck_path, "/health")
        self.assertEqual(target_id_record.target_id, "app-123")

    def test_create_compose_target_applies_provider_and_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            requests: list[tuple[str, JsonObject]] = []

            def mutate_provider(
                _host: str, _token: str, path: str, payload: JsonObject
            ) -> JsonObject:
                requests.append((path, payload))
                if path == "/api/project.create":
                    return {"projectId": "project-123"}
                if path == "/api/environment.create":
                    self.assertEqual(payload["projectId"], "project-123")
                    return {"environmentId": "env-123"}
                if path == "/api/compose.create":
                    self.assertEqual(payload["environmentId"], "env-123")
                    self.assertEqual(payload["serverId"], "server-123")
                    return {"composeId": "compose-123"}
                raise AssertionError(path)

            result = create_dokploy_compose_target(
                record_store=store,
                host="https://dokploy.example.invalid",
                token="token",
                context="cm_website",
                instance="testing",
                target_name="cm-website-testing",
                project_name="Odoo",
                environment_name="production",
                server_id="server-123",
                healthcheck_path="/web/health",
                domains=("cm-website-testing.shinycomputers.com",),
                updated_at="2026-05-04T23:10:00Z",
                apply=True,
                mutate_provider=mutate_provider,
                fetch_target_payload=lambda *_args: {
                    "name": "cm-website-testing",
                    "sourceType": "raw",
                    "composePath": "docker-compose.yml",
                    "environment": {"project": {"name": "Odoo"}},
                },
            )
            target_record = store.read_dokploy_target_record(
                context_name="cm_website", instance_name="testing"
            )
            target_id_record = store.read_dokploy_target_id_record(
                context_name="cm_website", instance_name="testing"
            )
            provider_target = store.read_provider_target_record(
                context_name="cm_website", instance_name="testing"
            )
            store.close()

        self.assertTrue(result.applied)
        self.assertEqual(
            [path for path, _payload in requests],
            ["/api/project.create", "/api/environment.create", "/api/compose.create"],
        )
        self.assertEqual(target_record.target_type, "compose")
        self.assertEqual(target_record.target_name, "cm-website-testing")
        self.assertEqual(target_record.domains, ("cm-website-testing.shinycomputers.com",))
        self.assertEqual(target_record.source_type, "raw")
        self.assertEqual(target_record.compose_path, "docker-compose.yml")
        self.assertEqual(target_id_record.target_id, "compose-123")
        self.assertEqual(provider_target.provider_target_type, "compose")
        self.assertEqual(provider_target.target_id, "compose-123")

    def test_create_application_target_canonicalizes_route_keys_before_persisting(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()

            def mutate_provider(
                _host: str, _token: str, path: str, payload: JsonObject
            ) -> JsonObject:
                if path == "/api/project.create":
                    return {"projectId": "project-123"}
                if path == "/api/environment.create":
                    return {"environmentId": "env-123"}
                if path == "/api/application.create":
                    return {"applicationId": "app-123"}
                raise AssertionError(path)

            result = create_dokploy_application_target(
                record_store=store,
                host="https://dokploy.example.invalid",
                token="token",
                context="Discord-Blue",
                instance="PrOd",
                target_name="discord-blue-prod",
                project_name="Discord Blue",
                environment_name="PrOd",
                updated_at="2026-05-04T23:06:00Z",
                apply=True,
                mutate_provider=mutate_provider,
                fetch_target_payload=lambda *_args: {
                    "name": "discord-blue-prod",
                    "environment": {"project": {"name": "Discord Blue"}},
                },
            )
            target_record = store.read_dokploy_target_record(
                context_name="discord-blue", instance_name="prod"
            )
            target_id_record = store.read_dokploy_target_id_record(
                context_name="discord-blue", instance_name="prod"
            )
            store.close()

        self.assertTrue(result.applied)
        self.assertEqual(result.target_record.context, "discord-blue")
        self.assertEqual(result.target_record.instance, "prod")
        self.assertEqual(target_record.context, "discord-blue")
        self.assertEqual(target_record.instance, "prod")
        self.assertEqual(target_id_record.context, "discord-blue")
        self.assertEqual(target_id_record.instance, "prod")

    def test_create_application_target_reuses_existing_environment(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            requests: list[tuple[str, JsonObject]] = []

            def mutate_provider(
                _host: str, _token: str, path: str, payload: JsonObject
            ) -> JsonObject:
                requests.append((path, payload))
                return {"applicationId": "app-456"}

            result = create_dokploy_application_target(
                record_store=store,
                host="https://dokploy.example.invalid",
                token="token",
                context="verireel",
                instance="testing",
                target_name="verireel-testing",
                project_name="VeriReel",
                environment_id="env-existing",
                apply=True,
                mutate_provider=mutate_provider,
                fetch_target_payload=lambda *_args: {"name": "verireel-testing"},
            )
            target_id_record = store.read_dokploy_target_id_record(
                context_name="verireel", instance_name="testing"
            )
            store.close()

        self.assertTrue(result.applied)
        self.assertEqual([path for path, _payload in requests], ["/api/application.create"])
        self.assertEqual(requests[0][1]["environmentId"], "env-existing")
        self.assertEqual(target_id_record.target_id, "app-456")

    def test_create_application_target_blocks_existing_provider_target_before_provider_mutation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            store.write_provider_target_record(
                ProviderTargetRecord(
                    context="verireel",
                    instance="testing",
                    provider_id="dokploy",
                    target_category="application",
                    target_id="existing-app-123",
                    display_name="verireel-testing",
                    provider_target_type="application",
                    provider_evidence={"project_name": "VeriReel"},
                    updated_at="2026-05-04T23:00:00Z",
                    source_label="test:existing-provider-target",
                )
            )
            requests: list[tuple[str, JsonObject]] = []

            def mutate_provider(
                _host: str, _token: str, path: str, payload: JsonObject
            ) -> JsonObject:
                requests.append((path, payload))
                return {"applicationId": "app-456"}

            with self.assertRaisesRegex(ValueError, "dual-write conflict"):
                create_dokploy_application_target(
                    record_store=store,
                    host="https://dokploy.example.invalid",
                    token="token",
                    context="verireel",
                    instance="testing",
                    target_name="verireel-testing",
                    project_name="VeriReel",
                    environment_id="env-existing",
                    apply=True,
                    mutate_provider=mutate_provider,
                    fetch_target_payload=lambda *_args: {"name": "verireel-testing"},
                )
            target_records = store.list_dokploy_target_records()
            target_id_records = store.list_dokploy_target_id_records()
            store.close()

        self.assertEqual(requests, [])
        self.assertEqual(target_records, ())
        self.assertEqual(target_id_records, ())

    def test_create_compose_target_replaces_provider_target_when_current_authority_matches_expectation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            stale_provider_target = ProviderTargetRecord(
                context="cm_website",
                instance="prod",
                provider_id="dokploy",
                target_category="compose",
                target_id="compose-cm-prod",
                display_name="cm-prod",
                provider_target_type="compose",
                provider_evidence={"project_name": "odoo"},
                updated_at="2026-06-14T03:53:56Z",
                source_label="test:stale-provider-target",
            )
            store.write_provider_target_record(stale_provider_target)
            requests: list[tuple[str, JsonObject]] = []

            def mutate_provider(
                _host: str, _token: str, path: str, payload: JsonObject
            ) -> JsonObject:
                requests.append((path, payload))
                if path == "/api/compose.create":
                    return {"composeId": "compose-cm-website-prod"}
                raise AssertionError(path)

            result = create_dokploy_compose_target(
                record_store=store,
                host="https://dokploy.example.invalid",
                token="token",
                context="cm_website",
                instance="prod",
                target_name="odoo-tenant-cm-website-prod",
                environment_id="env-prod",
                server_id="server-prod",
                app_name="odoo-tenant-cm-website-prod",
                domains=("cm-website-prod.shinycomputers.com",),
                deploy_timeout_seconds=900,
                expected_current_provider_target=(
                    stale_provider_target.to_deployed_target_reference()
                ),
                apply=True,
                mutate_provider=mutate_provider,
                fetch_target_payload=lambda *_args: {
                    "name": "odoo-tenant-cm-website-prod",
                    "sourceType": "raw",
                    "composePath": "docker-compose.yml",
                    "environment": {"project": {"name": "odoo"}},
                },
            )
            target_record = store.read_dokploy_target_record(
                context_name="cm_website", instance_name="prod"
            )
            target_id_record = store.read_dokploy_target_id_record(
                context_name="cm_website", instance_name="prod"
            )
            provider_target_record = store.read_provider_target_record(
                context_name="cm_website", instance_name="prod"
            )
            store.close()

        self.assertTrue(result.applied)
        self.assertEqual([path for path, _payload in requests], ["/api/compose.create"])
        self.assertEqual(target_record.target_name, "odoo-tenant-cm-website-prod")
        self.assertEqual(target_id_record.target_id, "compose-cm-website-prod")
        self.assertEqual(provider_target_record.target_id, "compose-cm-website-prod")
        self.assertEqual(provider_target_record.display_name, "odoo-tenant-cm-website-prod")

    def test_create_compose_target_with_replacement_expectation_requires_existing_provider_target(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            requests: list[tuple[str, JsonObject]] = []

            def mutate_provider(
                _host: str, _token: str, path: str, payload: JsonObject
            ) -> JsonObject:
                requests.append((path, payload))
                return {"composeId": "compose-cm-website-prod"}

            with self.assertRaisesRegex(ValueError, "expected an existing provider target"):
                create_dokploy_compose_target(
                    record_store=store,
                    host="https://dokploy.example.invalid",
                    token="token",
                    context="cm_website",
                    instance="prod",
                    target_name="odoo-tenant-cm-website-prod",
                    environment_id="env-prod",
                    server_id="server-prod",
                    expected_current_provider_target=DeployedTargetReference(
                        provider_id="dokploy",
                        target_category="compose",
                        target_id="compose-cm-prod",
                        display_name="cm-prod",
                        provider_target_type="compose",
                        provider_evidence={"project_name": "odoo"},
                    ),
                    apply=True,
                    mutate_provider=mutate_provider,
                    fetch_target_payload=lambda *_args: {"name": "odoo-tenant-cm-website-prod"},
                )
            store.close()

        self.assertEqual(requests, [])

    def test_create_application_target_rejects_healthcheck_path_before_provider_mutation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            requests: list[tuple[str, JsonObject]] = []

            def mutate_provider(
                _host: str, _token: str, path: str, payload: JsonObject
            ) -> JsonObject:
                requests.append((path, payload))
                return {"projectId": "project-123"}

            with self.assertRaisesRegex(ValueError, "healthcheck-path to start with /"):
                create_dokploy_application_target(
                    record_store=store,
                    host="https://dokploy.example.invalid",
                    token="token",
                    context="discord-blue",
                    instance="prod",
                    target_name="discord-blue-prod",
                    project_name="Discord Blue",
                    healthcheck_path="health",
                    apply=True,
                    mutate_provider=mutate_provider,
                    fetch_target_payload=lambda *_args: {},
                )
            store.close()

        self.assertEqual(requests, [])

    def test_cli_create_application_dry_run_outputs_plan(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            database_url = _sqlite_database_url(temporary_directory / "db.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            with patch.dict(
                "os.environ",
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                },
                clear=True,
            ):
                _write_dokploy_managed_secrets(store=store)
            store.close()

            with (
                patch.object(
                    PostgresRecordStore,
                    "ensure_schema",
                    side_effect=AssertionError("dry-run must not ensure schema"),
                ),
                patch.dict(
                    "os.environ",
                    {
                        "LAUNCHPLANE_DATABASE_URL": database_url,
                        control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                    },
                    clear=True,
                ),
            ):
                result = CliRunner().invoke(
                    CLI_MAIN,
                    [
                        "dokploy-targets",
                        "create-application",
                        "--database-url",
                        database_url,
                        "--context",
                        "discord-blue",
                        "--instance",
                        "prod",
                        "--target-name",
                        "discord-blue-prod",
                        "--project-name",
                        "Discord Blue",
                        "--server-id",
                        "server-123",
                    ],
                )

            store = PostgresRecordStore(database_url=database_url)
            records = store.list_dokploy_target_records()
            store.close()

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["mode"], "dry_run")
        self.assertEqual(payload["plan"]["application"]["target_name"], "discord-blue-prod")
        self.assertEqual(len(payload["provider_requests"]), 3)
        self.assertEqual(records, ())

    def test_cli_create_application_apply_requires_direct_db_acknowledgement(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            database_url = _sqlite_database_url(temporary_directory / "db.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            with patch.dict(
                "os.environ",
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                },
                clear=True,
            ):
                _write_dokploy_managed_secrets(store=store)
            store.close()

            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "dokploy-targets",
                    "create-application",
                    "--database-url",
                    database_url,
                    "--context",
                    "discord-blue",
                    "--instance",
                    "prod",
                    "--target-name",
                    "discord-blue-prod",
                    "--project-name",
                    "Discord Blue",
                    "--apply",
                ],
            )

        _assert_direct_db_mutation_rejected(self, result)

    def test_cli_create_application_apply_writes_records_with_direct_db_acknowledgement(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            database_url = _sqlite_database_url(temporary_directory / "db.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            with patch.dict(
                "os.environ",
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                },
                clear=True,
            ):
                _write_dokploy_managed_secrets(store=store)
            store.close()

            def dokploy_request(
                *,
                host: str,
                token: str,
                path: str,
                method: str = "GET",
                payload: JsonObject | None = None,
                query: dict[str, str | int] | None = None,
                timeout_seconds: int | float = 60,
            ) -> JsonObject:
                del host, token, method, payload, query, timeout_seconds
                if path == "/api/project.create":
                    return {"projectId": "project-123"}
                if path == "/api/environment.create":
                    return {"environmentId": "env-123"}
                if path == "/api/application.create":
                    return {"applicationId": "app-123"}
                raise AssertionError(path)

            with (
                patch(
                    "control_plane.dokploy.api.dokploy_request",
                    side_effect=dokploy_request,
                ),
                patch(
                    "control_plane.cli.dokploy_api.fetch_dokploy_target_payload",
                    return_value={
                        "name": "discord-blue-prod",
                        "environment": {"project": {"name": "Discord Blue"}},
                    },
                ),
                patch.dict(
                    "os.environ",
                    {
                        "LAUNCHPLANE_DATABASE_URL": database_url,
                        control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                    },
                    clear=True,
                ),
            ):
                result = CliRunner().invoke(
                    CLI_MAIN,
                    [
                        "dokploy-targets",
                        "create-application",
                        "--database-url",
                        database_url,
                        "--context",
                        "discord-blue",
                        "--instance",
                        "prod",
                        "--target-name",
                        "discord-blue-prod",
                        "--project-name",
                        "Discord Blue",
                        "--apply",
                        *_allow_direct_db_mutation_argument(),
                    ],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            store = PostgresRecordStore(database_url=database_url)
            target_record = store.read_dokploy_target_record(
                context_name="discord-blue", instance_name="prod"
            )
            target_id_record = store.read_dokploy_target_id_record(
                context_name="discord-blue", instance_name="prod"
            )
            store.close()

        payload = json.loads(result.output)
        self.assertEqual(payload["mode"], "apply")
        self.assertTrue(payload["applied"])
        self.assertEqual(target_record.target_name, "discord-blue-prod")
        self.assertEqual(target_id_record.target_id, "app-123")


if __name__ == "__main__":
    unittest.main()
