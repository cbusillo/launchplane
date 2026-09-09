"""Atomic landing admission permits one PUT and requires exact result proof."""

import unittest
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from control_plane.contracts.ordinary_agent_effect import (
    EffectState,
    OrdinaryAgentCompletedOutcome,
    OrdinaryAgentEffectRecord,
    OrdinaryAgentLandingFinalization,
    OrdinaryAgentPullRequestObservation,
    OrdinaryAgentReconciliationObservation,
    OrdinaryAgentRefObservation,
    OrdinaryAgentUnknownOutcome,
)
from control_plane.merge_train_github import (
    MergeTrainGitHubError,
    RecordingMergeTrainGitHubTransport,
)
from control_plane.ordinary_agent_github_transport import (
    DeadlineMergeTrainGitHubTransport,
    OrdinaryAgentProviderDeferred,
    OrdinaryAgentProviderEvidenceError,
)
from control_plane.ordinary_agent_landing_dispatch import (
    FinalizedOrdinaryLandingDispatcher,
    OrdinaryLandingDispatchStopped,
)
from control_plane.ordinary_agent_merge_train_executor import (
    OrdinaryAgentEffectTerminal,
    OrdinaryAgentMergeTrainEffectExecutor,
)
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from tests import test_ordinary_agent_landing_storage as landing_support


class LandingDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = landing_support.OrdinaryAgentLandingStorageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        observed, proposal = self.fixture.observed_proposal()
        self.finalized = self.fixture.store.finalize_ordinary_landing_preparation(
            preparation_id=observed.preparation_id,
            expected_revision=observed.revision,
            controller_fence=self.fixture.fence,
            proposal=proposal,
            custody_attempt_id=observed.custody_attempt_id,
        )
        p = self.finalized.preparation
        self.result_sha = "9" * 40
        self.proof: dict[str, Any] = {
            "data": {
                "rateLimit": {"cost": 1},
                "repository": {
                    "databaseId": p.target.repository_id,
                    "ref": {
                        "target": {
                            "oid": self.result_sha,
                            "tree": {"oid": p.expected_merge_tree_sha},
                            "parents": {
                                "totalCount": 2,
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [
                                    {"oid": p.expected_base_sha},
                                    {"oid": p.entry.expected_head_sha},
                                ],
                            },
                        }
                    },
                },
            }
        }
        self.now = 0.0

    def dispatcher(
        self, responses: Sequence[object], finalized: OrdinaryAgentLandingFinalization | None = None
    ) -> tuple[RecordingMergeTrainGitHubTransport, FinalizedOrdinaryLandingDispatcher]:
        inner = RecordingMergeTrainGitHubTransport(responses=tuple(responses))
        transport = DeadlineMergeTrainGitHubTransport(
            transport=inner,
            work_deadline=75,
            token_deadline=300,
            monotonic=lambda: self.now,
        )
        evidence = self.finalized.preparation.evidence
        assert evidence is not None
        return inner, FinalizedOrdinaryLandingDispatcher(
            finalization=finalized or self.finalized,
            transport=transport,
            store=self.fixture.store,
            utc_seconds=lambda: evidence.observed_at,
        )

    def state(self) -> EffectState:
        return self.fixture.store.read_ordinary_agent_effect(
            effect_id=self.finalized.effect.effect_id
        ).state

    def test_created_admission_dispatches_once_and_replay_cannot_dispatch(self) -> None:
        inner, dispatcher = self.dispatcher([{"merged": True, "sha": self.result_sha}, self.proof])
        self.assertEqual(dispatcher.dispatch(), self.result_sha)
        self.assertEqual(self.state(), "completed")
        self.assertEqual([r.method for r in inner.requests], ["PUT", "POST"])
        with self.assertRaises(OrdinaryLandingDispatchStopped):
            dispatcher.dispatch()
        replay = self.fixture.store.read_ordinary_landing_finalization(
            preparation_id=self.finalized.preparation.preparation_id
        )
        replay_inner, replay_dispatcher = self.dispatcher([], replay)
        with self.assertRaises(OrdinaryLandingDispatchStopped):
            replay_dispatcher.dispatch()
        self.assertEqual(replay_inner.requests, [])

    def test_ambiguous_merge_response_remains_unknown_without_retry(self) -> None:
        inner, dispatcher = self.dispatcher(
            [MergeTrainGitHubError("validation ambiguous", status_code=422)]
        )
        with self.assertRaises(MergeTrainGitHubError):
            dispatcher.dispatch()
        self.assertEqual(self.state(), "reconciliation_required")
        self.assertEqual(len(inner.requests), 1)
        with self.assertRaises(OrdinaryLandingDispatchStopped):
            dispatcher.dispatch()

    def test_known_provider_rejection_and_pre_dispatch_deadline_are_not_unknown(self) -> None:
        inner, dispatcher = self.dispatcher(
            [MergeTrainGitHubError("head changed", status_code=409)]
        )
        with self.assertRaises(MergeTrainGitHubError):
            dispatcher.dispatch()
        self.assertEqual(self.state(), "not_dispatched")
        self.assertEqual(len(inner.requests), 1)

    def test_expired_dispatch_window_never_calls_provider(self) -> None:
        self.now = 46
        inner, dispatcher = self.dispatcher([])
        with self.assertRaises(OrdinaryAgentProviderDeferred):
            dispatcher.dispatch()
        self.assertEqual(inner.requests, [])
        self.assertEqual(self.state(), "not_dispatched")

    def test_generic_executor_cannot_reacquire_custody_to_land(self) -> None:
        executor = OrdinaryAgentMergeTrainEffectExecutor(
            record=self.finalized.effect,
            controller_fence=self.fixture.fence,
            effect_store=self.fixture.store,
            custody_store=self.fixture.store,
            secret_store=self.fixture.store,
        )
        with self.assertRaisesRegex(
            OrdinaryAgentEffectTerminal, "landing_requires_joined_finalization"
        ):
            command = self.finalized.effect.command
            assert command.kind == "pull_request_landing"
            executor.land_pull_request(command.effect)
        self.assertEqual(self.state(), "dispatching")

    def test_reversed_merge_parents_cannot_prove_the_approved_landing(self) -> None:
        changed = deepcopy(self.proof)
        changed["data"]["repository"]["ref"]["target"]["parents"]["nodes"].reverse()
        _, dispatcher = self.dispatcher([{"merged": True, "sha": self.result_sha}, changed])
        with self.assertRaises(OrdinaryAgentProviderEvidenceError):
            dispatcher.dispatch()
        self.assertEqual(self.state(), "reconciliation_required")

    def test_wrong_postmerge_tree_is_unknown_and_storage_rejects_fabricated_completion(
        self,
    ) -> None:
        p = self.finalized.preparation
        wrong = OrdinaryAgentCompletedOutcome(
            result_sha=self.result_sha,
            proof=OrdinaryAgentRefObservation(
                repository=p.target.repository,
                ref="refs/heads/" + p.target.base_branch,
                sha=self.result_sha,
                tree_sha="8" * 40,
                parents=(p.expected_base_sha, p.entry.expected_head_sha),
            ),
        )
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "landing_result_tree_mismatch"
        ):
            self.fixture.store.record_ordinary_semantic_outcome(
                child_id=self.finalized.child.child_id, typed_outcome=wrong
            )
        changed = deepcopy(self.proof)
        changed["data"]["repository"]["ref"]["target"]["tree"]["oid"] = "8" * 40
        inner, dispatcher = self.dispatcher([{"merged": True, "sha": self.result_sha}, changed])
        with self.assertRaises(OrdinaryAgentProviderEvidenceError):
            dispatcher.dispatch()
        self.assertEqual(self.state(), "reconciliation_required")
        self.assertEqual(len(inner.requests), 2)

    def reconcile(self, changes: dict[str, object] | None = None) -> OrdinaryAgentEffectRecord:
        store = self.fixture.store
        p = self.finalized.preparation
        child = self.finalized.child
        unknown = store.record_ordinary_semantic_outcome(
            child_id=child.child_id,
            typed_outcome=OrdinaryAgentUnknownOutcome(reason="transport_ambiguous"),
        )
        store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=child.custody_attempt_id, reason="confirmed_revoked"
        )
        session_fixture = self.fixture.fixture.fixture
        now = session_fixture.now + 16
        session_fixture.clock.return_value = datetime.fromtimestamp(now, timezone.utc).isoformat()
        permit = store.reserve_ordinary_reconciliation_custody_attempt(
            effect_id=unknown.effect_id, expected_effect_revision=unknown.revision
        )
        self.fixture.fixture.issue(permit)
        proof = OrdinaryAgentPullRequestObservation(
            repository=p.target.repository,
            number=p.entry.pull_request_number,
            head_sha=p.entry.expected_head_sha,
            base_ref=p.target.base_branch,
            base_sha=p.expected_base_sha,
            state="closed",
            merged=True,
            merge_commit_sha=self.result_sha,
            merge_commit_tree_sha=p.expected_merge_tree_sha,
            merge_commit_parents=(p.expected_base_sha, p.entry.expected_head_sha),
        )
        observation = OrdinaryAgentReconciliationObservation(
            observation_id="landing-recovery",
            custody_attempt_id=permit.attempt_id,
            observed_at=now,
            observation=proof.model_copy(update=changes or {}),
        )
        recovered = store.append_ordinary_effect_reconciliation(
            child_id=child.child_id,
            typed_observation=observation,
        )
        self.assertEqual(
            store.append_ordinary_effect_reconciliation(
                child_id=child.child_id,
                typed_observation=observation,
            ),
            recovered,
        )
        self.assertEqual(recovered.dispatch_count, 1)
        return recovered

    def test_exact_reconciliation_completes_without_another_dispatch(self) -> None:
        self.assertEqual(self.reconcile().state, "completed_observed")

    def test_wrong_tree_reconciliation_is_recorded_as_terminal_conflict(self) -> None:
        self.assertEqual(
            self.reconcile({"merge_commit_tree_sha": "8" * 40}).state, "terminal_conflict"
        )

    def test_wrong_parent_reconciliation_is_recorded_as_terminal_conflict(self) -> None:
        p = self.finalized.preparation
        self.assertEqual(
            self.reconcile(
                {"merge_commit_parents": (p.entry.expected_head_sha, p.expected_base_sha)}
            ).state,
            "terminal_conflict",
        )

    def test_command_drift_is_rejected_before_provider_io(self) -> None:
        command = self.finalized.effect.command
        assert command.kind == "pull_request_landing"
        changed = self.finalized.model_copy(
            update={
                "effect": self.finalized.effect.model_copy(
                    update={
                        "command": command.model_copy(
                            update={"effect": replace(command.effect, head_sha="f" * 40)}
                        )
                    }
                )
            }
        )
        inner, dispatcher = self.dispatcher([], changed)
        with self.assertRaisesRegex(
            OrdinaryLandingDispatchStopped, "landing_command_scope_mismatch"
        ):
            dispatcher.dispatch()
        self.assertEqual(inner.requests, [])
