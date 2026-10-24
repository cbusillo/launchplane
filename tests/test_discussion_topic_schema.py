import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from alembic import command
from sqlalchemy import create_engine, inspect

from control_plane.contracts.discussion_topic import DiscussionTopicInput
from control_plane.storage.migrations.versions import (
    e7c1d5e9a3b6_drop_retired_delivery_tables as held,
    e8d2f6a0b4c7_drop_preview_enablement as preview,
    e9e3a7b1c5d8_join_retirement_paths as old_join,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.schema_migration import (
    SCHEMA_MIGRATION_TARGET_REVISION,
    alembic_config,
    schema_migration_target,
)
from tests.support.stores import sqlite_database_url


class DiscussionTopicSchemaTests(unittest.TestCase):
    def test_default_upgrade_adds_topics_without_applying_held_delivery_drop(self) -> None:
        with TemporaryDirectory() as directory:
            url = sqlite_database_url(Path(directory) / "schema.db")
            config = alembic_config(url)
            command.upgrade(config, preview.revision)
            command.upgrade(config, SCHEMA_MIGRATION_TARGET_REVISION)
            engine = create_engine(url)
            try:
                tables = set(inspect(engine).get_table_names())
                self.assertTrue(set(held.RETIRED_TABLES) <= tables)
            finally:
                engine.dispose()
            store = PostgresRecordStore(database_url=url)
            try:
                topic = store.add_discussion_topic(
                    "site-a",
                    "contact",
                    DiscussionTopicInput(
                        title="Decide: Contact links", issue_url="https://git.example.test/issues/7"
                    ),
                )
                self.assertEqual(store.list_discussion_topics("site-a"), (topic,))
            finally:
                store.close()

    def test_already_applied_delivery_paths_can_upgrade_and_keep_one_revision(self) -> None:
        for start in (held.revision, old_join.revision):
            with self.subTest(start=start), TemporaryDirectory() as directory:
                url = sqlite_database_url(Path(directory) / "schema.db")
                config = alembic_config(url)
                command.upgrade(config, start)
                target = schema_migration_target(
                    current_revision=start, target_revision=SCHEMA_MIGRATION_TARGET_REVISION
                )
                command.upgrade(config, target)
                store = PostgresRecordStore(database_url=url)
                try:
                    self.assertEqual(store.list_discussion_topics("site-a"), ())
                    store.verify_schema()
                finally:
                    store.close()
