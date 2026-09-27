import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from control_plane.contracts.merge_admission_record import MergeBatchNoEffectEvidence
from control_plane.contracts.merge_train_batch import build_merge_train_batch_landing_plan
from control_plane.merge_admission import (
    GuardedMergeAdmission,
    MergeAdmissionReconciliationRequiredError,
)
from control_plane.merge_train_github import GitHubMergeTrainClient
from tests.test_merge_train_batch_pull_request import _BatchProvider
from control_plane.storage.filesystem import FilesystemRecordStore
from tests import test_merge_admission_rolling_checks as rolling_fixtures


class ProtectedBatchGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        # Reuse the complete Git/provenance fixture exercised by the live evaluator.
        self.fixture = rolling_fixtures.RollingCandidateCheckTests()
        self.fixture.setUp()
        self.fixture.plan = build_merge_train_batch_landing_plan(
            candidate=self.fixture.candidate_record.candidate,
            merge_method="merge",
            created_at=self.fixture.candidate_record.updated_at,
            candidate_pull_request_number=9000,
        )
        self.fixture.landing_record = self.fixture.landing_record.model_copy(
            update={"landing_plan": self.fixture.plan}
        )
        self.fixture.controller = self.fixture.controller.model_copy(
            update={
                "active_record_id": self.fixture.landing_record.record_id,
                "step_payload": {
                    "landing_plan_id": self.fixture.plan.plan_id,
                    "expected_effect_sha": self.fixture.plan.candidate_sha,
                },
            }
        )
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = FilesystemRecordStore(Path(temporary.name))
        self.store.write_merge_train_controller_state_record(self.fixture.controller)
        self.store.write_merge_train_batch_candidate_record(self.fixture.candidate_record)
        self.store.write_merge_train_batch_landing_plan_record(self.fixture.landing_record)

        fixture = self.fixture

        class LiveEvaluator:
            @staticmethod
            def evaluate(**kwargs: Any) -> Any:
                return fixture._evaluate(
                    after_first_landing=False,
                    target_position=kwargs["entry"].position,
                    base_tree=fixture.plan.entries[0].recorded_candidate_parent_tree_sha,
                )

        self.guard = GuardedMergeAdmission(
            record_store=self.store,
            evaluator=LiveEvaluator(),
            candidate_record=fixture.candidate_record,
            landing_plan_record=fixture.landing_record,
            controller_state=fixture.controller,
            controller_state_provider=lambda: self.fixture.controller,
            admission_time_provider=lambda: "2026-08-11T03:01:00Z",
            trace_id="batch-test",
        )

    def admit(self, position: int) -> Any:
        entry = self.fixture.plan.entries[position - 1]
        self.fixture.controller = self.fixture.controller.model_copy(
            update={"active_pull_request_number": entry.pull_request_number}
        )
        self.store.write_merge_train_controller_state_record(self.fixture.controller)
        return self.guard.admit(
            entry=entry,
            observed_base_sha=self.fixture.plan.entries[0].expected_base_sha,
            observed_base_tree_sha=self.fixture.plan.entries[0].recorded_candidate_parent_tree_sha,
            observed_head_sha=entry.expected_head_sha,
            observed_head_tree_sha=entry.expected_head_tree_sha,
        )

    def test_shared_merge_interruption_reconciles_both_real_stored_outcomes(self) -> None:
        provider = _BatchProvider(self.fixture.candidate_record.candidate)
        provider.number = 9000
        provider.interrupt_after_merge = True
        client = GitHubMergeTrainClient(transport=provider)
        client.ensure_batch_pull_request(candidate=provider.candidate)

        def checkpoint(_plan: Any, entry: Any, phase: str) -> None:
            if phase == "merge_entry":
                self.fixture.controller = self.fixture.controller.model_copy(
                    update={"active_pull_request_number": entry.pull_request_number}
                )
                self.store.write_merge_train_controller_state_record(self.fixture.controller)

        def land() -> Any:
            return client.land_batch_candidate(
                landing_plan=self.guard.landing_plan_record.landing_plan,
                admission_guard=self.guard,
                recorded_at="2026-08-11T03:02:00Z",
                checkpoint=checkpoint,
            )

        with self.assertRaises(KeyboardInterrupt):
            land()
        admissions = self.store.list_merge_admission_records()
        self.assertEqual(len(admissions), 2)
        self.assertEqual(self.store.list_merge_landing_outcome_records(), ())
        landed = land()
        self.assertTrue(all(entry.status == "merged" for entry in landed.entries))
        self.assertEqual(len(provider.merge_calls), 1)
        outcomes = self.store.list_merge_landing_outcome_records()
        self.assertEqual(
            {outcome.admission_id for outcome in outcomes}, {a.admission_id for a in admissions}
        )
        self.assertTrue(all(outcome.status == "landed" for outcome in outcomes))
        self.assertEqual({outcome.merge_commit_sha for outcome in outcomes}, {provider.merge_sha})

    def test_both_real_admissions_persist_before_one_shared_no_effect_reconciliation(self) -> None:
        admissions = [self.admit(1), self.admit(2)]
        self.assertEqual(len(self.store.list_unresolved_merge_admission_records()), 2)
        self.assertEqual(
            {admission.landing_plan_sha256 for admission in admissions},
            {self.fixture.plan.landing_plan_sha256},
        )
        self.guard.reconcile_batch_no_effect(
            evidence=MergeBatchNoEffectEvidence(
                pull_request_number=9000,
                head_sha=self.fixture.plan.candidate_sha,
                state="open",
                merged=False,
                base_contains_head=False,
            ),
            observed_base_sha="d" * 40,
            observed_base_tree_sha="e" * 40,
            observed_at="2026-08-11T03:02:00Z",
        )
        self.assertEqual(self.store.list_unresolved_merge_admission_records(), ())
        for admission in admissions:
            outcomes = self.store.list_merge_landing_outcome_records(
                admission_id=admission.admission_id
            )
            self.assertEqual(
                [outcome.reason for outcome in outcomes],
                ["batch_reconciliation_confirmed_no_effect", "process_interrupted"],
            )
            self.assertEqual(outcomes[0].observed_pull_request_head_sha, "")
            proof = outcomes[0].batch_no_effect
            assert proof is not None
            self.assertEqual(proof.pull_request_number, 9000)

    def test_admitted_prefix_can_record_that_dispatch_never_happened(self) -> None:
        admission = self.admit(1)
        outcome = self.guard.record_not_dispatched(
            admission=admission, observed_at="2026-08-11T03:02:00Z"
        )
        self.assertEqual(outcome.reason, "batch_not_dispatched")
        self.assertFalse(outcome.provider_effect_attempted)
        self.assertEqual(self.store.list_unresolved_merge_admission_records(), ())

    def test_different_batch_pr_cannot_resolve_an_interrupted_attempt(self) -> None:
        self.admit(1)
        with self.assertRaises(MergeAdmissionReconciliationRequiredError):
            self.guard.reconcile_batch_no_effect(
                evidence=MergeBatchNoEffectEvidence(
                    pull_request_number=9001,
                    head_sha=self.fixture.plan.candidate_sha,
                    state="open",
                    merged=False,
                    base_contains_head=False,
                ),
                observed_base_sha="d" * 40,
                observed_base_tree_sha="e" * 40,
                observed_at="2026-08-11T03:02:00Z",
            )
        self.assertEqual(len(self.store.list_unresolved_merge_admission_records()), 1)
