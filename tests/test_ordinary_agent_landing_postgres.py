"""Real transaction contention for landing preparation and mutable authority."""

from concurrent.futures import ThreadPoolExecutor
import os
import unittest
from unittest.mock import patch

from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

from control_plane.contracts.ordinary_agent_effect import OrdinaryAgentUnknownOutcome
from control_plane.storage.postgres import (
    LaunchplaneOrdinaryAgentLandingPreparationRow,
    LaunchplaneOrdinaryAgentSemanticDispatchRow,
    LaunchplaneOrdinaryAgentLandingBindingRow,
)
from tests import test_postgres_integration as postgres_support
from tests import test_ordinary_agent_session_storage as session_support
from tests import test_ordinary_agent_landing_storage as landing_support


@unittest.skipUnless(
    os.environ.get("LAUNCHPLANE_TEST_POSTGRES_URL"), "isolated PostgreSQL not configured"
)
class OrdinaryAgentLandingPostgresTests(unittest.TestCase):
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
