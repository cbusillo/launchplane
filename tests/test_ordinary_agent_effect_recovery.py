"""Restart decisions never turn an uncertain provider response into a resend."""

import unittest

from control_plane.contracts import ordinary_agent_effect as effects
from control_plane.contracts.merge_train_effect import (
    CandidateHeadMergeEffect,
    MergeTrainEffectLineage,
)
from control_plane.ordinary_agent_effect_lifecycle import classify_effect_reconciliation
from control_plane.ordinary_agent_effect_recovery import recover_ordinary_effect
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from tests.test_ordinary_agent_effect_lifecycle import effect_record


class OrdinaryEffectRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.command = effects.CandidateHeadMergeCommand(
            effect=CandidateHeadMergeEffect(
                lineage=MergeTrainEffectLineage(
                    repository="example/repo", base_branch="main", batch_id="batch-one"
                ),
                candidate_ref="refs/heads/candidate-one",
                rolling_parent_sha="a" * 40,
                pull_request_number=12,
                head_sha="b" * 40,
            )
        )
        self.record = effect_record(self.command)
        self.child = effects.OrdinaryAgentSemanticDispatchAttemptRecord(
            child_id="dispatch-one",
            effect_id=self.record.effect_id,
            semantic_ordinal=1,
            custody_attempt_id="custody-one",
            dispatch_checkpoint_at=10,
            fixed_token_expires_at=100,
            controller_fence=self.record.controller_fence,
            command_sha256=self.record.command_sha256,
        )

    def test_dispatch_admission_association_is_coherent_and_legacy_optional(self) -> None:
        legacy_payload = self.child.model_dump(
            mode="json", exclude={"admission_id", "admission_binding_sha256"}
        )
        legacy = effects.OrdinaryAgentSemanticDispatchAttemptRecord.model_validate(legacy_payload)
        associated = effects.OrdinaryAgentSemanticDispatchAttemptRecord.model_validate(
            {
                **legacy_payload,
                "admission_id": "admission-one",
                "admission_binding_sha256": "f" * 64,
            }
        )

        self.assertIsNone(legacy.admission_id)
        self.assertEqual(legacy.model_dump(mode="json"), legacy_payload)
        self.assertEqual(associated.admission_id, "admission-one")
        for field, value in (
            ("admission_id", "admission-one"),
            ("admission_binding_sha256", "f" * 64),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "complete or absent"):
                    effects.OrdinaryAgentSemanticDispatchAttemptRecord.model_validate(
                        {**legacy_payload, field: value}
                    )

    def test_checkpoint_and_async_response_require_observation_not_fresh_dispatch(self) -> None:
        self.assertEqual(
            recover_ordinary_effect(
                effects.OrdinaryAgentEffectHistory(effect=self.record)
            ).disposition,
            "fresh",
        )
        for outcome in (
            None,
            effects.OrdinaryAgentAcceptedAsyncOutcome(),
            effects.OrdinaryAgentUnknownOutcome(reason="process_interrupted"),
        ):
            history = effects.OrdinaryAgentEffectHistory(
                effect=self.record.model_copy(update={"state": "dispatching", "dispatch_count": 1}),
                child=self.child,
                outcome=outcome,
            )
            self.assertEqual(recover_ordinary_effect(history).disposition, "observe")
        rejection = effects.OrdinaryAgentEffectHistory(
            effect=self.record.model_copy(update={"state": "not_dispatched", "dispatch_count": 1}),
            child=self.child,
            outcome=effects.OrdinaryAgentKnownNotDispatchedOutcome(reason="provider_rejected"),
        )
        self.assertEqual(recover_ordinary_effect(rejection).disposition, "terminal")
        contradictory = rejection.model_copy(
            update={
                "reconciliations": (
                    effects.OrdinaryAgentReconciliationObservation(
                        observation_id="later-observation",
                        custody_attempt_id="read-custody",
                        observed_at=20,
                        observation=effects.OrdinaryAgentRefObservation(
                            repository=self.record.target.repository,
                            ref=self.command.effect.candidate_ref,
                            sha=self.command.effect.rolling_parent_sha,
                            tree_sha="c" * 40,
                        ),
                    ),
                )
            }
        )
        self.assertEqual(recover_ordinary_effect(contradictory).disposition, "terminal")
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "effect_history_binding_conflict"
        ):
            recover_ordinary_effect(
                rejection.model_copy(
                    update={
                        "child": self.child.model_copy(update={"effect_id": "different-effect"})
                    }
                )
            )

    def test_proven_local_non_dispatch_can_retry_but_keeps_existing_attempt_cap(self) -> None:
        history = effects.OrdinaryAgentEffectHistory(
            effect=self.record.model_copy(update={"state": "not_dispatched", "dispatch_count": 1}),
            child=self.child,
            outcome=effects.OrdinaryAgentKnownNotDispatchedOutcome(
                reason="provider_attempt_deadline"
            ),
        )
        self.assertEqual(recover_ordinary_effect(history).disposition, "retry")
        exhausted = history.model_copy(
            update={
                "effect": history.effect.model_copy(
                    update={
                        "dispatch_count": effects.MAX_SEMANTIC_DISPATCH_ATTEMPTS_PER_EFFECT,
                    }
                )
            }
        )
        self.assertEqual(recover_ordinary_effect(exhausted).disposition, "terminal")
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "effect_history_binding_conflict"
        ):
            recover_ordinary_effect(history.model_copy(update={"child": None}))

    def test_lost_noop_response_requires_exact_containment_before_replay(self) -> None:
        proof = effects.OrdinaryAgentRefObservation(
            repository=self.record.target.repository,
            ref=self.command.effect.candidate_ref,
            sha=self.command.effect.rolling_parent_sha,
            tree_sha="c" * 40,
        )
        self.assertEqual(classify_effect_reconciliation(self.record, proof), "not_dispatched")
        proof = proof.model_copy(update={"contained_head_sha": self.command.effect.head_sha})
        self.assertEqual(classify_effect_reconciliation(self.record, proof), "completed_observed")
        history = effects.OrdinaryAgentEffectHistory(
            effect=self.record.model_copy(
                update={"state": "completed_observed", "dispatch_count": 1}
            ),
            child=self.child,
            outcome=effects.OrdinaryAgentUnknownOutcome(reason="transport_ambiguous"),
            reconciliations=(
                effects.OrdinaryAgentReconciliationObservation(
                    observation_id="observation-one",
                    custody_attempt_id="read-custody-one",
                    observed_at=20,
                    observation=proof,
                ),
            ),
        )
        recovered = recover_ordinary_effect(history)
        self.assertEqual(recovered.disposition, "replay")
        assert recovered.completed is not None
        self.assertTrue(recovered.completed.no_op)
        self.assertEqual(recovered.completed.result_sha, self.command.effect.rolling_parent_sha)
        self.assertEqual(recovered.completed.proof, proof)

        stale = history.model_copy(
            update={
                "reconciliations": (
                    history.reconciliations[0].model_copy(
                        update={"observed_at": self.child.dispatch_checkpoint_at - 1}
                    ),
                )
            }
        )
        self.assertEqual(recover_ordinary_effect(stale).disposition, "terminal")
