from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from alembic import command
from pydantic import ValidationError
from sqlalchemy import Engine, MetaData, Table, create_engine, select

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


def _awareness_payload(product: str = "awareness-site") -> dict[str, object]:
    return {
        "schema_version": 2,
        "classification": "active_awareness",
        "product": product,
        "display_name": "Awareness Site",
        "repository": f"example/{product}",
        "owner": {"github_login": "director", "github_id": "101"},
        "release_on_acceptance": "director_standing",
        "updated_at": "2026-10-24T20:00:00Z",
        "source": "product-onboarding",
    }


@contextmanager
def _sqlite_database() -> Iterator[str]:
    with TemporaryDirectory() as directory:
        yield f"sqlite+pysqlite:///{Path(directory) / 'records.sqlite3'}"


def _databases() -> Iterator[tuple[str, Callable[[], AbstractContextManager[str]]]]:
    yield "sqlite", _sqlite_database
    if os.environ.get("LAUNCHPLANE_TEST_POSTGRES_URL"):
        from tests.test_postgres_integration import _isolated_postgres_database

        yield "postgresql", _isolated_postgres_database


def _insert_profiles(engine: Engine, payloads: tuple[dict[str, object], ...]) -> None:
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


class NonruntimeProfileArchiveMigrationTests(unittest.TestCase):
    def _upgrade_with_profiles(
        self,
        payloads: tuple[dict[str, object], ...],
        replayed: tuple[dict[str, object], ...] | None = None,
    ) -> list[tuple[list[dict[str, object]], list[dict[str, object]]]]:
        results = []
        for name, database in _databases():
            with self.subTest(database=name), database() as database_url:
                config = alembic_config(database_url)
                command.upgrade(config, migration.down_revision)
                engine = create_engine(database_url)
                try:
                    _insert_profiles(engine, payloads)
                    command.upgrade(config, migration.revision)
                    if replayed is not None:
                        command.stamp(config, migration.down_revision)
                        _insert_profiles(engine, replayed)
                        command.upgrade(config, migration.revision)
                    with engine.connect() as connection:
                        profiles = Table(_PROFILES, MetaData(), autoload_with=connection)
                        archive = Table(
                            migration.ARCHIVE_TABLE, MetaData(), autoload_with=connection
                        )
                        remaining = [
                            dict(row) for row in connection.execute(select(profiles)).mappings()
                        ]
                        archived = [
                            dict(row)
                            for row in connection.execute(
                                select(archive).order_by(archive.c.product)
                            ).mappings()
                        ]
                finally:
                    engine.dispose()
                results.append((remaining, archived))
        return results

    def test_nonruntime_profiles_are_archived_and_removed_while_runtime_profile_stays(
        self,
    ) -> None:
        runtime = _runtime_payload()
        first = _awareness_payload("awareness-a")
        second = _awareness_payload("awareness-b")

        for remaining, archived in self._upgrade_with_profiles((runtime, first, second)):
            self.assertEqual([row["payload"] for row in remaining], [runtime])
            LaunchplaneProductProfileRecord.model_validate(remaining[0]["payload"])
            self.assertEqual([row["payload"] for row in archived], [first, second])
            for row, payload in zip(archived, (first, second), strict=True):
                self.assertEqual(row["product"], payload["product"])
                self.assertEqual(row["repository"], payload["repository"])
                self.assertEqual(row["source"], migration.MIGRATION_SOURCE)
                self.assertEqual(row["reason"], migration.MIGRATION_REASON)

    def test_replayed_upgrade_keeps_one_archive_row_per_product(self) -> None:
        runtime = _runtime_payload()
        first = _awareness_payload("awareness-a")
        second = _awareness_payload("awareness-b")

        for remaining, archived in self._upgrade_with_profiles(
            (runtime, first), replayed=(first, second)
        ):
            self.assertEqual([row["payload"] for row in remaining], [runtime])
            self.assertEqual([row["payload"] for row in archived], [first, second])

    def test_empty_profile_table_upgrades_without_archiving(self) -> None:
        for remaining, archived in self._upgrade_with_profiles(()):
            self.assertEqual(remaining, [])
            self.assertEqual(archived, [])

    def test_runtime_only_profiles_upgrade_unchanged(self) -> None:
        runtime = _runtime_payload()

        for remaining, archived in self._upgrade_with_profiles((runtime,)):
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
