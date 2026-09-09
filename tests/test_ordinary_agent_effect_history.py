"""Restart history preserves outcomes without provider or session authority."""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from control_plane.contracts.merge_train_effect import (
    CandidateRefDeleteEffect,
    MergeTrainEffectLineage,
    StackChildLabelEffect,
)
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapseEntry,
    MergeTrainStackCollapseMutation,
    MergeTrainStackCollapsePlan,
    MergeTrainStackCollapsePlanRecord,
)
from control_plane.contracts.ordinary_agent_effect import (
    CandidateRefDeleteCommand,
    OrdinaryAgentCompletedOutcome,
    OrdinaryAgentPullRequestObservation,
    StackChildLabelCommand,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import OrdinaryAgentJobBinding
from control_plane.github_app_identity import GitHubAppInstallationToken
from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.ordinary_agent_merge_train_executor import OrdinaryAgentMergeTrainEffectExecutor
from control_plane.ordinary_agent_effect_recovery import recover_ordinary_effect
from tests import test_ordinary_agent_effect_storage as effect_support
from tests import test_ordinary_agent_landing_dispatch as dispatch_support
from tests import test_ordinary_agent_landing_storage as landing_support
from tests import test_ordinary_agent_session_storage as session_support


class OrdinaryAgentEffectHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = dispatch_support.LandingDispatchTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.fixture.store
        self.effect_id = self.fixture.finalized.effect.effect_id

    def test_completed_history_survives_session_cancellation_without_provider_reads(self) -> None:
        pending = self.store.read_ordinary_agent_effect_history(effect_id=self.effect_id)
        self.assertEqual(pending.effect.state, "dispatching")
        self.assertIsNone(pending.outcome)
        self.assertEqual(recover_ordinary_effect(pending).disposition, "observe")
        inner, dispatcher = self.fixture.dispatcher(
            [{"merged": True, "sha": self.fixture.result_sha}, self.fixture.proof]
        )
        dispatcher.dispatch()
        complete = self.store.read_ordinary_agent_effect_history(effect_id=self.effect_id)
        assert isinstance(complete.outcome, OrdinaryAgentCompletedOutcome)
        self.assertEqual(complete.outcome.result_sha, self.fixture.result_sha)
        recovered = recover_ordinary_effect(complete)
        self.assertEqual(recovered.disposition, "replay")
        self.assertEqual(recovered.completed, complete.outcome)
        session_fixture = self.fixture.fixture.fixture.fixture
        self.store.cancel_ordinary_agent_session(
            proof=session_fixture.proof,
            session_id=self.fixture.finalized.preparation.session_id,
        )
        self.assertEqual(
            self.store.read_ordinary_agent_effect_history(effect_id=self.effect_id), complete
        )
        self.assertEqual(
            recover_ordinary_effect(
                self.store.read_ordinary_agent_effect_history(effect_id=self.effect_id)
            ),
            recovered,
        )
        self.assertEqual(len(inner.requests), 2)
        self.assertEqual(
            self.store.read_ordinary_landing_preparation(
                preparation_id=self.fixture.finalized.preparation.preparation_id
            ).state,
            "consumed",
        )

    def test_reconciled_history_retains_unknown_response_and_exact_observation(self) -> None:
        self.fixture.reconcile()
        history = self.store.read_ordinary_agent_effect_history(effect_id=self.effect_id)
        self.assertEqual(history.effect.state, "completed_observed")
        assert history.outcome is not None
        self.assertEqual(history.outcome.kind, "unknown")
        self.assertEqual(len(history.reconciliations), 1)
        observation = history.reconciliations[0].observation
        assert isinstance(observation, OrdinaryAgentPullRequestObservation)
        self.assertEqual(observation.merge_commit_sha, self.fixture.result_sha)
        recovered = recover_ordinary_effect(history)
        self.assertEqual(recovered.disposition, "replay")
        assert recovered.completed is not None
        self.assertEqual(recovered.completed.result_sha, self.fixture.result_sha)
        assert history.child is not None
        self.assertEqual(history.child.semantic_ordinal, history.effect.dispatch_count)

    def test_conflicting_reconciliation_is_history_without_success_or_replay_authority(
        self,
    ) -> None:
        self.fixture.reconcile({"merge_commit_tree_sha": "8" * 40})
        history = self.store.read_ordinary_agent_effect_history(effect_id=self.effect_id)
        self.assertEqual(history.effect.state, "terminal_conflict")
        self.assertEqual(history.effect.reason_code, "landing_result_tree_mismatch")
        self.assertEqual(recover_ordinary_effect(history).disposition, "terminal")
        assert history.outcome is not None
        self.assertEqual(history.outcome.kind, "unknown")
        self.assertEqual(len(history.reconciliations), 1)
        self.assertEqual(history.effect.dispatch_count, 1)


class OrdinaryAgentUndispatchedHistoryTests(unittest.TestCase):
    def test_retained_ref_completion_has_history_without_a_dispatch_child(self) -> None:
        fixture = landing_support.OrdinaryAgentLandingStorageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        plan = fixture.plan.landing_plan
        effect = fixture.store.reserve_ordinary_agent_effect(
            request_id=fixture.request.request_id,
            expected_binding_revision=1,
            controller_fence=fixture.fence,
            command=CandidateRefDeleteCommand(
                effect=CandidateRefDeleteEffect(
                    lineage=MergeTrainEffectLineage(
                        repository=plan.repository,
                        base_branch=plan.base_branch,
                        batch_id=plan.batch_id,
                        landing_plan_id=plan.plan_id,
                    ),
                    candidate_ref=plan.candidate_ref,
                    expected_ref_sha=plan.candidate_sha,
                )
            ),
            semantic_ordinal=1,
        )
        fixture.store.complete_ordinary_effect_without_dispatch(
            effect_id=effect.effect_id,
            expected_effect_revision=effect.revision,
            disposition="candidate_ref_retained_no_conditional_delete",
        )
        history = fixture.store.read_ordinary_agent_effect_history(effect_id=effect.effect_id)
        self.assertEqual(history.effect.state, "retained_no_conditional_delete")
        self.assertIsNone(history.child)
        self.assertIsNone(history.outcome)
        assert history.undispatched_completion is not None
        self.assertEqual(
            history.undispatched_completion.disposition,
            "candidate_ref_retained_no_conditional_delete",
        )
        self.assertIsNone(history.undispatched_completion.observation)
        recovered = recover_ordinary_effect(history)
        self.assertEqual(recovered.disposition, "retained")
        self.assertIsNone(recovered.completed)

    def test_existing_label_round_trips_real_custody_completion_without_dispatch(self) -> None:
        session_fixture = session_support.OrdinaryAgentSessionStorageTests()
        session_fixture.setUp(pull_request_limit=2)
        self.addCleanup(session_fixture.doCleanups)
        fixture = effect_support.OrdinaryAgentEffectStorageTests()
        fixture.prepare_effect_fixture(session_fixture, stack_child_number=13)
        fence, _ = fixture.prepare_controller()
        store = fixture.store
        timestamp = datetime.fromtimestamp(session_fixture.now, timezone.utc).isoformat()
        request = fixture.request
        plan = MergeTrainStackCollapsePlan(
            collapse_id="collapse-label-test",
            repository=request.target.repository,
            base_branch=request.target.base_branch,
            root_pull_request_number=12,
            root_initial_head_sha="b" * 40,
            root_head_ref="root",
            policy_key=fixture.merge_policy.policy.policies[0].policy_key,
            policy_sha256=fixture.merge_policy.policy_sha256,
            entries=(
                MergeTrainStackCollapseEntry(
                    pull_request_number=12,
                    position=1,
                    head_sha="b" * 40,
                    head_ref="root",
                    base_ref="main",
                ),
                MergeTrainStackCollapseEntry(
                    pull_request_number=13,
                    position=2,
                    head_sha="c" * 40,
                    head_ref="child",
                    base_ref="root",
                ),
            ),
            mutations=(
                MergeTrainStackCollapseMutation(
                    child_pull_request_number=13,
                    parent_pull_request_number=12,
                    child_head_sha="c" * 40,
                    expected_parent_head_sha="b" * 40,
                    parent_head_ref="root",
                ),
            ),
            created_at=timestamp,
            updated_at=timestamp,
        )
        store.write_ordinary_merge_train_record(
            request_id=request.request_id,
            expected_binding_revision=1,
            controller_fence=fence,
            record=MergeTrainStackCollapsePlanRecord(
                record_id="collapse-label-record",
                source="test",
                updated_at=timestamp,
                plan=plan,
                ordinary_job_binding=OrdinaryAgentJobBinding(
                    request_id=request.request_id,
                    binding_revision=1,
                    scope_sha256=request.scope_sha256,
                ),
            ),
        )
        command = StackChildLabelCommand(
            effect=StackChildLabelEffect(
                lineage=MergeTrainEffectLineage(
                    repository=request.target.repository,
                    base_branch=request.target.base_branch,
                    collapse_id=plan.collapse_id,
                ),
                pull_request_number=13,
                label="collapsed",
            )
        )
        record = store.reserve_ordinary_agent_effect(
            request_id=request.request_id,
            expected_binding_revision=1,
            controller_fence=fence,
            command=command,
            semantic_ordinal=1,
        )
        transport = RecordingMergeTrainGitHubTransport(responses=([{"name": "collapsed"}],))
        token = GitHubAppInstallationToken(
            token="test-token",
            app_id=42,
            installation_id=77,
            repository_id=request.target.repository_id,
            repository=request.target.repository,
            expires_at=datetime.fromtimestamp(session_fixture.now + 300, timezone.utc).isoformat(),
        )
        executor = OrdinaryAgentMergeTrainEffectExecutor(
            record=record,
            controller_fence=fence,
            effect_store=store,
            custody_store=store,
            secret_store=store,
            transport_factory=lambda _: transport,
            api_request=lambda **kwargs: None,
            monotonic=lambda: 0,
            utc_now=lambda: datetime.fromtimestamp(session_fixture.now, timezone.utc),
        )
        with (
            patch(
                "control_plane.ordinary_agent_custody.resolve_ordinary_agent_github_app_identity",
                return_value=SimpleNamespace(identity=SimpleNamespace(app_id=42)),
            ),
            patch(
                "control_plane.ordinary_agent_custody.mint_ordinary_agent_installation_token",
                return_value=token,
            ),
        ):
            executor.label_stack_child(command.effect)
        history = store.read_ordinary_agent_effect_history(effect_id=record.effect_id)
        self.assertEqual(recover_ordinary_effect(history).disposition, "replay")
        self.assertEqual(history.effect.state, "completed_observed")
        self.assertIsNone(history.child)
        self.assertIsNone(history.outcome)
        completion = history.undispatched_completion
        assert completion is not None and completion.observation is not None
        self.assertEqual(completion.disposition, "label_already_present")
        custody = store.read_ordinary_agent_custody_issue_attempt(
            completion.observation.custody_attempt_id
        )
        self.assertEqual(custody.state, "closed")
        self.assertIsNotNone(custody.token_expires_at)
        self.assertEqual([item.method for item in transport.requests], ["GET"])
