from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from alembic import command
from pydantic import ValidationError
from sqlalchemy import MetaData, Table, create_engine, select

from control_plane.contracts.product_onboarding_manifest import ProductOnboardingManifest
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.storage.migrations.versions import (
    a4c8e0f2b6d3_archive_nonruntime_product_profiles as migration,
)
from control_plane.storage.schema_migration import alembic_config
from tests.support.profiles import product_profile_payload

_PROFILES = "launchplane_product_profiles"


def _runtime_payload() -> dict[str, object]:
    payload: dict[str, object] = json.loads(json.dumps(product_profile_payload("runtime-site")))
    return payload


def _awareness_payload() -> dict[str, object]:
    return {
        "schema_version": 2,
        "classification": "active_awareness",
        "product": "awareness-site",
        "display_name": "Awareness Site",
        "repository": "example/awareness-site",
        "owner": {"github_login": "director", "github_id": "101"},
        "release_on_acceptance": "director_standing",
        "updated_at": "2026-10-24T20:00:00Z",
        "source": "product-onboarding",
    }


class NonruntimeProfileArchiveMigrationTests(unittest.TestCase):
    def _upgrade_with_profiles(
        self, payloads: tuple[dict[str, object], ...]
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        with TemporaryDirectory() as directory:
            database_url = f"sqlite+pysqlite:///{Path(directory) / 'records.sqlite3'}"
            config = alembic_config(database_url)
            command.upgrade(config, migration.down_revision)
            engine = create_engine(database_url)
            try:
                with engine.begin() as connection:
                    profiles = Table(_PROFILES, MetaData(), autoload_with=connection)
                    for payload in payloads:
                        connection.execute(
                            profiles.insert().values(
                                product=payload["product"],
                                display_name=payload["display_name"],
                                repository=payload["repository"],
                                driver_id=payload.get("driver_id", ""),
                                updated_at=payload["updated_at"],
                                payload=payload,
                            )
                        )
                command.upgrade(config, migration.revision)
                with engine.connect() as connection:
                    profiles = Table(_PROFILES, MetaData(), autoload_with=connection)
                    archive = Table(migration.ARCHIVE_TABLE, MetaData(), autoload_with=connection)
                    remaining = [
                        dict(row) for row in connection.execute(select(profiles)).mappings()
                    ]
                    archived = [dict(row) for row in connection.execute(select(archive)).mappings()]
            finally:
                engine.dispose()
        return remaining, archived

    def test_nonruntime_profile_is_archived_and_removed_while_runtime_profile_stays(self) -> None:
        runtime = _runtime_payload()
        awareness = _awareness_payload()

        remaining, archived = self._upgrade_with_profiles((runtime, awareness))

        self.assertEqual([row["product"] for row in remaining], ["runtime-site"])
        self.assertEqual(remaining[0]["payload"], runtime)
        LaunchplaneProductProfileRecord.model_validate(remaining[0]["payload"])
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]["product"], "awareness-site")
        self.assertEqual(archived[0]["repository"], "example/awareness-site")
        self.assertEqual(archived[0]["payload"], awareness)
        self.assertEqual(archived[0]["source"], migration.MIGRATION_SOURCE)
        self.assertEqual(archived[0]["reason"], migration.MIGRATION_REASON)

    def test_empty_profile_table_upgrades_without_archiving(self) -> None:
        remaining, archived = self._upgrade_with_profiles(())

        self.assertEqual(remaining, [])
        self.assertEqual(archived, [])

    def test_runtime_only_profiles_upgrade_unchanged(self) -> None:
        runtime = _runtime_payload()

        remaining, archived = self._upgrade_with_profiles((runtime,))

        self.assertEqual([row["payload"] for row in remaining], [runtime])
        self.assertEqual(archived, [])

    def test_nonruntime_profiles_and_onboarding_manifests_are_refused(self) -> None:
        with self.assertRaises(ValidationError):
            LaunchplaneProductProfileRecord.model_validate(_awareness_payload())
        manifest = {
            key: value
            for key, value in _awareness_payload().items()
            if key not in {"schema_version", "updated_at", "source"}
        }
        with self.assertRaises(ValidationError):
            ProductOnboardingManifest.model_validate(manifest)


if __name__ == "__main__":
    unittest.main()
