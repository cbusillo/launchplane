"""One original actor/key has either an admitted operation or durable exclusion."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
from typing import Any
import unittest
from unittest.mock import patch

from sqlalchemy import event, text
from sqlalchemy.sql.dml import Insert

from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.storage.postgres import LaunchplaneIdempotencyRow, PostgresRecordStore
from control_plane.testing_rebuild_request_settlement import (
    TestingRebuildRequestConflict,
    TestingRebuildRequestExclusion,
)


class TestingRebuildSettlementTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        self.open_store("sqlite+pysqlite:///" + str(Path(directory) / "settlement.sqlite"))
        self.store.ensure_schema()

    def open_store(self, url: str) -> None:
        self.store = PostgresRecordStore(database_url=url)
        self.addCleanup(self.store.close)
        self.peer = PostgresRecordStore(database_url=url)
        self.addCleanup(self.peer.close)
        self.operation = OdooStableTargetReplacementOperationRecord(
            operation_id="original-operation",
            product="fixture-product",
            context="testing-context",
            instance="testing",
            idempotency_key="original-key",
            idempotency_scope="original-actor",
            request_fingerprint="original-intent",
            request=OdooStableTargetReplacementApplyRequest(
                product="fixture-product",
                instance="testing",
                strategy="replace-and-cutover",
                data_source_mode="empty",
                allow_empty_data=True,
                reason="Fresh testing copy",
                artifact_id="verified-testing-build",
                source_git_ref="c" * 40,
            ),
            created_at="2026-10-20T14:00:00Z",
            updated_at="2026-10-20T14:00:00Z",
        )
        self.exclusion = TestingRebuildRequestExclusion(
            product=self.operation.product,
            original_scope=self.operation.idempotency_scope,
            original_key=self.operation.idempotency_key,
            request_fingerprint=self.operation.request_fingerprint,
            excluded_at=self.operation.created_at,
        )

    def exclude(self, store: PostgresRecordStore | None = None) -> object:
        return (store or self.store).settle_testing_rebuild_request(
            self.exclusion, trace_id="fixture"
        )

    def admit(self, store: PostgresRecordStore | None = None) -> object:
        return (
            store or self.store
        ).create_odoo_stable_target_replacement_operation_record_if_no_active_lane(self.operation)

    def test_exclusion_survives_a_new_store_and_refuses_late_admission(self) -> None:
        first = self.exclude()
        self.assertEqual(self.exclude(self.peer), first)
        with self.assertRaises(TestingRebuildRequestConflict):
            self.admit(self.peer)
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), ())

    def test_observed_operation_is_returned_without_any_queue_transition(self) -> None:
        self.assertEqual(self.admit(), (self.operation, True))
        self.assertEqual(self.exclude(self.peer), self.operation)
        self.assertEqual(
            self.peer.read_odoo_stable_target_replacement_operation_record(
                self.operation.operation_id
            ),
            self.operation,
        )

    def test_terminal_original_is_still_the_winner_after_context_changes(self) -> None:
        terminal = OdooStableTargetReplacementOperationRecord.model_validate(
            {
                **self.operation.model_dump(mode="python"),
                "status": "fail",
                "phase": "failed",
                "finished_at": self.operation.updated_at,
                "error_message": "Fixture operation ended",
            }
        )
        self.store.write_odoo_stable_target_replacement_operation_record(terminal)
        late = self.operation.model_copy(
            update={
                "context": "new-testing-context",
                "operation_id": "late-operation",
            }
        )
        observed, created = (
            self.peer.create_odoo_stable_target_replacement_operation_record_if_no_active_lane(late)
        )
        self.assertFalse(created)
        self.assertEqual(observed, terminal)
        self.assertEqual(self.exclude(), terminal)
        self.assertEqual(len(self.store.list_odoo_stable_target_replacement_operation_records()), 1)

    def test_foreign_product_or_intent_cannot_reuse_an_exclusion(self) -> None:
        self.exclude()
        for changes in ({"product": "another-product"}, {"request_fingerprint": "another-intent"}):
            with self.subTest(changes=changes), self.assertRaises(TestingRebuildRequestConflict):
                self.peer.settle_testing_rebuild_request(
                    self.exclusion.model_copy(update=changes), trace_id="foreign"
                )
        foreign = self.operation.model_copy(
            update={
                "product": "another-product",
                "request": self.operation.request.model_copy(update={"product": "another-product"}),
            }
        )
        with self.assertRaises(TestingRebuildRequestConflict):
            self.peer.create_odoo_stable_target_replacement_operation_record_if_no_active_lane(
                foreign
            )
        self.assertEqual(self.exclude(self.peer), self.exclusion)

    def test_another_actor_has_an_independent_original_key(self) -> None:
        self.exclude()
        other = self.operation.model_copy(update={"idempotency_scope": "another-actor"})
        self.assertEqual(
            self.peer.create_odoo_stable_target_replacement_operation_record_if_no_active_lane(
                other
            ),
            (other, True),
        )
        self.assertEqual(self.exclude(), self.exclusion)

    def test_failed_receipt_commit_does_not_leave_an_exclusion(self) -> None:
        def refuse_receipt(
            connection: Any,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            compiled = context.compiled
            if (
                compiled is not None
                and isinstance(compiled.statement, Insert)
                and compiled.statement.table.name == LaunchplaneIdempotencyRow.__tablename__
            ):
                raise RuntimeError("Fixture lost receipt commit")

        event.listen(self.store._engine, "before_cursor_execute", refuse_receipt)
        try:
            with self.assertRaisesRegex(RuntimeError, "Fixture lost receipt commit"):
                self.exclude()
        finally:
            event.remove(self.store._engine, "before_cursor_execute", refuse_receipt)
        self.assertEqual(self.admit(self.peer), (self.operation, True))
        self.assertEqual(self.exclude(), self.operation)

    def test_unreadable_exclusion_refuses_both_paths_with_a_fixed_conflict(self) -> None:
        self.exclude()
        receipt = self.exclusion.receipt("fixture")
        changes: tuple[dict[str, object], ...] = (
            {"response_payload": {}},
            {"scope": "foreign"},
            {"state": "unknown"},
        )
        for change in changes:
            with self.subTest(change=change):
                with self.store._session_factory() as session:
                    row = session.get(LaunchplaneIdempotencyRow, receipt.record_id)
                    assert row is not None
                    row.payload = {**receipt.model_dump(mode="json"), **change}
                    session.commit()
                with self.assertRaises(TestingRebuildRequestConflict):
                    self.exclude(self.peer)
                with self.assertRaises(TestingRebuildRequestConflict):
                    self.admit(self.peer)
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), ())


class TestingRebuildSettlementPostgresTests(TestingRebuildSettlementTests):
    def setUp(self) -> None:
        from tests.test_postgres_integration import _head_postgres_database

        self.open_store(self.enterContext(_head_postgres_database()))

    def race(self, *, exclusion_wins: bool) -> tuple[object, object]:
        acquired, attempted, release = Event(), Event(), Event()
        pids: dict[str, int] = {}
        winner_lock = self.store._lock_testing_rebuild_request
        contender_lock = self.peer._lock_testing_rebuild_request

        def hold_winner(session: Any, **kwargs: Any) -> None:
            winner_lock(session, **kwargs)
            pids["winner"] = session.scalar(text("SELECT pg_backend_pid()"))
            acquired.set()
            if not release.wait(timeout=10):
                raise AssertionError("Winner was not released")

        def observe_contender(session: Any, **kwargs: Any) -> None:
            pids["contender"] = session.scalar(text("SELECT pg_backend_pid()"))
            attempted.set()
            contender_lock(session, **kwargs)

        def late_admission() -> object:
            try:
                return self.admit(self.peer)
            except TestingRebuildRequestConflict:
                return "excluded"

        with (
            patch.object(self.store, "_lock_testing_rebuild_request", side_effect=hold_winner),
            patch.object(self.peer, "_lock_testing_rebuild_request", side_effect=observe_contender),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            first = executor.submit(self.exclude if exclusion_wins else self.admit)
            second = None
            try:
                self.assertTrue(acquired.wait(timeout=10))
                second = executor.submit(
                    late_admission if exclusion_wins else self.exclude,
                    *(() if exclusion_wins else (self.peer,)),
                )
                self.assertTrue(attempted.wait(timeout=10))
                self.assertNotEqual(pids["winner"], pids["contender"])
                blocked = False
                deadline = time.monotonic() + 5
                with self.store._engine.connect() as observer:
                    self.assertNotIn(
                        observer.scalar(text("SELECT pg_backend_pid()")), pids.values()
                    )
                    while not second.done() and time.monotonic() < deadline:
                        blocked = bool(
                            observer.scalar(
                                text("SELECT :winner = ANY(pg_blocking_pids(:contender))"), pids
                            )
                        )
                        if blocked:
                            break
                        release.wait(timeout=0.01)
                self.assertTrue(blocked, "Contender did not wait on the exact winner's native lock")
            finally:
                release.set()
                first.exception(timeout=15)
                if second is not None:
                    second.exception(timeout=15)
            assert second is not None
            return first.result(), second.result()

    def test_exclusion_commits_before_a_waiting_admission(self) -> None:
        self.assertEqual(self.race(exclusion_wins=True), (self.exclusion, "excluded"))
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), ())

    def test_admission_commits_before_a_waiting_settlement(self) -> None:
        self.assertEqual(self.race(exclusion_wins=False), ((self.operation, True), self.operation))
        self.assertEqual(self.exclude(), self.operation)

    def test_exclusion_fences_the_same_actor_key_in_another_product_and_context(self) -> None:
        self.operation = OdooStableTargetReplacementOperationRecord.model_validate(
            {
                **self.operation.model_dump(mode="python"),
                "product": "another-product",
                "context": "another-context",
                "request_fingerprint": "another-intent",
                "request": self.operation.request.model_copy(update={"product": "another-product"}),
            }
        )
        self.assertEqual(self.race(exclusion_wins=True), (self.exclusion, "excluded"))
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), ())
