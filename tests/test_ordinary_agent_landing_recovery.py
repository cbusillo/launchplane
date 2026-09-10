"""Recover persisted landing results across outcome/progress crash boundaries."""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from control_plane.contracts.merge_train_batch import MergeTrainBatchLandingEntry
from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentCompletedOutcome,
    OrdinaryAgentPullRequestObservation,
)
from control_plane.merge_admission import GuardedMergeAdmission, MergeAdmissionDeniedError
from control_plane.ordinary_agent_admission_store import OrdinaryAgentAdmissionAdapter
from control_plane.merge_train_github import (
    MergeTrainGitHubError,
    RecordingMergeTrainGitHubTransport,
)
from control_plane.ordinary_agent_effect_reconciliation import reconcile_ordinary_effect_once
from control_plane.ordinary_agent_landing_execution import OrdinaryLandingRecoveryRequired
from control_plane.ordinary_agent_landing_recovery import recover_ordinary_landing_entry
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from tests import test_ordinary_agent_landing_execution as support


class OrdinaryLandingRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = support.OrdinaryAgentLandingExecutionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def recover(self) -> MergeTrainBatchLandingEntry:
        fixture = self.fixture
        assert fixture.preparation is not None
        return recover_ordinary_landing_entry(
            store=fixture.store,
            request=fixture.fixture.request,
            preparation_id=fixture.preparation.preparation_id,
            candidate_record=fixture.fixture.candidate,
            landing_plan_record=fixture.fixture.plan,
            guard_factory=fixture.guard,
            checkpoint=fixture.checkpoints.append,
        )

    def test_landed_outcome_survives_failed_progress_without_duplicate_outcome_or_provider(
        self,
    ) -> None:
        fixture = self.fixture
        fixture.checkpoint_error = RuntimeError("lost progress")
        with self.assertRaisesRegex(RuntimeError, "lost progress"):
            fixture.run_landing()
        before = len(fixture.inner.requests)
        with patch(
            "control_plane.ordinary_agent_custody.mint_ordinary_agent_installation_token"
        ) as mint:
            entry = self.recover()
        mint.assert_not_called()
        self.assertEqual(len(fixture.inner.requests), before)
        self.assertEqual(entry.merge_commit_sha, fixture.result_sha)
        self.assertEqual(fixture.checkpoints, [entry])
        assert fixture.preparation is not None
        finalization = fixture.store.read_ordinary_landing_finalization(
            preparation_id=fixture.preparation.preparation_id
        )
        assert finalization is not None
        outcomes = fixture.store.list_merge_landing_outcome_records(
            admission_id=finalization.admission.admission_id
        )
        self.assertEqual(len(outcomes), 1)
        self.recover()
        self.assertEqual(
            fixture.store.list_merge_landing_outcome_records(
                admission_id=finalization.admission.admission_id
            ),
            outcomes,
        )

    def test_completed_effect_before_landing_outcome_recovers_from_stored_proof(self) -> None:
        with patch.object(
            GuardedMergeAdmission, "record_landed", side_effect=RuntimeError("lost outcome")
        ):
            with self.assertRaisesRegex(RuntimeError, "lost outcome"):
                self.fixture.run_landing()
        count = len(self.fixture.inner.requests)
        entry = self.recover()
        self.assertEqual(entry.status, "merged")
        self.assertEqual(len(self.fixture.inner.requests), count)
        self.assertEqual(self.fixture.checkpoints, [entry])
        assert self.fixture.preparation is not None
        finalization = self.fixture.store.read_ordinary_landing_finalization(
            preparation_id=self.fixture.preparation.preparation_id
        )
        assert finalization is not None
        outcomes = self.fixture.store.list_merge_landing_outcome_records(
            admission_id=finalization.admission.admission_id
        )
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].status, "landed")
        self.assertTrue(outcomes[0].exact_landing_confirmed)
        self.assertTrue(outcomes[0].provider_effect_attempted)

    def test_unknown_dispatch_does_not_checkpoint_or_retry(self) -> None:
        self.fixture.merge_error = MergeTrainGitHubError("lost response")
        with self.assertRaises(MergeTrainGitHubError):
            self.fixture.run_landing()
        count = len(self.fixture.inner.requests)
        with self.assertRaises(OrdinaryLandingRecoveryRequired):
            self.recover()
        self.assertEqual(len(self.fixture.inner.requests), count)
        self.assertEqual(self.fixture.checkpoints, [])

    def test_superseded_binding_cannot_recover_historical_progress(self) -> None:
        self.fixture.checkpoint_error = RuntimeError("lost progress")
        with self.assertRaises(RuntimeError):
            self.fixture.run_landing()
        self.fixture.fixture.request = self.fixture.fixture.request.model_copy(
            update={"binding_revision": self.fixture.fixture.request.binding_revision + 1}
        )
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "landing_history_binding_conflict"
        ):
            self.recover()
        self.assertEqual(self.fixture.checkpoints, [])

    def test_unavailable_adapter_scope_is_a_binding_denial_without_outcome_write(self) -> None:
        self.fixture.checkpoint_error = RuntimeError("lost progress")
        with self.assertRaises(RuntimeError):
            self.fixture.run_landing()
        with (
            patch.object(
                OrdinaryAgentAdmissionAdapter,
                "read_merge_admission_record",
                side_effect=MergeAdmissionDeniedError("unavailable"),
            ),
            patch.object(GuardedMergeAdmission, "record_landed") as write,
        ):
            with self.assertRaisesRegex(
                OrdinaryAgentSessionAdmissionDenied, "landing_history_binding_conflict"
            ):
                self.recover()
        write.assert_not_called()
        self.assertEqual(self.fixture.checkpoints, [])

    def test_pr_only_completion_cannot_create_an_exact_base_landing_outcome(self) -> None:
        with patch.object(
            GuardedMergeAdmission, "record_landed", side_effect=RuntimeError("lost outcome")
        ):
            with self.assertRaises(RuntimeError):
                self.fixture.run_landing()
        assert self.fixture.preparation is not None
        finalization = self.fixture.store.read_ordinary_landing_finalization(
            preparation_id=self.fixture.preparation.preparation_id
        )
        assert finalization is not None
        preparation = finalization.preparation
        history = self.fixture.store.read_ordinary_agent_effect_history(
            effect_id=finalization.effect.effect_id
        )
        # This is a valid merged-PR history shape. It proves the exact merge
        # commit, but carries no observation of the current base ref.
        observed = history.model_copy(
            update={
                "outcome": OrdinaryAgentCompletedOutcome(
                    result_sha=self.fixture.result_sha,
                    proof=OrdinaryAgentPullRequestObservation(
                        repository=preparation.target.repository,
                        number=preparation.entry.pull_request_number,
                        head_sha=preparation.entry.expected_head_sha,
                        base_ref=preparation.target.base_branch,
                        base_sha=preparation.expected_base_sha,
                        state="closed",
                        merged=True,
                        merge_commit_sha=self.fixture.result_sha,
                        merge_commit_tree_sha=preparation.expected_merge_tree_sha,
                        merge_commit_parents=(
                            preparation.expected_base_sha,
                            preparation.entry.expected_head_sha,
                        ),
                    ),
                )
            }
        )
        with (
            patch.object(
                self.fixture.store, "read_ordinary_agent_effect_history", return_value=observed
            ),
            patch.object(GuardedMergeAdmission, "record_landed") as write,
        ):
            with self.assertRaises(OrdinaryLandingRecoveryRequired):
                self.recover()
        write.assert_not_called()
        self.assertEqual(self.fixture.checkpoints, [])

    def test_reconciliation_predecessor_is_followed_by_one_terminal_outcome(self) -> None:
        with patch.object(
            GuardedMergeAdmission, "record_landed", side_effect=RuntimeError("lost outcome")
        ):
            with self.assertRaises(RuntimeError):
                self.fixture.run_landing()
        assert self.fixture.preparation is not None
        finalization = self.fixture.store.read_ordinary_landing_finalization(
            preparation_id=self.fixture.preparation.preparation_id
        )
        assert finalization is not None and finalization.preparation.evidence is not None
        guard = self.fixture.guard(finalization.preparation, finalization.preparation.evidence)
        previous = guard.record_reconcile_required(
            admission=finalization.admission,
            reason="process_interrupted",
            message="outcome write interrupted",
            observed_at=datetime.fromtimestamp(
                finalization.child.dispatch_checkpoint_at, timezone.utc
            ).isoformat(),
        )
        self.recover()
        self.recover()
        outcomes = self.fixture.store.list_merge_landing_outcome_records(
            admission_id=finalization.admission.admission_id
        )
        self.assertEqual([item.status for item in outcomes], ["landed", "reconcile_required"])
        self.assertEqual(outcomes[0].prior_outcome_id, previous.outcome_id)

    def test_lost_response_reconciles_ahead_base_then_recovers_exact_outcome_once(self) -> None:
        fixture = self.fixture
        fixture.merge_error = MergeTrainGitHubError("lost response")
        with self.assertRaises(MergeTrainGitHubError):
            fixture.run_landing()
        assert fixture.preparation is not None
        finalization = fixture.store.read_ordinary_landing_finalization(
            preparation_id=fixture.preparation.preparation_id
        )
        assert finalization is not None
        preparation = finalization.preparation
        assert preparation.evidence is not None
        guard = fixture.guard(preparation, preparation.evidence)
        previous = guard.record_reconcile_required(
            admission=finalization.admission,
            reason="provider_transport_ambiguous",
            message="provider response unavailable",
            observed_at=datetime.fromtimestamp(fixture.now, timezone.utc).isoformat(),
        )
        fixture.now += 16
        fixture.fixture.fixture.fixture.clock.return_value = datetime.fromtimestamp(
            fixture.now, timezone.utc
        ).isoformat()
        base_sha, base_tree = "7" * 40, "8" * 40
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                {
                    "number": preparation.entry.pull_request_number,
                    "state": "closed",
                    "merged": True,
                    "merge_commit_sha": fixture.result_sha,
                    "head": {
                        "sha": preparation.entry.expected_head_sha,
                        "repo": {"id": preparation.target.repository_id},
                    },
                    "base": {
                        "sha": preparation.expected_base_sha,
                        "ref": preparation.target.base_branch,
                        "repo": {"id": preparation.target.repository_id},
                    },
                },
                {
                    "sha": fixture.result_sha,
                    "tree": {"sha": preparation.expected_merge_tree_sha},
                    "parents": [
                        {"sha": preparation.expected_base_sha},
                        {"sha": preparation.entry.expected_head_sha},
                    ],
                    "message": "landed merge",
                },
                {
                    "ref": "refs/heads/" + preparation.target.base_branch,
                    "object": {"sha": base_sha},
                },
                {
                    "sha": base_sha,
                    "tree": {"sha": base_tree},
                    "parents": [{"sha": fixture.result_sha}],
                    "message": "later base commit",
                },
                {
                    "status": "ahead",
                    "base_commit": {"sha": fixture.result_sha},
                    "merge_base_commit": {"sha": fixture.result_sha},
                },
            )
        )
        with (
            patch(
                "control_plane.ordinary_agent_custody.resolve_ordinary_agent_github_app_identity",
                return_value=SimpleNamespace(identity=SimpleNamespace(app_id=42)),
            ),
            patch(
                "control_plane.ordinary_agent_custody.mint_ordinary_agent_installation_token",
                side_effect=fixture.mint,
            ),
        ):
            history = reconcile_ordinary_effect_once(
                store=fixture.store,
                request=fixture.fixture.request,
                effect_id=finalization.effect.effect_id,
                api_request=fixture.provider_request,
                transport_factory=lambda _: transport,
                monotonic=lambda: 0,
                utc_now=lambda: datetime.fromtimestamp(fixture.now, timezone.utc),
            )
        self.assertEqual(history.effect.state, "completed_observed")
        self.assertEqual([item.method for item in transport.requests], ["GET"] * 5)
        provider_calls = len(fixture.inner.requests)
        entry = self.recover()
        self.recover()
        self.assertEqual(len(fixture.inner.requests), provider_calls)
        self.assertEqual(entry.merge_commit_sha, fixture.result_sha)
        outcomes = fixture.store.list_merge_landing_outcome_records(
            admission_id=finalization.admission.admission_id
        )
        self.assertEqual([item.status for item in outcomes], ["landed", "reconcile_required"])
        landed = outcomes[0]
        self.assertEqual(landed.prior_outcome_id, previous.outcome_id)
        self.assertEqual(
            (landed.observed_base_sha, landed.observed_base_tree_sha), (base_sha, base_tree)
        )
        self.assertEqual(landed.merge_commit_tree_sha, preparation.expected_merge_tree_sha)
        self.assertTrue(landed.exact_landing_confirmed)
        self.assertTrue(landed.provider_effect_attempted)
