from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest

from alembic import command
from sqlalchemy import create_engine, inspect

from control_plane.storage.schema_adoption import (
    LEGACY_BASELINE_SCHEMA,
    LEGACY_CURRENT_SCHEMA,
    SchemaAdoptionError,
    SchemaInspectorProtocol,
    verify_existing_schema_for_stamp,
)
from control_plane.storage.schema_invariants import EXPECTED_ALEMBIC_HEAD_REVISION
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

    def test_rejects_every_table_the_migration_head_creates_after_adoption(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = f"sqlite+pysqlite:///{Path(directory) / 'head.sqlite3'}"
            command.upgrade(alembic_config(database_url), EXPECTED_ALEMBIC_HEAD_REVISION)
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
