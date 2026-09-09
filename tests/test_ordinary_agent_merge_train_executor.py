from __future__ import annotations

import unittest
from unittest.mock import Mock

from control_plane.contracts import ordinary_agent_effect as effects
from control_plane.contracts.merge_train_effect import (
    CandidateRefDeleteEffect,
    MergeTrainEffectLineage,
)
from control_plane.ordinary_agent_merge_train_executor import (
    OrdinaryAgentMergeTrainEffectExecutor,
)
from tests.test_ordinary_agent_effect_lifecycle import effect_record


class OrdinaryAgentMergeTrainEffectExecutorTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
