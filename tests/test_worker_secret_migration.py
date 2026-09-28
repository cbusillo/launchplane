import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import sqlalchemy as sa

from control_plane import runtime_environments
from control_plane import secrets as control_plane_secrets
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.worker_secret_migration import (
    LAUNCHPLANE_SERVICE_INTEGRATION,
    LAUNCHPLANE_WORKER_INTEGRATION,
    MISSPELLED_RUNTIME_ENVIRONMENT_INTEGRATION,
    RUNTIME_ENVIRONMENT_INTEGRATION,
    copy_global_secret_to_contexts,
    move_service_secrets,
    move_worker_secrets,
    remove_copied_secrets,
    set_integration_status,
)
from control_plane.workflows.launchplane import resolve_launchplane_github_token

RUNTIME = control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION


class WorkerSecretMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database_url = f"sqlite+pysqlite:///{self.root / 'launchplane.sqlite3'}"
        environment = patch.dict(
            os.environ,
            {
                control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                "LAUNCHPLANE_DATABASE_URL": self.database_url,
            },
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.store = PostgresRecordStore(database_url=self.database_url)
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        for record in runtime_environments.build_runtime_environment_records_from_definition(
            runtime_environments.RuntimeEnvironmentDefinition(
                schema_version=1,
                shared_env={},
                contexts={
                    "site": runtime_environments.RuntimeEnvironmentContextDefinition(
                        shared_env={},
                        instances={
                            "prod": runtime_environments.RuntimeEnvironmentInstanceDefinition(
                                env={"SITE_MODE": "live"}
                            )
                        },
                    ),
                    "launchplane": runtime_environments.RuntimeEnvironmentContextDefinition(
                        shared_env={"LAUNCHPLANE_ADVISORY_GITHUB_APP_ID": "123"},
                        instances={},
                    ),
                },
            ),
            updated_at="2026-09-28T00:00:00Z",
            source_label="test",
        ):
            self.store.write_runtime_environment_record(record)

    def write(
        self, *, integration: str, key: str, value: str, instance: str = "prod"
    ) -> dict[str, str]:
        return control_plane_secrets.write_secret_value(
            record_store=self.store,
            scope="context_instance" if instance else "context",
            integration=integration,
            name=key.lower().replace("_", "-"),
            plaintext_value=value,
            binding_key=key,
            context_name="site",
            instance_name=instance,
            actor="test",
        )

    def move(self, *, source: str, destination: str) -> int:
        engine = sa.create_engine(self.database_url)
        try:
            with engine.begin() as connection:
                return move_worker_secrets(connection, source=source, destination=destination)
        finally:
            engine.dispose()

    def move_service(self, *, source: str, destination: str) -> int:
        engine = sa.create_engine(self.database_url)
        try:
            with engine.begin() as connection:
                return move_service_secrets(connection, source=source, destination=destination)
        finally:
            engine.dispose()

    def write_shared(
        self, *, integration: str, key: str, value: str, scope: str, context: str = ""
    ) -> dict[str, str]:
        return control_plane_secrets.write_secret_value(
            record_store=self.store,
            scope=scope,  # type: ignore[arg-type]
            integration=integration,
            name=key,
            plaintext_value=value,
            binding_key=key,
            context_name=context,
            actor="test",
        )

    def github_token(self, context: str) -> str:
        return resolve_launchplane_github_token(control_plane_root=self.root, context_name=context)

    def app_values(self) -> dict[str, str]:
        return runtime_environments.resolve_runtime_environment_values(
            control_plane_root=self.root, context_name="site", instance_name="prod"
        )

    def worker_values(self) -> dict[str, str]:
        return control_plane_secrets.resolve_lane_worker_secret_values(
            context_name="site", instance_name="prod"
        )

    def test_lane_backup_keys_leave_the_app_environment_for_the_worker_store(self) -> None:
        self.write(integration=RUNTIME, key="PRODUCTION_BACKUP_SSH_PRIVATE_KEY", value="key")
        self.write(integration=RUNTIME, key="PRODUCTION_BACKUP_SSH_KNOWN_HOSTS", value="hosts")
        self.write(integration=RUNTIME, key="RESEND_API_KEY", value="app-secret")
        self.assertIn("PRODUCTION_BACKUP_SSH_PRIVATE_KEY", self.app_values())

        moved = self.move(
            source=RUNTIME_ENVIRONMENT_INTEGRATION, destination=LAUNCHPLANE_WORKER_INTEGRATION
        )

        self.assertEqual(moved, 2)
        self.assertEqual(
            self.worker_values(),
            {
                "PRODUCTION_BACKUP_SSH_PRIVATE_KEY": "key",
                "PRODUCTION_BACKUP_SSH_KNOWN_HOSTS": "hosts",
            },
        )
        app_values = self.app_values()
        self.assertNotIn("PRODUCTION_BACKUP_SSH_PRIVATE_KEY", app_values)
        self.assertNotIn("PRODUCTION_BACKUP_SSH_KNOWN_HOSTS", app_values)
        self.assertEqual(app_values["RESEND_API_KEY"], "app-secret")

    def test_rotating_a_moved_key_updates_the_same_record(self) -> None:
        first = self.write(
            integration=RUNTIME, key="PRODUCTION_BACKUP_SSH_PRIVATE_KEY", value="old"
        )
        self.move(
            source=RUNTIME_ENVIRONMENT_INTEGRATION, destination=LAUNCHPLANE_WORKER_INTEGRATION
        )

        rotated = self.write(
            integration=LAUNCHPLANE_WORKER_INTEGRATION,
            key="PRODUCTION_BACKUP_SSH_PRIVATE_KEY",
            value="new",
        )

        self.assertEqual(rotated["secret_id"], first["secret_id"])
        self.assertEqual(rotated["action"], "rotated")
        self.assertEqual(self.worker_values(), {"PRODUCTION_BACKUP_SSH_PRIVATE_KEY": "new"})

    def test_shared_keys_are_left_alone(self) -> None:
        self.write(
            integration=RUNTIME,
            key="PRODUCTION_BACKUP_SSH_KNOWN_HOSTS",
            value="shared",
            instance="",
        )

        moved = self.move(
            source=RUNTIME_ENVIRONMENT_INTEGRATION, destination=LAUNCHPLANE_WORKER_INTEGRATION
        )

        self.assertEqual(moved, 0)
        self.assertEqual(self.worker_values(), {})

    def test_downgrade_restores_the_runtime_environment(self) -> None:
        self.write(integration=RUNTIME, key="VERIREEL_PROD_PROXMOX_SSH_PRIVATE_KEY", value="key")
        self.move(
            source=RUNTIME_ENVIRONMENT_INTEGRATION, destination=LAUNCHPLANE_WORKER_INTEGRATION
        )

        self.move(
            source=LAUNCHPLANE_WORKER_INTEGRATION, destination=RUNTIME_ENVIRONMENT_INTEGRATION
        )

        self.assertEqual(self.worker_values(), {})
        self.assertEqual(self.app_values()["VERIREEL_PROD_PROXMOX_SSH_PRIVATE_KEY"], "key")

    def test_launchplane_credentials_move_to_the_service_store(self) -> None:
        self.write_shared(
            integration=RUNTIME, key="GITHUB_TOKEN", value="global-token", scope="global"
        )
        self.write_shared(
            integration=RUNTIME,
            key="GITHUB_TOKEN",
            value="launchplane-token",
            scope="context",
            context="launchplane",
        )
        self.write_shared(
            integration=RUNTIME,
            key="LAUNCHPLANE_ADVISORY_GITHUB_APP_PRIVATE_KEY",
            value="advisory-key",
            scope="context",
            context="launchplane",
        )

        moved = self.move_service(
            source=RUNTIME_ENVIRONMENT_INTEGRATION, destination=LAUNCHPLANE_SERVICE_INTEGRATION
        )

        self.assertEqual(moved, 3)
        self.assertEqual(self.github_token("launchplane"), "launchplane-token")
        self.assertEqual(self.github_token("site"), "global-token")
        self.assertEqual(
            control_plane_secrets.resolve_launchplane_service_secret(
                context_name="launchplane",
                binding_key="LAUNCHPLANE_ADVISORY_GITHUB_APP_PRIVATE_KEY",
            ),
            "advisory-key",
        )
        for context in ("site", "launchplane"):
            values = runtime_environments.resolve_runtime_context_values(
                control_plane_root=self.root, context_name=context
            )
            self.assertNotIn("GITHUB_TOKEN", values)
            self.assertNotIn("LAUNCHPLANE_ADVISORY_GITHUB_APP_PRIVATE_KEY", values)

    def test_service_credentials_move_back_on_downgrade(self) -> None:
        self.write_shared(
            integration=RUNTIME, key="GITHUB_TOKEN", value="global-token", scope="global"
        )
        self.move_service(
            source=RUNTIME_ENVIRONMENT_INTEGRATION, destination=LAUNCHPLANE_SERVICE_INTEGRATION
        )

        self.move_service(
            source=LAUNCHPLANE_SERVICE_INTEGRATION, destination=RUNTIME_ENVIRONMENT_INTEGRATION
        )

        self.assertEqual(
            control_plane_secrets.resolve_launchplane_service_secret(
                context_name="site", binding_key="GITHUB_TOKEN"
            ),
            "",
        )
        self.assertEqual(self.github_token("site"), "global-token")
        self.assertEqual(
            runtime_environments.resolve_runtime_context_values(
                control_plane_root=self.root, context_name="site"
            )["GITHUB_TOKEN"],
            "global-token",
        )

    def test_a_secret_also_bound_under_another_key_stays_in_place(self) -> None:
        self.write_shared(
            integration=RUNTIME,
            key="GITHUB_TOKEN",
            value="app-token",
            scope="context",
            context="site",
        )
        binding = next(
            binding
            for binding in self.store.list_secret_bindings(integration=RUNTIME, limit=None)
            if binding.binding_key == "GITHUB_TOKEN"
        )
        control_plane_secrets.relabel_secret_binding(
            record_store=self.store, binding_id=binding.binding_id, binding_key="APP_GITHUB_TOKEN"
        )

        moved = self.move_service(
            source=RUNTIME_ENVIRONMENT_INTEGRATION, destination=LAUNCHPLANE_SERVICE_INTEGRATION
        )

        self.assertEqual(moved, 0)
        self.assertEqual(
            runtime_environments.resolve_runtime_context_values(
                control_plane_root=self.root, context_name="site"
            )["APP_GITHUB_TOKEN"],
            "app-token",
        )

    def test_an_existing_service_credential_is_kept_and_the_move_does_not_fail(self) -> None:
        self.write_shared(integration=RUNTIME, key="GITHUB_TOKEN", value="legacy", scope="global")
        self.write_shared(
            integration=LAUNCHPLANE_SERVICE_INTEGRATION,
            key="GITHUB_TOKEN",
            value="provisioned",
            scope="global",
        )

        moved = self.move_service(
            source=RUNTIME_ENVIRONMENT_INTEGRATION, destination=LAUNCHPLANE_SERVICE_INTEGRATION
        )

        self.assertEqual(moved, 0)
        self.assertEqual(self.github_token("site"), "provisioned")

    def test_misspelled_integration_records_are_disabled(self) -> None:
        written = self.write_shared(
            integration=MISSPELLED_RUNTIME_ENVIRONMENT_INTEGRATION,
            key="GITHUB_TOKEN",
            value="stray-token",
            scope="context",
            context="site",
        )
        engine = sa.create_engine(self.database_url)
        try:
            with engine.begin() as connection:
                changed = set_integration_status(
                    connection,
                    integration=MISSPELLED_RUNTIME_ENVIRONMENT_INTEGRATION,
                    status="disabled",
                )
        finally:
            engine.dispose()

        self.assertEqual(changed, 1)
        self.assertEqual(self.store.read_secret_record(written["secret_id"]).status, "disabled")
        self.assertTrue(
            all(
                binding.status == "disabled"
                for binding in self.store.list_secret_bindings(
                    integration=MISSPELLED_RUNTIME_ENVIRONMENT_INTEGRATION, limit=None
                )
            )
        )

    def copy_odoo_key(self, contexts: tuple[str, ...]) -> tuple[str, ...]:
        engine = sa.create_engine(self.database_url)
        try:
            with engine.begin() as connection:
                return copy_global_secret_to_contexts(
                    connection,
                    integration=RUNTIME_ENVIRONMENT_INTEGRATION,
                    binding_key="ODOO_KEY",
                    contexts=contexts,
                    recorded_at="2026-09-29T00:00:00Z",
                )
        finally:
            engine.dispose()

    def site_secrets(self, context: str, instance: str) -> dict[str, str]:
        return control_plane_secrets.resolve_site_secret_values(
            context_name=context,
            instance_name=instance,
            include_site_shared=instance in {"prod", "testing"},
        )

    def test_a_global_secret_is_copied_to_each_site_without_decrypting(self) -> None:
        self.write_shared(integration=RUNTIME, key="ODOO_KEY", value="shared-key", scope="global")
        self.write_shared(
            integration=RUNTIME, key="ODOO_KEY", value="own-key", scope="context", context="opw"
        )

        copied = self.copy_odoo_key(("cm", "cm_website", "opw"))

        self.assertEqual(copied, ("cm", "cm_website"))
        for context in ("cm", "cm_website"):
            for instance in ("prod", "testing"):
                self.assertEqual(self.site_secrets(context, instance)["ODOO_KEY"], "shared-key")
            self.assertNotIn("ODOO_KEY", self.site_secrets(context, "pr-1"))
        self.assertEqual(self.site_secrets("opw", "prod")["ODOO_KEY"], "own-key")
        self.assertEqual(self.copy_odoo_key(("cm",)), ())

    def test_a_copied_secret_can_be_rotated_in_place(self) -> None:
        self.write_shared(integration=RUNTIME, key="ODOO_KEY", value="shared-key", scope="global")
        self.copy_odoo_key(("cm",))

        rotated = self.write_shared(
            integration=RUNTIME, key="ODOO_KEY", value="cm-key", scope="context", context="cm"
        )

        self.assertEqual(rotated["action"], "rotated")
        self.assertEqual(self.site_secrets("cm", "prod")["ODOO_KEY"], "cm-key")

    def test_downgrade_removes_only_the_copies(self) -> None:
        self.write_shared(integration=RUNTIME, key="ODOO_KEY", value="shared-key", scope="global")
        self.write_shared(
            integration=RUNTIME, key="ODOO_KEY", value="own-key", scope="context", context="opw"
        )
        self.copy_odoo_key(("cm", "opw"))
        engine = sa.create_engine(self.database_url)
        try:
            with engine.begin() as connection:
                removed = remove_copied_secrets(
                    connection,
                    integration=RUNTIME_ENVIRONMENT_INTEGRATION,
                    binding_key="ODOO_KEY",
                    contexts=("cm", "opw"),
                )
        finally:
            engine.dispose()

        self.assertEqual(removed, 1)
        self.assertNotIn("ODOO_KEY", self.site_secrets("cm", "prod"))
        self.assertEqual(self.site_secrets("opw", "prod")["ODOO_KEY"], "own-key")


if __name__ == "__main__":
    unittest.main()
