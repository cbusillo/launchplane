from __future__ import annotations

import unittest

from control_plane.contracts import ordinary_agent_effect as effects
from control_plane.contracts.merge_train_effect import (
    CandidateHeadMergeEffect,
    MergeTrainEffectLineage,
    PullRequestHeadRefreshEffect,
)
from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget
from control_plane.ordinary_agent_effect_lifecycle import (
    classify_effect_reconciliation,
    require_completed_effect_proof,
)
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied


def effect_record(
    command: effects.OrdinaryAgentSemanticCommand,
) -> effects.OrdinaryAgentEffectRecord:
    return effects.OrdinaryAgentEffectRecord(
        effect_id="effect-one",
        request_id="request-one",
        session_id="session-one",
        lease_id="lease-one",
        principal_id="agent-one",
        scope_sha256="a" * 64,
        binding_revision=1,
        semantic_ordinal=1,
        action_ordinal=1,
        command_sha256="b" * 64,
        command=command,
        target=OrdinaryAgentTarget(
            repository_id=123, repository="example/repo", base_branch="main"
        ),
        controller_fence=effects.OrdinaryAgentControllerFence(
            controller_key="example/repo:main",
            lease_owner="worker",
            lease_acquired_at="2026-09-09T00:00:00Z",
        ),
        policy_record_id="policy-one",
        policy_revision=1,
        policy_sha256="c" * 64,
        credential_id="credential-one",
        credential_version=1,
        credential_digest="d" * 64,
        reserved_at=1,
        updated_at=1,
    )


class OrdinaryAgentEffectLifecycleTests(unittest.TestCase):
    def test_candidate_completion_requires_ordered_parents_or_actual_containment(self) -> None:
        command = effects.CandidateHeadMergeCommand(
            effect=CandidateHeadMergeEffect(
                lineage=MergeTrainEffectLineage(
                    repository="example/repo", base_branch="main", batch_id="batch-one"
                ),
                candidate_ref="refs/heads/launchplane/train/job-one",
                rolling_parent_sha="a" * 40,
                pull_request_number=12,
                head_sha="b" * 40,
            )
        )
        record = effect_record(command)
        proof = effects.OrdinaryAgentRefObservation(
            repository="example/repo",
            ref=command.effect.candidate_ref,
            sha="c" * 40,
            tree_sha="d" * 40,
            parents=("a" * 40, "b" * 40),
        )
        self.assertEqual(
            require_completed_effect_proof(
                record, effects.OrdinaryAgentCompletedOutcome(result_sha="c" * 40, proof=proof)
            ),
            "completed",
        )
        with self.assertRaises(OrdinaryAgentSessionAdmissionDenied):
            require_completed_effect_proof(
                record,
                effects.OrdinaryAgentCompletedOutcome(
                    result_sha="c" * 40,
                    proof=proof.model_copy(update={"parents": ("b" * 40, "a" * 40)}),
                ),
            )
        no_op = proof.model_copy(update={"sha": "a" * 40, "parents": ()})
        with self.assertRaises(OrdinaryAgentSessionAdmissionDenied):
            require_completed_effect_proof(
                record,
                effects.OrdinaryAgentCompletedOutcome(result_sha="a" * 40, no_op=True, proof=no_op),
            )
        self.assertEqual(
            require_completed_effect_proof(
                record,
                effects.OrdinaryAgentCompletedOutcome(
                    result_sha="a" * 40,
                    no_op=True,
                    proof=no_op.model_copy(update={"contained_head_sha": "b" * 40}),
                ),
            ),
            "completed",
        )

    def test_refresh_observation_requires_exact_base_and_preserves_ambiguous_unchanged_head(
        self,
    ) -> None:
        command = effects.PullRequestHeadRefreshCommand(
            effect=PullRequestHeadRefreshEffect(
                lineage=MergeTrainEffectLineage(repository="example/repo", base_branch="main"),
                pull_request_number=12,
                expected_head_sha="a" * 40,
                expected_base_sha="b" * 40,
            )
        )
        record = effect_record(command)
        observation = effects.OrdinaryAgentPullRequestObservation(
            repository="example/repo",
            number=12,
            head_sha="a" * 40,
            base_ref="main",
            base_sha="b" * 40,
            state="open",
            merged=False,
        )
        self.assertEqual(
            classify_effect_reconciliation(record, observation), "reconciliation_required"
        )
        updated = observation.model_copy(
            update={"head_sha": "c" * 40, "head_parents": ("a" * 40, "b" * 40)}
        )
        self.assertEqual(classify_effect_reconciliation(record, updated), "rebind_pending")
        self.assertEqual(
            classify_effect_reconciliation(
                record, updated.model_copy(update={"base_sha": "d" * 40})
            ),
            "reconciliation_required",
        )
