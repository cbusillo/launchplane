from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from alembic import command
from sqlalchemy import MetaData, Table, create_engine, inspect, select

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.storage.migrations.versions import (
    c87d8d574b67_drop_preview_desired_state_label as migration,
)
from control_plane.storage.schema_migration import alembic_config

_TABLE = "launchplane_preview_desired_states"


class RetiredPreviewLabelTests(unittest.TestCase):
    def test_a_profile_saved_with_the_retired_enable_label_still_reads(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            {
                "product": "sellyouroutboard",
                "display_name": "SellYourOutboard",
                "repository": "cbusillo/sellyouroutboard",
                "driver_id": "generic-web",
                "image": {"repository": "ghcr.io/cbusillo/sellyouroutboard"},
                "runtime_port": 3000,
                "health_path": "/api/health",
                "preview": {
                    "enabled": True,
                    "context": "sellyouroutboard-preview",
                    "enable_label": "preview",
                },
                "updated_at": "2026-10-01T00:00:00Z",
                "source": "test",
            }
        )

        self.assertEqual(profile.preview.context, "sellyouroutboard-preview")
        self.assertNotIn("enable_label", profile.model_dump()["preview"])

    def test_migration_drops_the_label_column_and_keeps_each_row(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = f"sqlite+pysqlite:///{Path(directory) / 'records.sqlite3'}"
            config = alembic_config(database_url)
            command.upgrade(config, migration.down_revision)
            engine = create_engine(database_url)
            try:
                payload = {"label": "preview", "desired_count": 0}
                with engine.begin() as connection:
                    table = Table(_TABLE, MetaData(), autoload_with=connection)
                    connection.execute(
                        table.insert().values(
                            desired_state_id="desired-1",
                            product="verireel",
                            context="verireel-testing",
                            discovered_at="2026-10-01T00:00:00Z",
                            repository="every/verireel",
                            label="preview",
                            status="pass",
                            desired_count=0,
                            payload=payload,
                        )
                    )

                command.upgrade(config, migration.revision)
                with engine.connect() as connection:
                    table = Table(_TABLE, MetaData(), autoload_with=connection)
                    self.assertNotIn("label", table.columns)
                    row = connection.execute(select(table)).one()
                self.assertEqual(row.desired_state_id, "desired-1")
                self.assertEqual(row.payload, payload)

                command.downgrade(config, migration.down_revision)
                columns = {column["name"] for column in inspect(engine).get_columns(_TABLE)}
                self.assertIn("label", columns)
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
