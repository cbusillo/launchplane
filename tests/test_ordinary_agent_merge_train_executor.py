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
    MergeTrainEffectLineage,
)
from control_plane.contracts.ordinary_agent_custody import OrdinaryAgentCustodyCandidate
from control_plane.github_app_identity import GitHubAppInstallationToken
from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.ordinary_agent_custody import OrdinaryAgentProviderTokenLease
from control_plane.ordinary_agent_merge_train_executor import (
    OrdinaryAgentMergeTrainEffectExecutor,
)
from tests.test_ordinary_agent_effect_lifecycle import effect_record


class OrdinaryAgentMergeTrainEffectExecutorTests(unittest.TestCase):
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
        outcome = effect_store.record_ordinary_semantic_outcome.call_args.kwargs[
            "typed_outcome"
        ]
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
