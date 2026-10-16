from copy import deepcopy
import unittest
from typing import Any, cast

from control_plane.contracts.generic_web_extensions import GenericWebLaneExtensions
from control_plane.contracts.product_application_lifecycle_migration import (
    migrate_product_application_lifecycle_payload,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from tests.test_generic_web_deploy import _profile


class ApplicationLifecycleMigrationTests(unittest.TestCase):
    def payload(self) -> dict[str, Any]:
        payload = _profile().model_dump(mode="json")
        payload["schema_version"] = 1  # Historical stored profile.
        payload["lanes"] = [
            {
                "context": "sample",
                "instance": "testing",
                "generic_web_extensions": {"verification_extension": "verireel-testing"},
            },
            {"context": "sample", "instance": "prod"},
        ]
        payload["preview"] = {
            "transport_extension": "verireel-postgres",
            "data_transport_mode": "driver",
            "app_name_prefix": "sample",
            "transport_database_prefix": "sample_preview_",
        }
        return payload

    def test_legacy_behavior_moves_to_editable_nonproduction_declarations(self) -> None:
        payload = self.payload()
        original = deepcopy(payload)
        migrated = cast(dict[str, Any], migrate_product_application_lifecycle_payload(payload))
        profile = LaunchplaneProductProfileRecord.model_validate(migrated)
        self.assertEqual(payload, original)
        self.assertEqual(
            profile.lanes[1].model_dump(mode="json"),
            LaunchplaneProductProfileRecord.model_validate(payload)
            .lanes[1]
            .model_dump(mode="json"),
        )
        commands = profile.lanes[0].generic_web_extensions.maintenance
        self.assertTrue(any(item.on_reset for item in commands))
        self.assertTrue(any(not item.on_reset for item in commands))
        self.assertTrue(profile.lanes[0].generic_web_extensions.schedules)
        self.assertTrue(profile.preview.generic_web_extensions.maintenance)
        self.assertTrue(profile.preview.generic_web_extensions.schedules)
        self.assertEqual(migrate_product_application_lifecycle_payload(migrated), migrated)
        # Empty declarations serialize without extension fields. They still must
        # remain empty after saving and reading the converted schema.
        saved = profile.model_copy(
            update={
                "preview": profile.preview.model_copy(
                    update={"generic_web_extensions": GenericWebLaneExtensions()}
                )
            }
        ).model_dump(mode="json")
        self.assertEqual(migrate_product_application_lifecycle_payload(saved), saved)

    def test_generic_profiles_have_no_product_defaults(self) -> None:
        payload = _profile().model_dump(mode="json")
        self.assertEqual(migrate_product_application_lifecycle_payload(payload), payload)
        self.assertEqual(GenericWebLaneExtensions().maintenance, ())

    def test_explicit_commands_and_production_are_preserved(self) -> None:
        payload = self.payload()
        payload["lanes"][0]["generic_web_extensions"]["maintenance"] = [
            {"name": "custom", "command": "./custom"}
        ]
        payload["lanes"][1]["generic_web_extensions"] = {
            "maintenance": [{"name": "production-command", "command": "./production"}]
        }
        original = deepcopy(payload)
        migrated = cast(dict[str, Any], migrate_product_application_lifecycle_payload(payload))
        self.assertEqual(migrated["lanes"][1], original["lanes"][1])
        self.assertIn(
            original["lanes"][0]["generic_web_extensions"]["maintenance"][0],
            migrated["lanes"][0]["generic_web_extensions"]["maintenance"],
        )

    def test_database_migration_persists_declarations_without_production_edits(self) -> None:
        import importlib
        from unittest.mock import patch
        import sqlalchemy as sa

        migration = importlib.import_module(
            "control_plane.storage.migrations.versions.e4f8a2b6c0d3_profile_application_lifecycle"
        )
        engine = sa.create_engine("sqlite+pysqlite:///:memory:")
        metadata = sa.MetaData()
        profiles = sa.Table(
            "launchplane_product_profiles",
            metadata,
            sa.Column("product", sa.String(), primary_key=True),
            sa.Column("payload", sa.JSON()),
        )
        metadata.create_all(engine)
        payload = self.payload()
        with engine.begin() as connection:
            connection.execute(profiles.insert().values(product="sample", payload=payload))
            with patch.object(migration.op, "get_bind", return_value=connection):
                migration.upgrade()
                first = connection.execute(sa.select(profiles.c.payload)).scalar_one()
                migration.upgrade()
                migration.downgrade()
                final = connection.execute(sa.select(profiles.c.payload)).scalar_one()
            self.assertEqual(first, migrate_product_application_lifecycle_payload(payload))
            self.assertEqual(final, first)
        engine.dispose()
