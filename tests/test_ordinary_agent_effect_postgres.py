"""Real database races and immutable child history; never uses a runtime database."""

from concurrent.futures import ThreadPoolExecutor
import os
import unittest

from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from control_plane.contracts.ordinary_agent_session_lifecycle import OrdinaryAgentLeaseRecord
from control_plane.contracts.ordinary_agent_effect import OrdinaryAgentUnknownOutcome
from control_plane.storage.postgres import (
    LaunchplaneOrdinaryAgentEffectRow,
    LaunchplaneOrdinaryAgentLeaseRow,
    LaunchplaneOrdinaryAgentSemanticOutcomeRow,
    LaunchplaneOrdinaryAgentReadAttemptRow,
)
from tests import test_postgres_integration as postgres_support
from tests import test_ordinary_agent_session_storage as session_support
from tests import test_ordinary_agent_effect_storage as effect_support


@unittest.skipUnless(
    os.environ.get("LAUNCHPLANE_TEST_POSTGRES_URL"), "isolated PostgreSQL not configured"
)
class OrdinaryAgentEffectPostgresTests(unittest.TestCase):
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
