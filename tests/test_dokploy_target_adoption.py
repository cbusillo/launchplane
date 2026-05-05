import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from control_plane.cli import main
from control_plane import secrets as control_plane_secrets
from control_plane.dokploy import JsonObject
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.dokploy_target_adoption import (
    adopt_dokploy_target,
    create_dokploy_application_target,
)


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


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
                "control_plane.cli.control_plane_dokploy.fetch_dokploy_target_payload",
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
                        main,
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

            with patch(
                "control_plane.cli.control_plane_dokploy.fetch_dokploy_target_payload",
                return_value={
                    "name": "discord-blue-lxc",
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
                        main,
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
                "control_plane.cli.control_plane_dokploy.fetch_dokploy_target_payload",
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
                        main,
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

            with patch.dict(
                "os.environ",
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                },
                clear=True,
            ):
                result = CliRunner().invoke(
                    main,
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


if __name__ == "__main__":
    unittest.main()
