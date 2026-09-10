"""Real database races and immutable child history; never uses a runtime database."""

from concurrent.futures import ThreadPoolExecutor
import os
from threading import Barrier, Event, current_thread
import unittest

from sqlalchemy import event, func, select, update
from sqlalchemy.exc import DBAPIError

from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequestRecord,
    OrdinaryAgentLeaseRecord,
)
from control_plane.contracts.ordinary_agent_effect import OrdinaryAgentUnknownOutcome
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from control_plane.storage.postgres import (
    LaunchplaneOrdinaryAgentEffectRow,
    LaunchplaneOrdinaryAgentFiniteRequestRow,
    LaunchplaneOrdinaryAgentLeaseRow,
    LaunchplaneOrdinaryAgentSemanticOutcomeRow,
    LaunchplaneOrdinaryAgentReadAttemptRow,
    LaunchplaneOrdinaryAgentNoOpLandingFinalizationRow,
)
from tests import test_postgres_integration as postgres_support
from tests import test_ordinary_agent_session_storage as session_support
from tests import test_ordinary_agent_effect_storage as effect_support


@unittest.skipUnless(
    os.environ.get("LAUNCHPLANE_TEST_POSTGRES_URL"), "isolated PostgreSQL not configured"
)
class OrdinaryAgentEffectPostgresTests(unittest.TestCase):
    def test_concurrent_readmission_and_session_cancel_preserve_canonical_lock_order(self) -> None:
        from tests.test_ordinary_agent_readmission_storage import ReadmissionStorageFixture

        with postgres_support._store_for_fresh_head_database() as store:
            session_fixture = session_support.OrdinaryAgentSessionStorageTests()
            session_fixture.prepare_store(store)
            self.addCleanup(session_fixture.doCleanups)
            fixture = ReadmissionStorageFixture(self, session_fixture)
            _, attempt, observation, _ = fixture.observe()
            cancel_holds_authz = Event()
            finalizer_waiting_for_authz = Event()
            allow_cancel = Event()

            def after_statement(
                _connection: object,
                _cursor: object,
                statement: str,
                parameters: object,
                _context: object,
                _executemany: bool,
            ) -> None:
                if (
                    current_thread().name.startswith("readmission-cancel")
                    and "pg_advisory_xact_lock" in statement
                    and "active-authz-policy" in str(parameters)
                ):
                    cancel_holds_authz.set()
                    if not allow_cancel.wait(timeout=5):
                        raise AssertionError("session cancellation was not resumed")

            def before_statement(
                _connection: object,
                _cursor: object,
                statement: str,
                parameters: object,
                _context: object,
                _executemany: bool,
            ) -> None:
                if (
                    current_thread().name.startswith("readmission-finalizer")
                    and "pg_advisory_xact_lock" in statement
                    and "active-authz-policy" in str(parameters)
                ):
                    finalizer_waiting_for_authz.set()

            event.listen(store._engine, "after_cursor_execute", after_statement)
            event.listen(store._engine, "before_cursor_execute", before_statement)
            try:
                with (
                    ThreadPoolExecutor(
                        max_workers=1, thread_name_prefix="readmission-cancel"
                    ) as cancel_executor,
                    ThreadPoolExecutor(
                        max_workers=1, thread_name_prefix="readmission-finalizer"
                    ) as finalizer_executor,
                ):
                    cancel_future = cancel_executor.submit(
                        store.cancel_ordinary_agent_session,
                        proof=session_fixture.proof,
                        session_id=fixture.request.session_id,
                    )
                    self.assertTrue(cancel_holds_authz.wait(timeout=5))
                    finalize_future = finalizer_executor.submit(
                        fixture.finalize,
                        attempt=attempt,
                        observation=observation,
                    )
                    self.assertTrue(finalizer_waiting_for_authz.wait(timeout=5))
                    allow_cancel.set()
                    cancel_future.result(timeout=5)
                    with self.assertRaises(OrdinaryAgentSessionAdmissionDenied):
                        finalize_future.result(timeout=5)
            finally:
                allow_cancel.set()
                event.remove(store._engine, "after_cursor_execute", after_statement)
                event.remove(store._engine, "before_cursor_execute", before_statement)

            with store._session_factory() as session:
                request_row = session.get(
                    LaunchplaneOrdinaryAgentFiniteRequestRow,
                    fixture.request.request_id,
                )
                attempt_row = session.get(
                    LaunchplaneOrdinaryAgentReadAttemptRow, attempt.attempt_id
                )
                assert request_row is not None and attempt_row is not None
                request = OrdinaryAgentFiniteRequestRecord.model_validate(request_row.payload)
                self.assertIsNotNone(request.cancellation_requested_at)
                self.assertEqual(request.binding_revision, 1)
                self.assertEqual(attempt_row.state, "completed")

    def test_concurrent_readmission_and_reacquire_cannot_cross_binding_revision(self) -> None:
        from tests.test_ordinary_agent_readmission_storage import ReadmissionStorageFixture

        with postgres_support._store_for_fresh_head_database() as store:
            session_fixture = session_support.OrdinaryAgentSessionStorageTests()
            session_fixture.prepare_store(store)
            self.addCleanup(session_fixture.doCleanups)
            fixture = ReadmissionStorageFixture(self, session_fixture)
            _, attempt, observation, _ = fixture.observe()
            barrier = Barrier(2)

            def finalize() -> object:
                barrier.wait()
                return fixture.finalize(attempt=attempt, observation=observation)

            def acquire() -> object:
                barrier.wait()
                return store.acquire_ordinary_merge_train_controller_state_record(
                    claim_fence=fixture.effect.claim.claim_fence,
                    expected_binding_revision=1,
                    policy_key=fixture.effect.merge_policy.policy.policies[0].policy_key,
                    policy_sha256=fixture.effect.merge_policy.policy_sha256,
                    lease_seconds=30,
                    initial_active_action="snapshot",
                    initial_active_phase="read",
                    adoptable_active_actions=("snapshot",),
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = (executor.submit(finalize), executor.submit(acquire))
                outcomes: list[object] = []
                failures: list[Exception] = []
                for future in futures:
                    try:
                        outcomes.append(future.result())
                    except Exception as exc:  # exact loser depends on lock acquisition order
                        failures.append(exc)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], OrdinaryAgentSessionAdmissionDenied)
            self.assertIn(
                str(failures[0]),
                {"binding_revision_conflict", "refresh_progress_must_be_retired"},
            )
            with store._session_factory() as session:
                row = session.get(LaunchplaneOrdinaryAgentReadAttemptRow, attempt.attempt_id)
                assert row is not None
                request = session.get(
                    LaunchplaneOrdinaryAgentFiniteRequestRow,
                    fixture.request.request_id,
                )
                assert request is not None
                persisted = OrdinaryAgentFiniteRequestRecord.model_validate(request.payload)
                if row.state == "consumed":
                    self.assertEqual(persisted.binding_revision, 2)
                    self.assertEqual(row.revision, attempt.revision + 1)
                else:
                    self.assertEqual(row.state, "completed")
                    self.assertEqual(persisted.binding_revision, 1)

    def test_concurrent_refresh_rebind_has_one_atomic_transition_and_one_replay(self) -> None:
        with postgres_support._store_for_fresh_head_database() as store:
            session_fixture = session_support.OrdinaryAgentSessionStorageTests()
            session_fixture.prepare_store(store)
            self.addCleanup(session_fixture.doCleanups)
            fixture = effect_support.OrdinaryAgentEffectStorageTests()
            fixture.prepare_effect_fixture(session_fixture)
            effect, fence, completed = fixture.complete_refresh()

            def rebind() -> OrdinaryAgentFiniteRequestRecord:
                return store.rebind_ordinary_agent_after_head_refresh(
                    effect_id=effect.effect_id,
                    expected_effect_revision=completed.revision,
                    controller_fence=fence,
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = tuple(executor.map(lambda _: rebind(), range(2)))
            self.assertEqual(results[0], results[1])
            self.assertEqual(results[0].binding_revision, 2)
            controller = store.list_merge_train_controller_state_records(
                repository=fixture.request.target.repository,
                base_branch=fixture.request.target.base_branch,
                limit=1,
            )[0]
            self.assertEqual(controller.status, "idle")
            self.assertIsNone(controller.ordinary_job_binding)
            self.assertEqual(controller.last_owner, fence.lease_owner)

    def test_concurrent_no_op_finalization_has_one_created_and_one_replay(self) -> None:
        from tests.test_ordinary_agent_noop_storage import NoOpLandingStorageFixture

        with postgres_support._store_for_fresh_head_database() as store:
            session_fixture = session_support.OrdinaryAgentSessionStorageTests()
            session_fixture.prepare_store(store)
            self.addCleanup(session_fixture.doCleanups)
            fixture = NoOpLandingStorageFixture(self, session_fixture)
            preparation, proposal = fixture.observed()
            successor = fixture.successor(preparation)

            def finalize() -> str:
                return fixture.finalize(preparation, proposal, successor).disposition

            with ThreadPoolExecutor(max_workers=2) as executor:
                dispositions = tuple(executor.map(lambda _: finalize(), range(2)))
            self.assertEqual(sorted(dispositions), ["created", "replay"])
            with store._session_factory() as session:
                self.assertEqual(
                    session.scalar(
                        select(func.count()).select_from(
                            LaunchplaneOrdinaryAgentNoOpLandingFinalizationRow
                        )
                    ),
                    1,
                )

    def test_recovery_snapshot_is_repeatable_and_does_not_block_writer(self) -> None:
        with postgres_support._store_for_fresh_head_database() as store:
            session_fixture = session_support.OrdinaryAgentSessionStorageTests()
            session_fixture.prepare_store(store)
            self.addCleanup(session_fixture.doCleanups)
            fixture = effect_support.OrdinaryAgentEffectStorageTests()
            fixture.prepare_effect_fixture(session_fixture)
            fence, command = fixture.prepare_controller()
            effect = store.reserve_ordinary_agent_effect(
                request_id=fixture.request.request_id,
                expected_binding_revision=1,
                controller_fence=fence,
                command=command,
                semantic_ordinal=1,
            )
            paused, resume = Event(), Event()

            def pause_before_effect_read(
                _connection: object,
                _cursor: object,
                statement: str,
                _parameters: object,
                _context: object,
                _executemany: bool,
            ) -> None:
                if (
                    current_thread().name.startswith("snapshot-reader")
                    and statement.lstrip().upper().startswith("SELECT")
                    and "launchplane_ordinary_agent_effects" in statement
                ):
                    paused.set()
                    if not resume.wait(timeout=5):
                        raise AssertionError("snapshot reader was not resumed")

            def complete_effect() -> None:
                with store._session_factory() as session:
                    row = session.get(LaunchplaneOrdinaryAgentEffectRow, effect.effect_id)
                    assert row is not None
                    completed = effect.model_copy(
                        update={"state": "completed", "revision": effect.revision + 1}
                    )
                    row.revision = completed.revision
                    row.payload = completed.model_dump(mode="json", exclude_none=True)
                    session.commit()

            event.listen(store._engine, "before_cursor_execute", pause_before_effect_read)
            try:
                with (
                    ThreadPoolExecutor(
                        max_workers=1, thread_name_prefix="snapshot-reader"
                    ) as snapshot_executor,
                    ThreadPoolExecutor(
                        max_workers=1, thread_name_prefix="effect-writer"
                    ) as writer_executor,
                ):
                    snapshot_future = snapshot_executor.submit(
                        store.read_ordinary_agent_job_recovery_snapshot,
                        claim_fence=fixture.claim.claim_fence,
                    )
                    self.assertTrue(paused.wait(timeout=5))
                    writer_future = writer_executor.submit(complete_effect)
                    try:
                        writer_future.result(timeout=5)
                    finally:
                        resume.set()
                    before_write = snapshot_future.result(timeout=5)
            finally:
                resume.set()
                event.remove(store._engine, "before_cursor_execute", pause_before_effect_read)

            assert before_write.unresolved_effect is not None
            self.assertEqual(before_write.unresolved_effect.effect, effect)
            after_write = store.read_ordinary_agent_job_recovery_snapshot(
                claim_fence=fixture.claim.claim_fence
            )
            self.assertIsNone(after_write.unresolved_effect)
            self.assertEqual(after_write.completed_effects, 1)

    def test_two_reservations_charge_once_and_database_rejects_history_rewrite(self) -> None:
        with postgres_support._store_for_fresh_head_database() as store:
            session_fixture = session_support.OrdinaryAgentSessionStorageTests()
            session_fixture.prepare_store(store)
            self.addCleanup(session_fixture.doCleanups)
            fixture = effect_support.OrdinaryAgentEffectStorageTests()
            fixture.prepare_effect_fixture(session_fixture)
            fence, command = fixture.prepare_controller()
            read = store.reserve_ordinary_agent_snapshot_attempt(
                request_id=fixture.request.request_id,
                expected_binding_revision=1,
                controller_fence=fence,
            )
            with self.assertRaises(DBAPIError), store._session_factory() as session:
                session.execute(
                    update(LaunchplaneOrdinaryAgentReadAttemptRow)
                    .where(LaunchplaneOrdinaryAgentReadAttemptRow.attempt_id == read.attempt_id)
                    .values(request_id="another-job")
                )
                session.commit()

            def reserve() -> str:
                return store.reserve_ordinary_agent_effect(
                    request_id=fixture.request.request_id,
                    expected_binding_revision=1,
                    controller_fence=fence,
                    command=command,
                    semantic_ordinal=1,
                ).effect_id

            with ThreadPoolExecutor(max_workers=2) as executor:
                ids = tuple(executor.map(lambda _: reserve(), range(2)))
            self.assertEqual(ids[0], ids[1])
            with store._session_factory() as session:
                rows = tuple(session.scalars(select(LaunchplaneOrdinaryAgentEffectRow)))
                lease = session.get(LaunchplaneOrdinaryAgentLeaseRow, fixture.request.lease_id)
                assert lease is not None
                self.assertEqual(len(rows), 1)
                self.assertEqual(
                    OrdinaryAgentLeaseRecord.model_validate(lease.payload).budget.actions_used, 1
                )
            with self.assertRaises(DBAPIError), store._session_factory() as session:
                session.execute(
                    update(LaunchplaneOrdinaryAgentEffectRow)
                    .where(LaunchplaneOrdinaryAgentEffectRow.effect_id == ids[0])
                    .values(action_ordinal=2)
                )
                session.commit()
            effect = store.read_ordinary_agent_effect(effect_id=ids[0])
            permit = store.reserve_ordinary_custody_attempt(
                effect_id=effect.effect_id, expected_effect_revision=effect.revision
            )
            fixture.issue(permit)
            child = store.checkpoint_ordinary_semantic_dispatch(
                effect_id=effect.effect_id,
                controller_fence=fence,
                custody_attempt_id=permit.attempt_id,
                fixed_token_expires_at=session_fixture.now + 300,
            )
            store.record_ordinary_semantic_outcome(
                child_id=child.child_id,
                typed_outcome=OrdinaryAgentUnknownOutcome(reason="transport_ambiguous"),
            )
            with self.assertRaises(DBAPIError), store._session_factory() as session:
                session.execute(
                    update(LaunchplaneOrdinaryAgentSemanticOutcomeRow)
                    .where(LaunchplaneOrdinaryAgentSemanticOutcomeRow.child_id == child.child_id)
                    .values(payload={"kind": "completed", "result_sha": "invented"})
                )
                session.commit()

    def test_completion_history_uses_current_claim_and_releases_execution(self) -> None:
        with postgres_support._store_for_fresh_head_database() as store:
            session_fixture = session_support.OrdinaryAgentSessionStorageTests()
            session_fixture.prepare_store(store)
            self.addCleanup(session_fixture.doCleanups)
            fixture = effect_support.OrdinaryAgentEffectStorageTests()
            fixture.prepare_effect_fixture(session_fixture)
            # Exercise the same behavior through real row/advisory locks, without
            # inventing a parallel fake-store completion implementation.
            fixture.test_completion_requires_current_claim_and_persists_terminal_request()
