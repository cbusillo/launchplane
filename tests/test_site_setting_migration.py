import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import sqlalchemy as sa

from control_plane import runtime_environments
from control_plane import secrets as control_plane_secrets
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.site_setting_migration import copy_global_settings_to_contexts

KEYS = ("ODOO_DB_USER", "ENV_OVERRIDE_DISABLE_CRON")


class SiteSettingMigrationTests(unittest.TestCase):
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
        self.write("global", env={"ODOO_DB_USER": "odoo", "ENV_OVERRIDE_DISABLE_CRON": True})
        self.write("instance", context="cm", instance="prod", env={"CM_MODE": "live"})
        self.write("context", context="opw", env={"ODOO_DB_USER": "opw_user"})

    def write(
        self, scope: str, *, env: dict[str, object], context: str = "", instance: str = ""
    ) -> None:
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord.model_validate(
                {
                    "scope": scope,
                    "context": context,
                    "instance": instance,
                    "env": env,
                    "updated_at": "2026-09-28T00:00:00Z",
                    "source_label": "test",
                }
            )
        )

    def copy(self, contexts: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
        engine = sa.create_engine(self.database_url)
        try:
            with engine.begin() as connection:
                return copy_global_settings_to_contexts(
                    connection,
                    keys=(*KEYS, "ENV_OVERRIDE_UNSET"),
                    contexts=contexts,
                    recorded_at="2026-09-29T00:00:00Z",
                    source_label="migration:test",
                )
        finally:
            engine.dispose()

    def site_values(self, context: str, instance: str) -> dict[str, str]:
        return runtime_environments.resolve_site_runtime_environment(
            control_plane_root=self.root, context_name=context, instance_name=instance
        ).values

    def test_each_site_gets_the_global_settings_it_lacks(self) -> None:
        self.assertEqual(
            self.copy(("cm", "opw")),
            {"cm": KEYS, "opw": ("ENV_OVERRIDE_DISABLE_CRON",)},
        )

        self.assertEqual(
            self.site_values("cm", "prod"),
            {"CM_MODE": "live", "ODOO_DB_USER": "odoo", "ENV_OVERRIDE_DISABLE_CRON": "True"},
        )
        self.assertEqual(
            self.site_values("opw", "testing"),
            {"ODOO_DB_USER": "opw_user", "ENV_OVERRIDE_DISABLE_CRON": "True"},
        )
        self.assertEqual(self.copy(("cm", "opw")), {})

    def test_other_products_and_the_global_settings_are_untouched(self) -> None:
        self.write("context", context="sellyouroutboard", env={"SITE": "syo"})

        self.copy(("cm",))

        self.assertEqual(self.site_values("sellyouroutboard", "prod"), {"SITE": "syo"})
        self.assertEqual(
            runtime_environments.load_runtime_environment_definition(
                control_plane_root=self.root
            ).shared_env,
            {"ODOO_DB_USER": "odoo", "ENV_OVERRIDE_DISABLE_CRON": True},
        )


if __name__ == "__main__":
    unittest.main()
