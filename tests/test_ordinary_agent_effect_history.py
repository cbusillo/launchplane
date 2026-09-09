"""Restart history preserves outcomes without provider or session authority."""

import unittest

from tests import test_ordinary_agent_landing_dispatch as dispatch_support


class OrdinaryAgentEffectHistoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = dispatch_support.LandingDispatchTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.fixture.store
        self.effect_id = self.fixture.finalized.effect.effect_id

    def test_completed_history_survives_session_cancellation_without_provider_reads(self):
        pending = self.store.read_ordinary_agent_effect_history(effect_id=self.effect_id)
        self.assertEqual(pending.effect.state, "dispatching")
        self.assertIsNone(pending.outcome)
        inner, dispatcher = self.fixture.dispatcher(
            [{"merged": True, "sha": self.fixture.result_sha}, self.fixture.proof]
        )
        dispatcher.dispatch()
        complete = self.store.read_ordinary_agent_effect_history(effect_id=self.effect_id)
        self.assertEqual(complete.outcome.result_sha, self.fixture.result_sha)
        session_fixture = self.fixture.fixture.fixture.fixture
        self.store.cancel_ordinary_agent_session(
            proof=session_fixture.proof,
            session_id=self.fixture.finalized.preparation.session_id,
        )
        self.assertEqual(
            self.store.read_ordinary_agent_effect_history(effect_id=self.effect_id), complete
        )
        self.assertEqual(len(inner.requests), 2)
        self.assertEqual(
            self.store.read_ordinary_landing_preparation(
                preparation_id=self.fixture.finalized.preparation.preparation_id
            ).state,
            "consumed",
        )

    def test_reconciled_history_retains_unknown_response_and_exact_observation(self):
        self.fixture.reconcile()
        history = self.store.read_ordinary_agent_effect_history(effect_id=self.effect_id)
        self.assertEqual(history.effect.state, "completed_observed")
        self.assertEqual(history.outcome.kind, "unknown")
        self.assertEqual(len(history.reconciliations), 1)
        self.assertEqual(
            history.reconciliations[0].observation.merge_commit_sha, self.fixture.result_sha
        )
        self.assertEqual(history.child.semantic_ordinal, history.effect.dispatch_count)

    def test_conflicting_reconciliation_is_history_without_success_or_replay_authority(self):
        self.fixture.reconcile({"merge_commit_tree_sha": "8" * 40})
        history = self.store.read_ordinary_agent_effect_history(effect_id=self.effect_id)
        self.assertEqual(history.effect.state, "terminal_conflict")
        self.assertEqual(history.effect.reason_code, "landing_result_tree_mismatch")
        self.assertEqual(history.outcome.kind, "unknown")
        self.assertEqual(len(history.reconciliations), 1)
        self.assertEqual(history.effect.dispatch_count, 1)
