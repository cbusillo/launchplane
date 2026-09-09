from __future__ import annotations

import unittest
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
from unittest.mock import patch

from control_plane.contracts import ordinary_agent_effect as effects
from control_plane.contracts.merge_train_effect import (
    CandidateHeadMergeEffect,
    CandidateRefDeleteEffect,
    CandidateRefPrepareEffect,
    MergeTrainEffectLineage,
    StackChildLabelEffect,
    StackChildCommentEffect,
)
from control_plane.contracts.ordinary_agent_custody import OrdinaryAgentCustodyCandidate
from control_plane.github_app_identity import GitHubAppInstallationToken
from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.ordinary_agent_custody import OrdinaryAgentProviderTokenLease
from control_plane.ordinary_agent_effect_lifecycle import require_completed_effect_proof
from control_plane.ordinary_agent_effect_lifecycle import classify_effect_reconciliation
from control_plane.ordinary_agent_reconciliation_reader import read_ordinary_effect_observation
from control_plane.ordinary_agent_github_transport import DeadlineMergeTrainGitHubTransport
from control_plane.ordinary_agent_merge_train_executor import (
    OrdinaryAgentMergeTrainEffectExecutor,
)
from tests.test_ordinary_agent_effect_lifecycle import effect_record


class OrdinaryAgentMergeTrainEffectExecutorTests(unittest.TestCase):
    def test_comment_dispatch_body_can_be_reconciled_after_response_loss(self) -> None:
        effect = StackChildCommentEffect(
            lineage=MergeTrainEffectLineage(
                repository="example/repo", base_branch="main", collapse_id="collapse-one"
            ),
            pull_request_number=7,
            body="Collapsed child",
        )
        record = effect_record(effects.StackChildCommentCommand(effect=effect))
        store = _dispatch_store(record)
        transport = RecordingMergeTrainGitHubTransport(responses=({"id": 81},))
        executor = OrdinaryAgentMergeTrainEffectExecutor(
            record=record,
            controller_fence=record.controller_fence,
            effect_store=store,
            custody_store=Mock(),
            secret_store=Mock(),
            transport_factory=lambda _: transport,
            monotonic=lambda: 0,
        )
        with patch(
            "control_plane.ordinary_agent_merge_train_executor.ordinary_agent_provider_token_lease",
            _provider_lease,
        ):
            executor.comment_stack_child(effect)
        sent = transport.requests[0].body
        assert sent is not None
        observed_transport = RecordingMergeTrainGitHubTransport(
            responses=([{"id": 81, "body": sent["body"]}],)
        )
        observed = read_ordinary_effect_observation(
            DeadlineMergeTrainGitHubTransport(
                transport=observed_transport,
                work_deadline=45,
                token_deadline=300,
                monotonic=lambda: 0,
            ),
            record,
        )
        self.assertEqual(classify_effect_reconciliation(record, observed), "completed_observed")
        self.assertEqual([request.method for request in transport.requests], ["POST"])
        self.assertEqual([request.method for request in observed_transport.requests], ["GET"])

    def test_candidate_prepare_uses_write_response_as_exact_proof(self) -> None:
        effect = CandidateRefPrepareEffect(
            lineage=MergeTrainEffectLineage(
                repository="example/repo", base_branch="main", batch_id="batch-one"
            ),
            candidate_ref="refs/heads/launchplane/train/jobs/request-one/1/batch-one",
            base_sha="a" * 40,
        )
        record = effect_record(effects.CandidateRefPrepareCommand(effect=effect))
        effect_store = _dispatch_store(record)
        transport = RecordingMergeTrainGitHubTransport(
            responses=({"ref": effect.candidate_ref, "object": {"sha": effect.base_sha}},)
        )
        executor = OrdinaryAgentMergeTrainEffectExecutor(
            record=record,
            controller_fence=record.controller_fence,
            effect_store=effect_store,
            custody_store=Mock(),
            secret_store=Mock(),
            transport_factory=lambda _: transport,
            monotonic=lambda: 0,
        )

        with patch(
            "control_plane.ordinary_agent_merge_train_executor.ordinary_agent_provider_token_lease",
            _provider_lease,
        ):
            executor.prepare_candidate_ref(effect)

        self.assertEqual(len(transport.requests), 1)
        proof = effect_store.record_ordinary_semantic_outcome.call_args.kwargs[
            "typed_outcome"
        ].proof
        self.assertEqual((proof.ref, proof.sha), (effect.candidate_ref, effect.base_sha))

    def test_candidate_noop_keeps_the_observed_sha_in_its_durable_completion(self) -> None:
        effect = CandidateHeadMergeEffect(
            lineage=MergeTrainEffectLineage(
                repository="example/repo", base_branch="main", batch_id="batch-one"
            ),
            candidate_ref="refs/heads/candidate-one",
            rolling_parent_sha="a" * 40,
            pull_request_number=7,
            head_sha="b" * 40,
        )
        record = effect_record(effects.CandidateHeadMergeCommand(effect=effect))
        store = _dispatch_store(record)
        store.record_ordinary_semantic_outcome.side_effect = lambda **kwargs: (
            require_completed_effect_proof(record, kwargs["typed_outcome"])
        )
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                None,
                {"object": {"sha": effect.rolling_parent_sha}},
                {
                    "sha": effect.rolling_parent_sha,
                    "tree": {"sha": "c" * 40},
                    "parents": [{"sha": effect.head_sha}],
                },
                {
                    "status": "ahead",
                    "base_commit": {"sha": effect.head_sha},
                    "merge_base_commit": {"sha": effect.head_sha},
                },
            )
        )
        executor = OrdinaryAgentMergeTrainEffectExecutor(
            record=record,
            controller_fence=record.controller_fence,
            effect_store=store,
            custody_store=Mock(),
            secret_store=Mock(),
            transport_factory=lambda _: transport,
            monotonic=lambda: 0,
        )
        with patch(
            "control_plane.ordinary_agent_merge_train_executor.ordinary_agent_provider_token_lease",
            _provider_lease,
        ):
            result = executor.merge_candidate_head(effect)
        self.assertIsNone(result.result_sha)
        self.assertEqual(result.result_tree_sha, "c" * 40)
        durable = store.record_ordinary_semantic_outcome.call_args.kwargs["typed_outcome"]
        self.assertEqual(durable.result_sha, effect.rolling_parent_sha)
        self.assertTrue(durable.no_op)
        self.assertEqual(
            transport.requests[-1].path,
            f"/repos/example/repo/compare/{effect.head_sha}...{effect.rolling_parent_sha}",
        )
        self.assertEqual(sum(request.method == "POST" for request in transport.requests), 1)

    def test_candidate_merge_persists_exact_response_proof_before_returning(self) -> None:
        effect = CandidateHeadMergeEffect(
            lineage=MergeTrainEffectLineage(
                repository="example/repo", base_branch="main", batch_id="batch-one"
            ),
            candidate_ref="refs/heads/launchplane/train/jobs/request-one/1/batch-one",
            rolling_parent_sha="a" * 40,
            pull_request_number=7,
            head_sha="b" * 40,
        )
        record = effect_record(effects.CandidateHeadMergeCommand(effect=effect))
        effect_store = Mock()
        effect_store.reserve_ordinary_custody_attempt.return_value = (
            effects.OrdinaryAgentCustodyAttemptReservation(
                effect_id=record.effect_id,
                effect_revision=record.revision + 1,
                purpose="dispatch",
                semantic_ordinal=1,
                custody_ordinal=1,
                attempt_id="custody_one",
                idempotency_key="effect-one-one",
                candidate=_custody_candidate(),
            )
        )
        effect_store.checkpoint_ordinary_semantic_dispatch.return_value = (
            effects.OrdinaryAgentSemanticDispatchAttemptRecord(
                child_id="child_one",
                effect_id=record.effect_id,
                command_sha256=record.command_sha256,
                controller_fence=record.controller_fence,
                semantic_ordinal=1,
                custody_attempt_id="custody_one",
                dispatch_checkpoint_at=1,
                fixed_token_expires_at=2_000_000_000,
            )
        )
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                {
                    "sha": "c" * 40,
                    "tree": {"sha": "d" * 40},
                    "parents": [{"sha": "a" * 40}, {"sha": "b" * 40}],
                },
            )
        )
        executor = OrdinaryAgentMergeTrainEffectExecutor(
            record=record,
            controller_fence=record.controller_fence,
            effect_store=effect_store,
            custody_store=Mock(),
            secret_store=Mock(),
            transport_factory=lambda _: transport,
            monotonic=lambda: 0,
        )

        with patch(
            "control_plane.ordinary_agent_merge_train_executor.ordinary_agent_provider_token_lease",
            _provider_lease,
        ):
            result = executor.merge_candidate_head(effect)

        self.assertEqual(
            (result.result_sha, result.result_tree_sha, result.parent_shas),
            ("c" * 40, "d" * 40, ("a" * 40, "b" * 40)),
        )
        outcome = effect_store.record_ordinary_semantic_outcome.call_args.kwargs["typed_outcome"]
        self.assertEqual(outcome.proof.tree_sha, "d" * 40)
        self.assertEqual(outcome.proof.parents, ("a" * 40, "b" * 40))

    def test_candidate_delete_is_retained_without_custody_or_provider_dispatch(self) -> None:
        effect = CandidateRefDeleteEffect(
            lineage=MergeTrainEffectLineage(
                repository="example/repo", base_branch="main", batch_id="batch-one"
            ),
            candidate_ref="refs/heads/launchplane/train/jobs/request-one/1/batch-one",
            expected_ref_sha="a" * 40,
        )
        record = effect_record(effects.CandidateRefDeleteCommand(effect=effect))
        effect_store = Mock()
        executor = OrdinaryAgentMergeTrainEffectExecutor(
            record=record,
            controller_fence=record.controller_fence,
            effect_store=effect_store,
            custody_store=Mock(),
            secret_store=Mock(),
            transport_factory=Mock(side_effect=AssertionError("provider must not run")),
        )

        self.assertFalse(executor.delete_candidate_ref(effect))

        effect_store.complete_ordinary_effect_without_dispatch.assert_called_once_with(
            effect_id=record.effect_id,
            expected_effect_revision=record.revision,
            disposition="candidate_ref_retained_no_conditional_delete",
        )
        effect_store.reserve_ordinary_custody_attempt.assert_not_called()

    def test_existing_label_completes_with_observation_before_dispatch_checkpoint(self) -> None:
        effect = StackChildLabelEffect(
            lineage=MergeTrainEffectLineage(
                repository="example/repo", base_branch="main", collapse_id="collapse-one"
            ),
            pull_request_number=7,
            label="collapsed",
        )
        record = effect_record(effects.StackChildLabelCommand(effect=effect))
        effect_store = _dispatch_store(record)
        effect_store.checkpoint_ordinary_semantic_dispatch.side_effect = AssertionError(
            "an existing label must not create a dispatch child"
        )
        transport = RecordingMergeTrainGitHubTransport(responses=([{"name": "collapsed"}],))
        executor = OrdinaryAgentMergeTrainEffectExecutor(
            record=record,
            controller_fence=record.controller_fence,
            effect_store=effect_store,
            custody_store=Mock(),
            secret_store=Mock(),
            transport_factory=lambda _: transport,
            monotonic=lambda: 0,
        )
        with patch(
            "control_plane.ordinary_agent_merge_train_executor.ordinary_agent_provider_token_lease",
            _provider_lease,
        ):
            executor.label_stack_child(effect)
        self.assertEqual([r.method for r in transport.requests], ["GET"])
        completed = effect_store.complete_ordinary_effect_without_dispatch.call_args.kwargs
        observation = completed["typed_observation"]
        self.assertEqual(observation.custody_attempt_id, "custody_one")
        self.assertEqual(
            observation.observation,
            effects.OrdinaryAgentLabelObservation(
                repository="example/repo", number=7, label="collapsed", present=True
            ),
        )
        effect_store.record_ordinary_semantic_outcome.assert_not_called()

    def test_absent_label_checkpoints_before_write_and_incomplete_read_does_not_write(self) -> None:
        payloads: tuple[list[dict[str, str]], ...] = ([], [{"name": "unrelated"}] * 100, [{}])
        for labels in payloads:
            with self.subTest(label_count=len(labels)):
                effect = StackChildLabelEffect(
                    lineage=MergeTrainEffectLineage(
                        repository="example/repo", base_branch="main", collapse_id="collapse-one"
                    ),
                    pull_request_number=7,
                    label="collapsed",
                )
                record = effect_record(effects.StackChildLabelCommand(effect=effect))
                effect_store = _dispatch_store(record)
                transport = RecordingMergeTrainGitHubTransport(responses=(labels, []))
                executor = OrdinaryAgentMergeTrainEffectExecutor(
                    record=record,
                    controller_fence=record.controller_fence,
                    effect_store=effect_store,
                    custody_store=Mock(),
                    secret_store=Mock(),
                    transport_factory=lambda _: transport,
                    monotonic=lambda: 0,
                )
                with patch(
                    "control_plane.ordinary_agent_merge_train_executor.ordinary_agent_provider_token_lease",
                    _provider_lease,
                ):
                    if labels:
                        with self.assertRaisesRegex(RuntimeError, "ordinary_label_observation"):
                            executor.label_stack_child(effect)
                        effect_store.checkpoint_ordinary_semantic_dispatch.assert_not_called()
                        self.assertEqual([r.method for r in transport.requests], ["GET"])
                    else:
                        executor.label_stack_child(effect)
                        effect_store.checkpoint_ordinary_semantic_dispatch.assert_called_once()
                        effect_store.record_ordinary_semantic_outcome.assert_called_once()
                        self.assertEqual([r.method for r in transport.requests], ["GET", "POST"])
                effect_store.complete_ordinary_effect_without_dispatch.assert_not_called()

    def test_executor_rejects_a_command_other_than_the_reserved_effect(self) -> None:
        effect = CandidateRefDeleteEffect(
            lineage=MergeTrainEffectLineage(
                repository="example/repo", base_branch="main", batch_id="batch-one"
            ),
            candidate_ref="refs/heads/launchplane/train/jobs/request-one/1/batch-one",
            expected_ref_sha="a" * 40,
        )
        record = effect_record(effects.CandidateRefDeleteCommand(effect=effect))
        effect_store = Mock()
        executor = OrdinaryAgentMergeTrainEffectExecutor(
            record=record,
            controller_fence=record.controller_fence,
            effect_store=effect_store,
            custody_store=Mock(),
            secret_store=Mock(),
        )

        with self.assertRaisesRegex(PermissionError, "ordinary_effect_command_mismatch"):
            executor.delete_candidate_ref(
                CandidateRefDeleteEffect(
                    lineage=effect.lineage,
                    candidate_ref=effect.candidate_ref,
                    expected_ref_sha="b" * 40,
                )
            )
        effect_store.complete_ordinary_effect_without_dispatch.assert_not_called()


def _custody_candidate() -> OrdinaryAgentCustodyCandidate:
    return OrdinaryAgentCustodyCandidate(
        principal_id="agent_one",
        repository_id=123,
        repository="example/repo",
        base_branch="main",
        credential_id="credential_one",
        credential_version=1,
        secret_id="secret_one",
        secret_binding_id="binding_one",
        secret_version_id="version_one",
        expected_app_id=42,
        effect_profile="guarded_merge",
    )


def _dispatch_store(record: effects.OrdinaryAgentEffectRecord) -> Mock:
    store = Mock()
    store.reserve_ordinary_custody_attempt.return_value = (
        effects.OrdinaryAgentCustodyAttemptReservation(
            effect_id=record.effect_id,
            effect_revision=record.revision + 1,
            purpose="dispatch",
            semantic_ordinal=1,
            custody_ordinal=1,
            attempt_id="custody_one",
            idempotency_key="effect-one-one",
            candidate=_custody_candidate(),
        )
    )
    store.checkpoint_ordinary_semantic_dispatch.return_value = (
        effects.OrdinaryAgentSemanticDispatchAttemptRecord(
            child_id="child_one",
            effect_id=record.effect_id,
            command_sha256=record.command_sha256,
            controller_fence=record.controller_fence,
            semantic_ordinal=1,
            custody_attempt_id="custody_one",
            dispatch_checkpoint_at=1,
            fixed_token_expires_at=2_000_000_000,
        )
    )
    return store


@contextmanager
def _provider_lease(**_: object) -> Iterator[OrdinaryAgentProviderTokenLease]:
    now = datetime.now(timezone.utc)
    yield OrdinaryAgentProviderTokenLease(
        installation_token=GitHubAppInstallationToken(
            token="provider-secret",
            app_id=42,
            installation_id=77,
            repository_id=123,
            repository="example/repo",
            expires_at=(now + timedelta(minutes=30)).isoformat(),
        ),
        attempt_id="custody_one",
    )


if __name__ == "__main__":
    unittest.main()
