"""Real PostgreSQL activation invariants, races, replacement, and rollback."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import unittest

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from control_plane.contracts.ordinary_agent_activation import (
    OrdinaryAgentDeliveryActivationRecord,
)
from control_plane.storage.postgres import (
    OrdinaryAgentDeliveryActivationConflictError,
    PostgresRecordStore,
)
from tests import test_postgres_integration as postgres_support
from tests.test_ordinary_agent_activation_storage import (
    _event,
    _record,
    _reference,
)


class _FailingReplacementStore(PostgresRecordStore):
    def _after_ordinary_agent_delivery_activation_write_step(self, step_name: str) -> None:
        if step_name == "installed_event_inserted":
            raise RuntimeError("injected replacement event failure")


@unittest.skipUnless(
    os.environ.get("LAUNCHPLANE_TEST_POSTGRES_URL"), "isolated PostgreSQL not configured"
)
class OrdinaryAgentDeliveryActivationPostgresTests(unittest.TestCase):
    def test_schema_has_exact_timeless_current_scope_unique_index(self) -> None:
        with postgres_support._store_for_fresh_head_database() as store:
            revision, digest, valid = store.ordinary_agent_delivery_activation_schema_capability()
            self.assertEqual(revision, store.schema_revision())
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            self.assertTrue(valid)
            index = next(
                item
                for item in inspect(store._engine).get_indexes(
                    "launchplane_ordinary_agent_delivery_activations"
                )
                if item["name"] == "launchplane_ordinary_agent_activation_current_scope_uidx"
            )
            predicate = str(index["dialect_options"]["postgresql_where"]).lower()
            self.assertTrue(index["unique"])
            self.assertEqual(
                tuple(index["column_names"]),
                ("repository_id", "base_branch", "managed_set_id", "managed_rule_id"),
            )
            self.assertIn("revoked_at is null", predicate)
            self.assertIn("superseded_at is null", predicate)
            self.assertNotIn("now", predicate)

    def test_database_rejects_two_current_rows_for_one_scope(self) -> None:
        with postgres_support._store_for_fresh_head_database() as store:
            first = _record(
                operation_id="postgres-unique-setup-one",
                installed_at="2026-09-10T20:00:00Z",
                expires_at="2026-09-11T20:00:00Z",
            )
            second = _record(
                operation_id="postgres-unique-setup-two",
                installed_at="2026-09-10T20:01:00Z",
                expires_at="2026-09-11T20:01:00Z",
                scope=first.scope.model_copy(
                    update={
                        "target": first.scope.target.model_copy(
                            update={"repository": "example/renamed-repository"}
                        )
                    }
                ),
            )
            store.install_ordinary_agent_delivery_activation(
                first,
                _event(
                    first,
                    action="installed",
                    source_operation_id=first.source_setup_operation_id,
                ),
            )
            with store._session_factory() as session:
                session.add(store._ordinary_agent_delivery_activation_row(second))
                with self.assertRaises(IntegrityError):
                    session.commit()

    def test_concurrent_first_installs_have_one_winner(self) -> None:
        with postgres_support._store_for_fresh_head_database() as store:
            records = tuple(
                _record(
                    operation_id=f"postgres-race-setup-{ordinal}",
                    installed_at="2026-09-10T20:00:00Z",
                    expires_at="2026-09-11T20:00:00Z",
                )
                for ordinal in (1, 2)
            )

            def install(record: OrdinaryAgentDeliveryActivationRecord) -> str:
                try:
                    return store.install_ordinary_agent_delivery_activation(
                        record,
                        _event(
                            record,
                            action="installed",
                            source_operation_id=record.source_setup_operation_id,
                        ),
                    ).status
                except OrdinaryAgentDeliveryActivationConflictError:
                    return "conflict"

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = tuple(executor.map(install, records))

            self.assertEqual(sorted(outcomes), ["conflict", "written"])
            self.assertEqual(len(store.list_ordinary_agent_delivery_activation_records()), 1)
            self.assertEqual(len(store.list_ordinary_agent_delivery_activation_event_records()), 1)

    def test_expired_replacement_persists_old_terminal_projection_and_both_events(self) -> None:
        with postgres_support._store_for_fresh_head_database() as store:
            first = _record(
                operation_id="postgres-replacement-setup-one",
                installed_at="2026-09-10T20:00:00Z",
                expires_at="2026-09-10T21:00:00Z",
            )
            store.install_ordinary_agent_delivery_activation(
                first,
                _event(
                    first,
                    action="installed",
                    source_operation_id=first.source_setup_operation_id,
                ),
            )
            predecessor = _reference(first)
            second = _record(
                operation_id="postgres-replacement-setup-two",
                installed_at="2026-09-10T22:00:00Z",
                expires_at="2026-09-11T22:00:00Z",
                predecessor=predecessor,
            )
            superseded = OrdinaryAgentDeliveryActivationRecord.model_validate(
                {
                    **first.model_dump(mode="json"),
                    "revision": 2,
                    "updated_at": second.installed_at,
                    "superseded_by_activation_id": second.activation_id,
                    "superseded_at": second.installed_at,
                    "activation_sha256": "",
                }
            )
            superseded_event = _event(
                superseded,
                action="superseded",
                source_operation_id=second.source_setup_operation_id,
                previous=first,
            )
            installed_event = _event(
                second,
                action="installed",
                source_operation_id=second.source_setup_operation_id,
            )

            store.install_ordinary_agent_delivery_activation(
                second,
                installed_event,
                predecessor=superseded,
                predecessor_event=superseded_event,
            )

            self.assertEqual(
                store.read_ordinary_agent_delivery_activation_record(first.activation_id),
                superseded,
            )
            self.assertEqual(
                store.recover_ordinary_agent_delivery_activation_by_source_operation(
                    second.source_setup_operation_id
                ),
                (second, installed_event),
            )
            self.assertEqual(len(store.list_ordinary_agent_delivery_activation_event_records()), 3)

    def test_replacement_event_failure_rolls_back_old_projection_and_new_record(self) -> None:
        with postgres_support._store_for_fresh_head_database() as store:
            first = _record(
                operation_id="postgres-rollback-setup-one",
                installed_at="2026-09-10T20:00:00Z",
                expires_at="2026-09-10T21:00:00Z",
            )
            first_event = _event(
                first,
                action="installed",
                source_operation_id=first.source_setup_operation_id,
            )
            store.install_ordinary_agent_delivery_activation(first, first_event)
            predecessor = _reference(first)
            second = _record(
                operation_id="postgres-rollback-setup-two",
                installed_at="2026-09-10T22:00:00Z",
                expires_at="2026-09-11T22:00:00Z",
                predecessor=predecessor,
            )
            superseded = OrdinaryAgentDeliveryActivationRecord.model_validate(
                {
                    **first.model_dump(mode="json"),
                    "revision": 2,
                    "updated_at": second.installed_at,
                    "superseded_by_activation_id": second.activation_id,
                    "superseded_at": second.installed_at,
                    "activation_sha256": "",
                }
            )
            failing_store = _FailingReplacementStore(database_url=store.database_url)
            self.addCleanup(failing_store.close)

            with self.assertRaisesRegex(RuntimeError, "injected replacement event failure"):
                failing_store.install_ordinary_agent_delivery_activation(
                    second,
                    _event(
                        second,
                        action="installed",
                        source_operation_id=second.source_setup_operation_id,
                    ),
                    predecessor=superseded,
                    predecessor_event=_event(
                        superseded,
                        action="superseded",
                        source_operation_id=second.source_setup_operation_id,
                        previous=first,
                    ),
                )

            self.assertEqual(
                store.read_ordinary_agent_delivery_activation_record(first.activation_id), first
            )
            with self.assertRaises(FileNotFoundError):
                store.read_ordinary_agent_delivery_activation_record(second.activation_id)
            self.assertEqual(
                store.list_ordinary_agent_delivery_activation_event_records(), (first_event,)
            )


if __name__ == "__main__":
    unittest.main()
