from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from unittest.mock import patch

from alembic import command
from sqlalchemy import create_engine, inspect, text

from control_plane.storage.schema_adoption import (
    LEGACY_BASELINE_SCHEMA,
    LEGACY_CURRENT_SCHEMA,
    SchemaAdoptionError,
    SchemaInspectorProtocol,
    verify_existing_schema_for_stamp,
    LEGACY_BASELINE_REVISION,
    LEGACY_CURRENT_SCHEMA_REVISION,
    schema_stamp_revision_for_engine,
)
from control_plane.storage.schema_invariants import (
    EXPECTED_ALEMBIC_HEAD_REVISION,
    PRE_RETIREMENT_ALEMBIC_REVISION,
)
from control_plane.storage.schema_migration import alembic_config


class FakeInspector:
    def __init__(
        self,
        columns_by_table: dict[str, set[str]],
        indexes_by_table: dict[str, list[dict[str, object]]] | None = None,
        primary_keys_by_table: dict[str, tuple[str, ...]] | None = None,
        column_types_by_table: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.columns_by_table = columns_by_table
        self.indexes_by_table = indexes_by_table or {}
        self.primary_keys_by_table = primary_keys_by_table or {}
        self.column_types_by_table = column_types_by_table or {}

    def get_table_names(self) -> list[str]:
        return list(self.columns_by_table)

    def get_columns(self, table_name: str) -> list[dict[str, object]]:
        return [
            {
                "name": column_name,
                "type": self.column_types_by_table.get(table_name, {}).get(column_name, ""),
            }
            for column_name in self.columns_by_table[table_name]
        ]

    def get_indexes(self, table_name: str) -> list[dict[str, object]]:
        return self.indexes_by_table.get(table_name, [])

    def get_pk_constraint(self, table_name: str) -> dict[str, object]:
        return {"constrained_columns": list(self.primary_keys_by_table.get(table_name, ()))}


class LegacyWithHeadTable:
    """A legacy database that also holds one table exactly as the migration head creates it."""

    def __init__(
        self,
        legacy: SchemaInspectorProtocol,
        head: SchemaInspectorProtocol,
        head_table: str,
    ) -> None:
        self.legacy = legacy
        self.head = head
        self.head_table = head_table

    def _source(self, table_name: str) -> SchemaInspectorProtocol:
        return self.head if table_name == self.head_table else self.legacy

    def get_table_names(self) -> list[str]:
        return [*self.legacy.get_table_names(), self.head_table]

    def get_columns(self, table_name: str) -> Sequence[Mapping[str, object]]:
        return self._source(table_name).get_columns(table_name)

    def get_indexes(self, table_name: str) -> Sequence[Mapping[str, object]]:
        return self._source(table_name).get_indexes(table_name)

    def get_pk_constraint(self, table_name: str) -> Mapping[str, object]:
        return self._source(table_name).get_pk_constraint(table_name)


class SchemaAdoptionTests(unittest.TestCase):
    def test_stamped_revision_is_not_replaced_by_retired_table_presence(self) -> None:
        with TemporaryDirectory() as directory:
            engine = create_engine(f"sqlite+pysqlite:///{Path(directory) / 'records.db'}")
            try:
                with engine.begin() as connection:
                    connection.execute(text("CREATE TABLE alembic_version (version_num TEXT)"))
                    connection.execute(
                        text("INSERT INTO alembic_version VALUES (:revision)"),
                        {"revision": LEGACY_BASELINE_REVISION},
                    )
                    connection.execute(
                        text("CREATE TABLE launchplane_preview_enablement (id TEXT)")
                    )
                self.assertEqual(schema_stamp_revision_for_engine(engine), "")
            finally:
                engine.dispose()

    def test_unversioned_adoption_requires_a_complete_supported_shape(self) -> None:
        engine = create_engine("sqlite://")
        try:
            for revision, schema in (
                (LEGACY_BASELINE_REVISION, LEGACY_BASELINE_SCHEMA),
                (LEGACY_CURRENT_SCHEMA_REVISION, LEGACY_CURRENT_SCHEMA),
            ):
                columns = {name: set(names) for name, names in schema.items()}
                with (
                    self.subTest(revision=revision),
                    patch(
                        "control_plane.storage.schema_adoption.inspect",
                        return_value=FakeInspector(columns),
                    ),
                ):
                    self.assertEqual(schema_stamp_revision_for_engine(engine), revision)
            columns["launchplane_preview_enablement"].remove("payload")
            with (
                patch(
                    "control_plane.storage.schema_adoption.inspect",
                    return_value=FakeInspector(columns),
                ),
                self.assertRaises(SchemaAdoptionError),
            ):
                schema_stamp_revision_for_engine(engine)
        finally:
            engine.dispose()

    def _baseline_columns_by_table(self) -> dict[str, set[str]]:
        return {table_name: set(columns) for table_name, columns in LEGACY_BASELINE_SCHEMA.items()}

    def test_accepts_existing_tables_that_match_revision_columns(self) -> None:
        inspector = FakeInspector(self._baseline_columns_by_table())

        verify_existing_schema_for_stamp(
            inspector=inspector,
            expected_schema=LEGACY_BASELINE_SCHEMA,
        )

    def test_accepts_current_legacy_revision_shape(self) -> None:
        columns_by_table = {
            table_name: set(columns) for table_name, columns in LEGACY_CURRENT_SCHEMA.items()
        }
        inspector = FakeInspector(columns_by_table)

        verify_existing_schema_for_stamp(
            inspector=inspector,
            expected_schema=LEGACY_CURRENT_SCHEMA,
        )

    def test_rejects_missing_expected_columns_before_stamp(self) -> None:
        columns_by_table = self._baseline_columns_by_table()
        columns_by_table["launchplane_backup_gates"].remove("payload")
        inspector = FakeInspector(columns_by_table)

        with self.assertRaisesRegex(
            SchemaAdoptionError,
            "launchplane_backup_gates missing columns: payload",
        ):
            verify_existing_schema_for_stamp(
                inspector=inspector,
                expected_schema=LEGACY_BASELINE_SCHEMA,
            )

    def test_rejects_unexpected_live_columns_before_stamp(self) -> None:
        columns_by_table = self._baseline_columns_by_table()
        columns_by_table["launchplane_backup_gates"].add("operator_notes")
        inspector = FakeInspector(columns_by_table)

        with self.assertRaisesRegex(
            SchemaAdoptionError,
            "launchplane_backup_gates has unexpected columns: operator_notes",
        ):
            verify_existing_schema_for_stamp(
                inspector=inspector,
                expected_schema=LEGACY_BASELINE_SCHEMA,
            )

    def test_rejects_missing_expected_tables_before_stamp(self) -> None:
        columns_by_table = self._baseline_columns_by_table()
        del columns_by_table["launchplane_backup_gates"]
        inspector = FakeInspector(columns_by_table)

        with self.assertRaisesRegex(
            SchemaAdoptionError,
            "missing tables: launchplane_backup_gates",
        ):
            verify_existing_schema_for_stamp(
                inspector=inspector,
                expected_schema=LEGACY_BASELINE_SCHEMA,
            )

    def test_rejects_tables_beyond_the_selected_adoption_revision(self) -> None:
        columns_by_table = self._baseline_columns_by_table()
        columns_by_table["launchplane_human_sessions"] = {"session_id"}
        inspector = FakeInspector(columns_by_table)

        with self.assertRaisesRegex(
            SchemaAdoptionError,
            "has Launchplane tables beyond the adoption revision: launchplane_human_sessions",
        ):
            verify_existing_schema_for_stamp(
                inspector=inspector,
                expected_schema=LEGACY_BASELINE_SCHEMA,
            )

    def test_rejects_current_orm_later_tables_without_manual_denylist_updates(self) -> None:
        columns_by_table = self._baseline_columns_by_table()
        columns_by_table["launchplane_authz_policies"] = {"policy_id"}
        inspector = FakeInspector(columns_by_table)

        with self.assertRaisesRegex(
            SchemaAdoptionError,
            "has Launchplane tables beyond the adoption revision: launchplane_authz_policies",
        ):
            verify_existing_schema_for_stamp(
                inspector=inspector,
                expected_schema=LEGACY_BASELINE_SCHEMA,
            )

    def test_rejects_tables_at_both_sides_of_retirement_before_adoption(self) -> None:
        for revision in (PRE_RETIREMENT_ALEMBIC_REVISION, EXPECTED_ALEMBIC_HEAD_REVISION):
            with self.subTest(revision=revision):
                self._assert_rejects_later_tables(revision)

    def _assert_rejects_later_tables(self, revision: str) -> None:
        with TemporaryDirectory() as directory:
            database_url = f"sqlite+pysqlite:///{Path(directory) / 'head.sqlite3'}"
            command.upgrade(alembic_config(database_url), revision)
            engine = create_engine(database_url)
            try:
                head = cast(SchemaInspectorProtocol, inspect(engine))
                later_tables = sorted(
                    set(head.get_table_names()) - set(LEGACY_CURRENT_SCHEMA) - {"alembic_version"}
                )
                self.assertTrue(later_tables)
                legacy = FakeInspector(
                    {name: set(columns) for name, columns in LEGACY_CURRENT_SCHEMA.items()}
                )
                for table_name in later_tables:
                    with (
                        self.subTest(table=table_name),
                        self.assertRaisesRegex(SchemaAdoptionError, table_name),
                    ):
                        verify_existing_schema_for_stamp(
                            inspector=LegacyWithHeadTable(legacy, head, table_name),
                            expected_schema=LEGACY_CURRENT_SCHEMA,
                        )
            finally:
                engine.dispose()

    def test_ignores_tables_not_owned_by_launchplane_orm(self) -> None:
        columns_by_table = self._baseline_columns_by_table()
        columns_by_table["external_extension_table"] = {"id", "payload"}
        inspector = FakeInspector(columns_by_table)

        verify_existing_schema_for_stamp(
            inspector=inspector,
            expected_schema=LEGACY_BASELINE_SCHEMA,
        )

    def test_rejects_existing_critical_table_missing_required_index(self) -> None:
        columns_by_table = {
            table_name: set(columns) for table_name, columns in LEGACY_CURRENT_SCHEMA.items()
        }
        table_name = "launchplane_odoo_stable_target_replacement_operations"
        columns_by_table[table_name] = {
            "operation_id",
            "product",
            "context",
            "instance",
            "idempotency_key",
            "idempotency_scope",
            "status",
            "phase",
            "created_at",
            "updated_at",
            "lease_owner",
            "lease_expires_at",
            "heartbeat_at",
            "attempt",
            "payload",
        }
        inspector = FakeInspector(columns_by_table)

        with self.assertRaisesRegex(
            SchemaAdoptionError,
            "launchplane_odoo_stable_target_replacement_operations missing "
            "required index launchplane_odoo_replacement_operation_idempotency_idx",
        ):
            verify_existing_schema_for_stamp(
                inspector=inspector,
                expected_schema={
                    **LEGACY_CURRENT_SCHEMA,
                    table_name: frozenset(columns_by_table[table_name]),
                },
            )

    def test_rejects_existing_active_operation_index_without_partial_predicate(
        self,
    ) -> None:
        columns_by_table = {
            table_name: set(columns) for table_name, columns in LEGACY_CURRENT_SCHEMA.items()
        }
        table_name = "launchplane_odoo_stable_bootstrap_operations"
        columns_by_table[table_name] = {
            "operation_id",
            "product",
            "context",
            "instance",
            "idempotency_key",
            "status",
            "phase",
            "created_at",
            "updated_at",
            "lease_owner",
            "lease_expires_at",
            "heartbeat_at",
            "attempt",
            "payload",
        }
        inspector = FakeInspector(
            columns_by_table,
            indexes_by_table={
                table_name: [
                    {
                        "name": "launchplane_odoo_bootstrap_operation_idempotency_idx",
                        "unique": False,
                        "column_names": ["idempotency_key", "updated_at"],
                    },
                    {
                        "name": "launchplane_odoo_bootstrap_active_lane_uidx",
                        "unique": True,
                        "column_names": ["product", "context", "instance"],
                    },
                    {
                        "name": "launchplane_odoo_bootstrap_worker_claim_idx",
                        "unique": False,
                        "column_names": ["status", "lease_expires_at", "updated_at"],
                    },
                ]
            },
        )

        with self.assertRaisesRegex(
            SchemaAdoptionError,
            "launchplane_odoo_bootstrap_active_lane_uidx has predicate <none>",
        ):
            verify_existing_schema_for_stamp(
                inspector=inspector,
                expected_schema={
                    **LEGACY_CURRENT_SCHEMA,
                    table_name: frozenset(columns_by_table[table_name]),
                },
            )

    def test_rejects_route_binding_table_without_composite_primary_key(self) -> None:
        table_name = "launchplane_route_bindings"
        route_binding_columns = {
            "product",
            "context",
            "instance",
            "provider_id",
            "target_category",
            "ingress_provider",
            "ingress_endpoint_key",
            "termination_kind",
            "tls_owner",
            "primary_domain",
            "status",
            "freshness_status",
            "updated_at",
            "payload",
        }
        inspector = FakeInspector(
            {**self._baseline_columns_by_table(), table_name: route_binding_columns},
            indexes_by_table={
                table_name: [
                    {
                        "name": "launchplane_route_bindings_lookup_idx",
                        "unique": False,
                        "column_names": ["product", "context", "status", "instance"],
                    },
                    {
                        "name": "launchplane_route_bindings_updated_idx",
                        "unique": False,
                        "column_names": ["updated_at"],
                    },
                ]
            },
        )

        with self.assertRaisesRegex(
            SchemaAdoptionError,
            r"launchplane_route_bindings has primary key \(<none>\)",
        ):
            verify_existing_schema_for_stamp(
                inspector=inspector,
                expected_schema={
                    **LEGACY_BASELINE_SCHEMA,
                    table_name: frozenset(route_binding_columns),
                },
            )

    def test_rejects_route_binding_payload_that_is_not_jsonb(self) -> None:
        table_name = "launchplane_route_bindings"
        route_binding_columns = {
            "product",
            "context",
            "instance",
            "provider_id",
            "target_category",
            "ingress_provider",
            "ingress_endpoint_key",
            "termination_kind",
            "tls_owner",
            "primary_domain",
            "status",
            "freshness_status",
            "updated_at",
            "payload",
        }
        inspector = FakeInspector(
            {**self._baseline_columns_by_table(), table_name: route_binding_columns},
            indexes_by_table={
                table_name: [
                    {
                        "name": "launchplane_route_bindings_lookup_idx",
                        "unique": False,
                        "column_names": ["product", "context", "status", "instance"],
                    },
                    {
                        "name": "launchplane_route_bindings_updated_idx",
                        "unique": False,
                        "column_names": ["updated_at"],
                    },
                ]
            },
            primary_keys_by_table={table_name: ("product", "context", "instance")},
            column_types_by_table={table_name: {"payload": "JSON"}},
        )

        with self.assertRaisesRegex(
            SchemaAdoptionError,
            "launchplane_route_bindings.payload has type json",
        ):
            verify_existing_schema_for_stamp(
                inspector=inspector,
                expected_schema={
                    **LEGACY_BASELINE_SCHEMA,
                    table_name: frozenset(route_binding_columns),
                },
                verify_column_types=True,
            )


if __name__ == "__main__":
    unittest.main()
