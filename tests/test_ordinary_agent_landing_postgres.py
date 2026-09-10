"""Real transaction contention for landing preparation and mutable authority."""

from concurrent.futures import ThreadPoolExecutor
import os
import unittest
from unittest.mock import patch

from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentLandingFinalization,
    OrdinaryAgentUnknownOutcome,
)
from control_plane.storage.postgres import (
    LaunchplaneOrdinaryAgentLandingPreparationRow,
    LaunchplaneOrdinaryAgentSemanticDispatchRow,
    LaunchplaneOrdinaryAgentLandingBindingRow,
)
from tests import test_postgres_integration as postgres_support
from tests import test_ordinary_agent_session_storage as session_support
from tests import test_ordinary_agent_landing_storage as landing_support
from tests import test_ordinary_agent_landing_retry_storage as retry_support


@unittest.skipUnless(
    os.environ.get("LAUNCHPLANE_TEST_POSTGRES_URL"), "isolated PostgreSQL not configured"
)
class OrdinaryAgentLandingPostgresTests(unittest.TestCase):
    def test_mixed_consumed_and_terminal_retry_reuses_existing_effect(self) -> None:
        with postgres_support._store_for_fresh_head_database() as store:
            session_fixture = session_support.OrdinaryAgentSessionStorageTests()
            session_fixture.prepare_store(store)
            self.addCleanup(session_fixture.doCleanups)
            fixture = retry_support.OrdinaryAgentLandingRetryStorageTests()
            fixture.prepare_retry_fixture(session_fixture)
            first = fixture._finalize(fixture.landing.reserve().preparation)
            charged = fixture._actions_used()
            fixture._record_deadline_and_reacquire(first)
            second = fixture._reserve_retry(first.preparation.preparation_id).preparation
            observed_second, _ = fixture._issue_and_observe(second)
            terminal_second = store.close_ordinary_landing_preparation(
                preparation_id=observed_second.preparation_id,
                expected_revision=observed_second.revision,
                reason_code="provider_wait",
            )
            store.close_ordinary_agent_custody_issue_attempt(
                attempt_id=terminal_second.custody_attempt_id,
                reason="confirmed_revoked",
            )
            fixture._yield_and_reacquire()
            third = fixture._reserve_retry(terminal_second.preparation_id).preparation
            finalized = fixture._finalize(third)
            self.assertEqual((third.attempt_ordinal, finalized.child.semantic_ordinal), (3, 2))
            self.assertEqual(finalized.effect.effect_id, first.effect.effect_id)
            self.assertEqual(finalized.effect.command_sha256, first.effect.command_sha256)
            self.assertEqual(fixture._actions_used(), charged)

    def test_concurrent_retry_reservations_and_finalizers_create_one_successor(self) -> None:
        with postgres_support._store_for_fresh_head_database() as store:
            session_fixture = session_support.OrdinaryAgentSessionStorageTests()
            session_fixture.prepare_store(store)
            self.addCleanup(session_fixture.doCleanups)
            fixture = retry_support.OrdinaryAgentLandingRetryStorageTests()
            fixture.prepare_retry_fixture(session_fixture)
            first = fixture._finalize(fixture.landing.reserve().preparation)
            charged = fixture._actions_used()
            fixture._record_deadline_and_reacquire(first)

            with ThreadPoolExecutor(max_workers=2) as executor:
                reservations = tuple(
                    executor.map(
                        lambda _: fixture._reserve_retry(first.preparation.preparation_id),
                        range(2),
                    )
                )
            self.assertEqual(
                sorted(item.disposition for item in reservations), ["created", "replay"]
            )
            self.assertEqual(reservations[0].preparation, reservations[1].preparation)
            observed, proposal = fixture._issue_and_observe(reservations[0].preparation)

            def finalize(_: int) -> OrdinaryAgentLandingFinalization:
                return store.finalize_ordinary_landing_preparation(
                    preparation_id=observed.preparation_id,
                    expected_revision=observed.revision,
                    controller_fence=fixture.landing.fence,
                    proposal=proposal,
                    custody_attempt_id=observed.custody_attempt_id,
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                finalizations = tuple(executor.map(finalize, range(2)))
            self.assertEqual(
                sorted(item.disposition for item in finalizations), ["created", "replay"]
            )
            self.assertEqual(finalizations[0].child, finalizations[1].child)
            self.assertEqual(finalizations[0].effect.effect_id, first.effect.effect_id)
            self.assertEqual(finalizations[0].effect.dispatch_count, 2)
            self.assertEqual(fixture._actions_used(), charged)
            with store._session_factory() as session:
                children = tuple(
                    session.query(LaunchplaneOrdinaryAgentSemanticDispatchRow)
                    .filter(
                        LaunchplaneOrdinaryAgentSemanticDispatchRow.effect_id
                        == first.effect.effect_id
                    )
                    .all()
                )
                bindings = tuple(
                    session.query(LaunchplaneOrdinaryAgentLandingBindingRow)
                    .filter(
                        LaunchplaneOrdinaryAgentLandingBindingRow.effect_id
                        == first.effect.effect_id
                    )
                    .all()
                )
            self.assertEqual(
                sorted(item.semantic_ordinal for item in children),
                [1, 2],
            )
            self.assertEqual(
                sorted(item.dispatch_ordinal for item in bindings),
                [1, 2],
            )

    def test_dispatch_progress_preserves_immutable_finalization_replay(self) -> None:
        with postgres_support._store_for_fresh_head_database() as store:
            session_fixture = session_support.OrdinaryAgentSessionStorageTests()
            session_fixture.prepare_store(store)
            self.addCleanup(session_fixture.doCleanups)
            fixture = landing_support.OrdinaryAgentLandingStorageTests()
            fixture.prepare_landing_fixture(session_fixture)
            preparation, proposal = fixture.observed_proposal()
            finalized = fixture.finalize(preparation, proposal)
            store.record_ordinary_semantic_outcome(
                child_id=finalized.child.child_id,
                typed_outcome=OrdinaryAgentUnknownOutcome(reason="response_ambiguous"),
            )
            self.assertEqual(fixture.finalize(preparation, proposal).disposition, "replay")
            history = store.read_ordinary_agent_effect_history(effect_id=finalized.effect.effect_id)
            self.assertEqual(history.effect.state, "reconciliation_required")
            assert history.outcome is not None
            assert history.child is not None
            self.assertEqual(history.outcome.kind, "unknown")
            self.assertEqual(history.child.child_id, finalized.child.child_id)
            for model, identity in (
                (LaunchplaneOrdinaryAgentLandingPreparationRow, preparation.preparation_id),
                (LaunchplaneOrdinaryAgentSemanticDispatchRow, finalized.child.child_id),
                (LaunchplaneOrdinaryAgentLandingBindingRow, preparation.preparation_id),
            ):
                with self.subTest(table=model.__tablename__):
                    with store._session_factory() as session:
                        row = session.get(model, identity)
                        assert isinstance(
                            row,
                            (
                                LaunchplaneOrdinaryAgentLandingPreparationRow,
                                LaunchplaneOrdinaryAgentSemanticDispatchRow,
                                LaunchplaneOrdinaryAgentLandingBindingRow,
                            ),
                        )
                        row.payload = {**row.payload, "reason_code": "rewritten"}
                        with self.assertRaises(DBAPIError):
                            session.commit()
                        session.rollback()
            self.assertEqual(fixture.finalize(preparation, proposal).disposition, "replay")

    def test_concurrent_finalizers_create_one_dispatch_and_one_history_result(self) -> None:
        with postgres_support._store_for_fresh_head_database() as store:
            session_fixture = session_support.OrdinaryAgentSessionStorageTests()
            session_fixture.prepare_store(store)
            self.addCleanup(session_fixture.doCleanups)
            fixture = landing_support.OrdinaryAgentLandingStorageTests()
            fixture.prepare_landing_fixture(session_fixture)
            preparation, proposal = fixture.observed_proposal()
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = tuple(
                    executor.map(lambda _: fixture.finalize(preparation, proposal), range(2))
                )
            self.assertEqual(
                sorted(result.disposition for result in results), ["created", "replay"]
            )
            self.assertEqual(results[0].child, results[1].child)
            self.assertEqual(
                store.read_merge_admission_record(proposal.record.admission_id), proposal.record
            )

    def test_finalization_failure_rolls_back_all_flushed_records(self) -> None:
        with postgres_support._store_for_fresh_head_database() as store:
            session_fixture = session_support.OrdinaryAgentSessionStorageTests()
            session_fixture.prepare_store(store)
            self.addCleanup(session_fixture.doCleanups)
            fixture = landing_support.OrdinaryAgentLandingStorageTests()
            fixture.prepare_landing_fixture(session_fixture)
            fixture.test_failure_after_flush_leaves_no_orphan_admission_or_effect()

    def test_first_owner_event_contends_but_another_pr_progresses(self) -> None:
        with postgres_support._store_for_fresh_head_database() as store:
            session_fixture = session_support.OrdinaryAgentSessionStorageTests()
            session_fixture.prepare_store(store)
            self.addCleanup(session_fixture.doCleanups)
            fixture = landing_support.OrdinaryAgentLandingStorageTests()
            fixture.prepare_landing_fixture(session_fixture)
            preparation = fixture.reserve().preparation
            original = postgres_support._owner_acceptance_event()
            record = original.model_copy(
                update={
                    "binding": original.binding.model_copy(
                        update={
                            "repository_id": str(preparation.target.repository_id),
                            "repository": preparation.target.repository,
                            "pull_request_number": preparation.entry.pull_request_number,
                        }
                    )
                }
            )
            other = record.model_copy(
                update={
                    "event_id": "unrelated-owner-event",
                    "binding": record.binding.model_copy(
                        update={
                            "pull_request_number": preparation.entry.pull_request_number + 1,
                        }
                    ),
                }
            )

            begin = store._begin_serialized_write

            def bounded_writer(session: Session) -> None:
                begin(session)
                session.execute(text("SET LOCAL lock_timeout = '150ms'"))

            with store._session_factory() as held:
                before = store._ordinary_landing_authority(held, preparation=preparation)
                # There is no event or sequence row yet. A row lock alone cannot
                # protect this absence; the real writer must share the scope lock.
                with patch.object(store, "_begin_serialized_write", side_effect=bounded_writer):
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(store.write_owner_acceptance_event_record, record)
                        with self.assertRaises(OperationalError):
                            future.result(timeout=5)
                        self.assertEqual(
                            executor.submit(
                                store.write_owner_acceptance_event_record, other
                            ).result(timeout=5),
                            "written",
                        )
                held.rollback()
            self.assertEqual(store.write_owner_acceptance_event_record(record), "written")
            with store._session_factory() as session:
                after = store._ordinary_landing_authority(session, preparation=preparation)
            self.assertNotEqual(before, after)

    def test_preparation_replay_charges_once_and_history_rewrite_is_rejected(self) -> None:
        with postgres_support._store_for_fresh_head_database() as store:
            session_fixture = session_support.OrdinaryAgentSessionStorageTests()
            session_fixture.prepare_store(store)
            self.addCleanup(session_fixture.doCleanups)
            fixture = landing_support.OrdinaryAgentLandingStorageTests()
            fixture.prepare_landing_fixture(session_fixture)
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = tuple(executor.map(lambda _: fixture.reserve(), range(2)))
            self.assertEqual(sorted(item.disposition for item in results), ["created", "replay"])
            preparation = results[0].preparation
            with self.assertRaises(DBAPIError), store._session_factory() as session:
                session.execute(
                    update(LaunchplaneOrdinaryAgentLandingPreparationRow)
                    .where(
                        LaunchplaneOrdinaryAgentLandingPreparationRow.preparation_id
                        == preparation.preparation_id
                    )
                    .values(action_ordinal=2)
                )
                session.commit()
            closed = store.close_ordinary_landing_preparation(
                preparation_id=preparation.preparation_id,
                expected_revision=preparation.revision,
                reason_code="process_interrupted",
            )
            self.assertEqual(fixture.reserve().preparation, closed)
            self.assertIsNone(
                store.read_ordinary_landing_finalization(preparation_id=closed.preparation_id)
            )
