"""Atomic storage for proved ordinary landing no-ops."""

from datetime import datetime, timezone
from unittest.mock import patch
import hashlib
import unittest

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchLandingEntry,
    MergeTrainBatchLandingPlanRecord,
    build_merge_train_batch_landing_plan,
    build_merge_train_batch_landing_plan_record,
    build_ordinary_merge_train_candidate_ref,
)
from control_plane.contracts.merge_train_controller_state import MergeTrainControllerStateRecord
from control_plane.contracts.merge_admission_record import MergeAdmissionProposal
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.merge_train_effect import (
    CandidateHeadMergeEffect,
    MergeTrainEffectLineage,
)
from control_plane.contracts.ordinary_agent_effect import (
    CandidateHeadMergeCommand,
    OrdinaryAgentCompletedOutcome,
    OrdinaryAgentLandingPreparation,
    OrdinaryAgentRefObservation,
)
from control_plane.contracts.ordinary_agent_noop import OrdinaryAgentNoOpLandingFinalization
from control_plane.merge_admission import GuardedMergeAdmission, MergeAdmissionEvaluation
from control_plane.github_app_identity import ordinary_agent_effect_permissions
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from control_plane.storage.postgres import (
    LaunchplaneMergeAdmissionRow,
    LaunchplaneMergeLandingOutcomeRow,
    LaunchplaneMergeTrainBatchCandidateRow,
    LaunchplaneMergeTrainBatchLandingPlanRow,
    LaunchplaneMergeTrainControllerStateRow,
    LaunchplaneOrdinaryAgentEffectRow,
    LaunchplaneOrdinaryAgentFiniteRequestRow,
    LaunchplaneOrdinaryAgentLandingPreparationRow,
    LaunchplaneOrdinaryAgentNoOpLandingFinalizationRow,
)
from tests import test_ordinary_agent_landing_storage as landing_support
from tests import test_ordinary_agent_session_storage as session_support
from tests.test_merge_admission_records import _StaticEvaluator


class NoOpLandingStorageFixture:
    """Reusable fully proved no-op landing fixture for storage and route tests."""

    def __init__(
        self,
        test_case: unittest.TestCase,
        session_fixture: session_support.OrdinaryAgentSessionStorageTests,
    ) -> None:
        self.test_case = test_case
        self.session_fixture = session_fixture
        self.landing = landing_support.OrdinaryAgentLandingStorageTests()
        self.landing.prepare_landing_fixture(session_fixture)
        self.store = self.landing.store
        self._replace_candidate_with_no_op()
        self._record_no_op_candidate_history()

    def _replace_candidate_with_no_op(self) -> None:
        candidate = self.landing.candidate.candidate
        provenance = candidate.structural_provenance
        assert provenance is not None
        original_step = provenance.steps[0]
        no_op_step = original_step.model_copy(
            update={
                "result_sha": original_step.parent_sha,
                "result_tree_sha": original_step.parent_tree_sha,
                "kind": "no_op_already_contained",
            }
        )
        provenance_payload = provenance.model_dump(mode="python")
        provenance_payload.update(
            {
                "steps": (no_op_step,),
                "candidate_sha": no_op_step.parent_sha,
                "candidate_tree_sha": no_op_step.parent_tree_sha,
                "provenance_sha256": "",
                "candidate_sha256": "",
            }
        )
        no_op_provenance = type(provenance).model_validate(provenance_payload)
        candidate_payload = candidate.model_dump(mode="python")
        binding = self.landing.candidate.ordinary_job_binding
        assert binding is not None
        candidate_payload.update(
            {
                "candidate_ref": build_ordinary_merge_train_candidate_ref(
                    binding=binding, batch_id=candidate.batch_id
                ),
                "candidate_sha": no_op_step.parent_sha,
                "candidate_tree_sha": no_op_step.parent_tree_sha,
                "candidate_sha256": "",
                "structural_provenance": no_op_provenance,
            }
        )
        no_op_candidate = type(candidate).model_validate(candidate_payload)
        self.landing.candidate = self.landing.candidate.model_copy(
            update={"candidate": no_op_candidate}
        )
        no_op_plan = build_merge_train_batch_landing_plan(
            candidate=no_op_candidate,
            merge_method="merge",
            created_at=self.landing.plan.landing_plan.created_at,
        )
        self.landing.plan = self.landing.plan.model_copy(update={"landing_plan": no_op_plan})
        self.landing.structural = self.landing.structural.model_copy(
            update={
                "candidate_sha256": no_op_candidate.candidate_sha256,
                "landing_plan_sha256": no_op_plan.landing_plan_sha256,
                "provenance_sha256": no_op_provenance.provenance_sha256,
            }
        )
        with self.store._session_factory() as session:
            candidate_row = session.get(
                LaunchplaneMergeTrainBatchCandidateRow,
                self.landing.candidate.record_id,
            )
            plan_row = session.get(
                LaunchplaneMergeTrainBatchLandingPlanRow,
                self.landing.plan.record_id,
            )
            controller_row = session.get(
                LaunchplaneMergeTrainControllerStateRow,
                self.landing.fence.controller_key,
            )
            assert candidate_row is not None and plan_row is not None
            assert controller_row is not None
            candidate_row.payload = self.landing.candidate.model_dump(mode="json")
            plan_row.plan_id = no_op_plan.plan_id
            plan_row.payload = self.landing.plan.model_dump(mode="json")
            controller = MergeTrainControllerStateRecord.model_validate(controller_row.payload)
            controller_row.payload = controller.model_copy(
                update={"active_record_id": self.landing.candidate.record_id}
            ).model_dump(mode="json")
            session.commit()

    def _record_no_op_candidate_history(self) -> None:
        candidate = self.landing.candidate.candidate
        provenance = candidate.structural_provenance
        assert provenance is not None
        step = provenance.steps[0]
        command = CandidateHeadMergeCommand(
            effect=CandidateHeadMergeEffect(
                lineage=MergeTrainEffectLineage(
                    repository=candidate.repository,
                    base_branch=candidate.base_branch,
                    batch_id=candidate.batch_id,
                ),
                candidate_ref=candidate.candidate_ref,
                rolling_parent_sha=step.parent_sha,
                pull_request_number=step.pull_request_number,
                head_sha=step.head_sha,
            )
        )
        effect = self.store.reserve_ordinary_agent_effect(
            request_id=self.landing.request.request_id,
            expected_binding_revision=1,
            controller_fence=self.landing.fence,
            command=command,
            semantic_ordinal=1,
        )
        custody = self.store.reserve_ordinary_custody_attempt(
            effect_id=effect.effect_id, expected_effect_revision=effect.revision
        )
        self.landing.fixture.issue(custody)
        child = self.store.checkpoint_ordinary_semantic_dispatch(
            effect_id=effect.effect_id,
            controller_fence=self.landing.fence,
            custody_attempt_id=custody.attempt_id,
            fixed_token_expires_at=self.session_fixture.now + 300,
        )
        self.store.record_ordinary_semantic_outcome(
            child_id=child.child_id,
            typed_outcome=OrdinaryAgentCompletedOutcome(
                result_sha=step.parent_sha,
                no_op=True,
                proof=OrdinaryAgentRefObservation(
                    repository=candidate.repository,
                    ref=candidate.candidate_ref,
                    sha=step.parent_sha,
                    tree_sha=step.parent_tree_sha,
                    contained_head_sha=step.head_sha,
                ),
            ),
        )
        self.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=custody.attempt_id, reason="confirmed_revoked"
        )
        with self.store._session_factory() as session:
            controller_row = session.get(
                LaunchplaneMergeTrainControllerStateRow,
                self.landing.fence.controller_key,
            )
            assert controller_row is not None
            controller = MergeTrainControllerStateRecord.model_validate(controller_row.payload)
            controller_row.payload = controller.model_copy(
                update={
                    "active_record_id": self.landing.plan.record_id,
                    "active_pull_request_number": self.landing.pull_request,
                    "step_payload": {
                        "landing_plan_id": self.landing.plan.landing_plan.plan_id,
                        "expected_effect_sha": self.landing.plan.landing_plan.candidate_sha,
                    },
                }
            ).model_dump(mode="json")
            session.commit()

    def _guard(self, preparation: OrdinaryAgentLandingPreparation) -> GuardedMergeAdmission:
        guard = self.landing.guard(preparation)
        assert isinstance(guard.evaluator, _StaticEvaluator)
        evaluation = guard.evaluator.evaluation
        readiness = evaluation.readiness
        candidate_evidence = readiness.candidate.model_copy(
            update={
                "evidence": readiness.candidate.evidence.model_copy(
                    update={
                        "record_id": self.landing.candidate.record_id,
                        "candidate_sha": self.landing.candidate.candidate.candidate_sha,
                    }
                )
            }
        )
        readiness_payload = readiness.model_dump(mode="python")
        readiness_payload.update(
            {
                "target": readiness.target.model_copy(
                    update={"expected_effect_sha": self.landing.candidate.candidate.candidate_sha}
                ),
                "candidate": candidate_evidence,
                "readiness_digest": "",
            }
        )
        corrected_readiness = type(readiness).model_validate(readiness_payload)
        guard.evaluator = _StaticEvaluator(
            MergeAdmissionEvaluation(
                corrected_readiness,
                self.landing.structural,
            )
        )
        return guard

    def observed(self) -> tuple[OrdinaryAgentLandingPreparation, MergeAdmissionProposal]:
        preparation = self.landing.reserve().preparation
        now = self.session_fixture.now
        self.store.acquire_ordinary_agent_custody_issue_attempt(
            attempt_id=preparation.custody_attempt_id,
            idempotency_key_sha256=hashlib.sha256(preparation.idempotency_key.encode()).hexdigest(),
            request_sha256=canonical_json_sha256(
                {
                    "candidate": preparation.candidate.model_dump(mode="json"),
                    "request": preparation.request_payload,
                }
            ),
            candidate=preparation.candidate,
            requested_permissions=ordinary_agent_effect_permissions(
                preparation.candidate.effect_profile
            ),
            dispatch_window_seconds=30,
        )
        self.store.mark_ordinary_agent_custody_issued(
            attempt_id=preparation.custody_attempt_id,
            app_id=preparation.candidate.expected_app_id,
            installation_id=77,
            token_expires_at=datetime.fromtimestamp(now + 300, timezone.utc).isoformat(),
            residual_expires_at=datetime.fromtimestamp(now + 360, timezone.utc).isoformat(),
        )
        preparation = self.landing.reserve().preparation
        evidence = self.landing.evidence(preparation)
        observed = self.store.record_ordinary_landing_evidence(
            preparation_id=preparation.preparation_id,
            expected_revision=preparation.revision,
            controller_fence=self.landing.fence,
            evidence=evidence,
        )
        proposal = self._guard(observed).build_proposal(
            entry=observed.entry,
            observed_base_sha=observed.expected_base_sha,
            observed_base_tree_sha=observed.expected_base_tree_sha,
            observed_head_sha=observed.entry.expected_head_sha,
            observed_head_tree_sha=observed.entry.expected_head_tree_sha,
        )
        return observed, proposal

    def successor(
        self, preparation: OrdinaryAgentLandingPreparation
    ) -> MergeTrainBatchLandingPlanRecord:
        skipped = MergeTrainBatchLandingEntry.model_validate(
            {
                **preparation.entry.model_dump(mode="json"),
                "status": "skipped",
                "recorded_rolling_base_sha": preparation.expected_base_sha,
                "recorded_rolling_base_tree_sha": preparation.expected_base_tree_sha,
                "landed_head_sha": preparation.entry.expected_head_sha,
                "landed_head_tree_sha": preparation.entry.expected_head_tree_sha,
                "merge_commit_sha": preparation.expected_base_sha,
                "merge_commit_tree_sha": preparation.expected_base_tree_sha,
            }
        )
        plan = self.landing.plan.landing_plan.model_copy(update={"entries": (skipped,)})
        return build_merge_train_batch_landing_plan_record(
            landing_plan=plan,
            source="test:no-op-successor",
            updated_at=datetime.fromtimestamp(
                self.session_fixture.now + 1, timezone.utc
            ).isoformat(),
            ordinary_job_binding=self.landing.plan.ordinary_job_binding,
        )

    def finalize(
        self,
        preparation: OrdinaryAgentLandingPreparation,
        proposal: MergeAdmissionProposal,
        successor: MergeTrainBatchLandingPlanRecord,
    ) -> OrdinaryAgentNoOpLandingFinalization:
        return self.store.finalize_ordinary_no_op_landing_preparation(
            preparation_id=preparation.preparation_id,
            expected_revision=preparation.revision,
            controller_fence=self.landing.fence,
            proposal=proposal,
            custody_attempt_id=preparation.custody_attempt_id,
            successor=successor,
        )


class OrdinaryAgentNoOpStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        session_fixture = session_support.OrdinaryAgentSessionStorageTests()
        session_fixture.setUp()
        self.addCleanup(session_fixture.doCleanups)
        self.fixture = NoOpLandingStorageFixture(self, session_fixture)

    def test_joined_finalization_replay_and_later_successor_are_exact(self) -> None:
        preparation, proposal = self.fixture.observed()
        successor = self.fixture.successor(preparation)
        with self.fixture.store._session_factory() as session:
            request_row = session.get(
                LaunchplaneOrdinaryAgentFiniteRequestRow,
                self.fixture.landing.request.request_id,
            )
            assert request_row is not None
            before_ids = tuple(request_row.payload["execution_record_ids"])
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "landing_dispatch_no_op_forbidden"
        ):
            self.fixture.landing.finalize(preparation, proposal)

        created = self.fixture.finalize(preparation, proposal, successor)
        self.assertEqual(created.disposition, "created")
        self.assertEqual(created.preparation.state, "consumed")
        self.assertIsNone(created.preparation.effect_id)
        self.assertEqual(created.outcome.reason, "already_contained_no_provider_effect")
        self.assertFalse(created.outcome.provider_effect_attempted)
        replay = self.fixture.finalize(preparation, proposal, successor)
        self.assertEqual(replay.disposition, "replay")
        self.assertEqual(replay.successor, successor)

        with self.fixture.store._session_factory() as session:
            predecessor = session.get(
                LaunchplaneMergeTrainBatchLandingPlanRow,
                self.fixture.landing.plan.record_id,
            )
            controller_row = session.get(
                LaunchplaneMergeTrainControllerStateRow,
                self.fixture.landing.fence.controller_key,
            )
            request_row = session.get(
                LaunchplaneOrdinaryAgentFiniteRequestRow,
                self.fixture.landing.request.request_id,
            )
            assert predecessor is not None and controller_row is not None
            assert request_row is not None
            controller = MergeTrainControllerStateRecord.model_validate(controller_row.payload)
            self.assertEqual(predecessor.status, "superseded")
            self.assertEqual(controller.active_record_id, successor.record_id)
            self.assertEqual(request_row.payload["execution_record_ids"], list(before_ids))
            self.assertEqual(
                session.scalar(select(func.count()).select_from(LaunchplaneOrdinaryAgentEffectRow)),
                1,
            )

        later = build_merge_train_batch_landing_plan_record(
            landing_plan=successor.landing_plan,
            source="test:later-successor",
            updated_at=datetime.fromtimestamp(
                self.fixture.session_fixture.now + 2, timezone.utc
            ).isoformat(),
            ordinary_job_binding=successor.ordinary_job_binding,
        )
        self.fixture.store.write_ordinary_merge_train_record(
            request_id=self.fixture.landing.request.request_id,
            expected_binding_revision=1,
            controller_fence=self.fixture.landing.fence,
            record=later,
            expected_predecessor_record_id=successor.record_id,
        )
        historical = self.fixture.store.read_ordinary_no_op_landing_finalization(
            preparation_id=preparation.preparation_id
        )
        assert historical is not None
        self.assertEqual(historical.successor, successor)

    def test_unarmed_skipped_write_and_late_failure_leave_no_partial_state(self) -> None:
        preparation, proposal = self.fixture.observed()
        successor = self.fixture.successor(preparation)
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "landing_no_op_unproven"):
            self.fixture.store.write_ordinary_merge_train_record(
                request_id=self.fixture.landing.request.request_id,
                expected_binding_revision=1,
                controller_fence=self.fixture.landing.fence,
                record=successor,
                expected_predecessor_record_id=self.fixture.landing.plan.record_id,
            )

        def fail_after_flush(session: Session) -> None:
            session.flush()
            raise ValueError("injected no-op commit failure")

        with patch.object(Session, "commit", fail_after_flush), self.assertRaises(ValueError):
            self.fixture.finalize(preparation, proposal, successor)
        with self.fixture.store._session_factory() as session:
            preparation_row = session.get(
                LaunchplaneOrdinaryAgentLandingPreparationRow,
                preparation.preparation_id,
            )
            predecessor = session.get(
                LaunchplaneMergeTrainBatchLandingPlanRow,
                self.fixture.landing.plan.record_id,
            )
            assert preparation_row is not None and predecessor is not None
            self.assertEqual(preparation_row.payload["state"], "observed")
            self.assertEqual(predecessor.status, "active")
            self.assertIsNone(
                session.get(LaunchplaneMergeAdmissionRow, proposal.record.admission_id)
            )
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(
                        LaunchplaneOrdinaryAgentNoOpLandingFinalizationRow
                    )
                ),
                0,
            )
            self.assertEqual(
                session.scalar(select(func.count()).select_from(LaunchplaneMergeLandingOutcomeRow)),
                0,
            )

    def test_changed_replay_is_rejected(self) -> None:
        preparation, proposal = self.fixture.observed()
        successor = self.fixture.successor(preparation)
        self.fixture.finalize(preparation, proposal, successor)
        changed = successor.model_copy(update={"source": "test:changed"})
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "no_op_finalization_replay_conflict"
        ):
            self.fixture.finalize(preparation, proposal, changed)


if __name__ == "__main__":
    unittest.main()
