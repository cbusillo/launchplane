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
    LAUNCHPLANE_WORKER_INTEGRATION,
    RUNTIME_ENVIRONMENT_INTEGRATION,
    move_worker_secrets,
)

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
                    )
                },
            ),
            updated_at="2026-09-28T00:00:00Z",
            source_label="test",
        ):
            self.store.write_runtime_environment_record(record)

    def write(self, *, integration: str, key: str, value: str, instance: str = "prod"):
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


if __name__ == "__main__":
    unittest.main()
