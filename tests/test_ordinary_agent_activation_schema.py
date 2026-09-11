from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from alembic import command
from sqlalchemy import create_engine, inspect, text

from control_plane.storage.schema_invariants import (
    EXPECTED_ALEMBIC_HEAD_REVISION,
    RUNTIME_COMPATIBLE_ALEMBIC_REVISIONS,
    ordinary_agent_delivery_activation_schema_capability,
    ordinary_agent_delivery_activation_schema_invariant_errors,
    ordinary_agent_delivery_activation_schema_invariants_sha256,
)
from control_plane.storage.schema_migration import alembic_config


class OrdinaryAgentDeliveryActivationSchemaTests(unittest.TestCase):
    def test_migration_adds_exact_tables_checks_indexes_and_head_membership(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = (
                f"sqlite+pysqlite:///{Path(temporary_directory_name) / 'records.sqlite3'}"
            )
            config = alembic_config(database_url)
            command.upgrade(config, "d8a0b2c4e6f9")
            command.upgrade(config, EXPECTED_ALEMBIC_HEAD_REVISION)
            engine = create_engine(database_url)
            self.addCleanup(engine.dispose)

            self.assertEqual(RUNTIME_COMPATIBLE_ALEMBIC_REVISIONS, ("e0f2a4c6d8b1",))
            self.assertEqual(ordinary_agent_delivery_activation_schema_invariant_errors(engine), [])
            revision, digest, valid = ordinary_agent_delivery_activation_schema_capability(engine)
            self.assertEqual(revision, EXPECTED_ALEMBIC_HEAD_REVISION)
            self.assertEqual(digest, ordinary_agent_delivery_activation_schema_invariants_sha256())
            self.assertTrue(valid)

            inspector = inspect(engine)
            activation_checks = {
                check["name"]
                for check in inspector.get_check_constraints(
                    "launchplane_ordinary_agent_delivery_activations"
                )
            }
            event_checks = {
                check["name"]
                for check in inspector.get_check_constraints(
                    "launchplane_ordinary_agent_delivery_activation_events"
                )
            }
            self.assertEqual(len(activation_checks), 5)
            self.assertEqual(len(event_checks), 4)
            current_index = next(
                item
                for item in inspector.get_indexes("launchplane_ordinary_agent_delivery_activations")
                if item["name"] == "launchplane_ordinary_agent_activation_current_scope_uidx"
            )
            predicate = str(current_index["dialect_options"]["sqlite_where"]).lower()
            self.assertEqual(
                tuple(current_index["column_names"]),
                ("repository_id", "base_branch", "managed_set_id", "managed_rule_id"),
            )
            self.assertEqual(
                "".join(predicate.split()),
                "revoked_atisnullandsuperseded_atisnull",
            )
            self.assertNotIn("now", predicate)

    def test_capability_fails_closed_when_required_index_is_missing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = (
                f"sqlite+pysqlite:///{Path(temporary_directory_name) / 'records.sqlite3'}"
            )
            command.upgrade(alembic_config(database_url), EXPECTED_ALEMBIC_HEAD_REVISION)
            engine = create_engine(database_url)
            self.addCleanup(engine.dispose)
            with engine.begin() as connection:
                connection.execute(
                    text("DROP INDEX launchplane_ordinary_agent_activation_current_scope_uidx")
                )

            revision, _, valid = ordinary_agent_delivery_activation_schema_capability(engine)
            self.assertEqual(revision, EXPECTED_ALEMBIC_HEAD_REVISION)
            self.assertFalse(valid)
            self.assertTrue(
                any(
                    "launchplane_ordinary_agent_activation_current_scope_uidx" in error
                    for error in ordinary_agent_delivery_activation_schema_invariant_errors(engine)
                )
            )


if __name__ == "__main__":
    unittest.main()
