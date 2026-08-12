from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest

from control_plane.contracts.merge_admission_record import MergeLandingOutcomeRecord
from control_plane.contracts.merge_readiness import (
    MergeReadinessCandidateEvidence,
    MergeReadinessResult,
)
from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchEntry,
    MergeTrainBatchLandingPlanRecord,
    build_merge_train_batch_candidate_ref,
    build_merge_train_batch_id,
    build_merge_train_batch_landing_plan,
)
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerStateRecord,
    build_merge_train_controller_key,
)
from control_plane.contracts.merge_train_structural_provenance import (
    MergeTrainRollingStep,
    MergeTrainStructuralCandidateResult,
    MergeTrainStructuralEntryBinding,
    MergeTrainStructuralProvenance,
)
from control_plane.merge_admission import (
    GuardedMergeAdmission,
    MergeAdmissionDeniedError,
    MergeAdmissionEvaluation,
    MergeAdmissionEvaluator,
    MergeAdmissionReconciliationRequiredError,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.test_merge_readiness import (
    BASE_SHA,
    CANDIDATE_SHA,
    HEAD_SHA,
    OTHER_SHA,
    POLICY_SHA,
    REPOSITORY,
    TREE_SHA,
    _candidate,
    _evaluate,
    _fence,
    _merge_admission,
    _policy_fingerprints,
    _target,
)


def _ambiguous_outcome(
    *,
    observation_sequence: int = 1,
    prior_outcome_id: str = "",
    observed_at: str = "",
) -> MergeLandingOutcomeRecord:
    admission = _merge_admission()
    return MergeLandingOutcomeRecord(
        admission_id=admission.admission_id,
        admission_binding_sha256=admission.admission_binding_sha256,
        attempt_id=admission.attempt_id,
        observation_sequence=observation_sequence,
        prior_outcome_id=prior_outcome_id,
        source="test:ambiguous",
        repository=admission.repository,
        base_branch=admission.base_branch,
        pull_request_number=admission.pull_request_number,
        status="reconcile_required",
        reason="provider_transport_ambiguous",
        provider_effect_attempted=True,
        provider_message="connection reset after request send",
        observed_at=observed_at or f"2026-08-11T03:0{observation_sequence + 1}:00Z",
    )


def _landed_outcome(
    *,
    observation_sequence: int,
    prior_outcome_id: str,
) -> MergeLandingOutcomeRecord:
    admission = _merge_admission()
    return MergeLandingOutcomeRecord(
        admission_id=admission.admission_id,
        admission_binding_sha256=admission.admission_binding_sha256,
        attempt_id=admission.attempt_id,
        observation_sequence=observation_sequence,
        prior_outcome_id=prior_outcome_id,
        source="test:reconciled-landed",
        repository=admission.repository,
        base_branch=admission.base_branch,
        pull_request_number=admission.pull_request_number,
        status="landed",
        reason="provider_and_git_confirmed",
        provider_effect_attempted=True,
        observed_pull_request_head_sha=admission.pull_request_head_sha,
        observed_pull_request_head_tree_sha=admission.pull_request_head_tree_sha,
        observed_base_sha="7" * 40,
        observed_base_tree_sha="8" * 40,
        merge_commit_sha="9" * 40,
        merge_commit_tree_sha="a" * 40,
        base_contains_merge_commit=True,
        exact_landing_confirmed=True,
        observed_at="2026-08-11T03:04:00Z",
    )


def _guard_records() -> tuple[
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchLandingPlanRecord,
    MergeTrainControllerStateRecord,
    MergeTrainStructuralCandidateResult,
]:
    entry = MergeTrainBatchEntry(
        pull_request_number=2083,
        position=2,
        head_sha=HEAD_SHA,
        head_tree_sha=TREE_SHA,
        impact_status="known",
    )
    provenance = MergeTrainStructuralProvenance(
        repository=REPOSITORY,
        base_branch="main",
        base_sha=BASE_SHA,
        base_tree_sha=OTHER_SHA,
        policy_key=f"{REPOSITORY}:main",
        policy_sha256=POLICY_SHA,
        entries=(
            MergeTrainStructuralEntryBinding(
                position=1,
                pull_request_number=entry.pull_request_number,
                head_sha=entry.head_sha,
                head_tree_sha=entry.head_tree_sha,
                impact_status="known",
            ),
        ),
        steps=(
            MergeTrainRollingStep(
                position=1,
                pull_request_number=entry.pull_request_number,
                parent_sha=BASE_SHA,
                parent_tree_sha=OTHER_SHA,
                head_sha=entry.head_sha,
                head_tree_sha=entry.head_tree_sha,
                result_sha=CANDIDATE_SHA,
                result_tree_sha="6" * 40,
                kind="merge_commit",
            ),
        ),
        candidate_sha=CANDIDATE_SHA,
        candidate_tree_sha="6" * 40,
    )
    normalized_entry = entry.model_copy(update={"position": 1})
    batch_id = build_merge_train_batch_id(
        repository=REPOSITORY,
        base_branch="main",
        base_sha=BASE_SHA,
        entry_head_shas=(HEAD_SHA,),
    )
    candidate = MergeTrainBatchCandidate(
        batch_id=batch_id,
        repository=REPOSITORY,
        base_branch="main",
        base_sha=BASE_SHA,
        policy_key=f"{REPOSITORY}:main",
        policy_sha256=POLICY_SHA,
        candidate_ref=build_merge_train_batch_candidate_ref(
            repository=REPOSITORY,
            base_branch="main",
            batch_id=batch_id,
        ),
        candidate_sha=CANDIDATE_SHA,
        candidate_tree_sha="6" * 40,
        status="passed",
        entries=(normalized_entry,),
        structural_provenance=provenance,
        required_checks_status="pass",
        created_at="2026-08-11T03:00:00Z",
        updated_at="2026-08-11T03:00:00Z",
    )
    candidate_record = MergeTrainBatchCandidateRecord(
        record_id="candidate-record-1",
        source="test",
        updated_at="2026-08-11T03:00:00Z",
        candidate=candidate,
    )
    landing_plan = build_merge_train_batch_landing_plan(
        candidate=candidate,
        merge_method="merge",
        created_at="2026-08-11T03:00:30Z",
    )
    landing_record = MergeTrainBatchLandingPlanRecord(
        record_id="landing-record-1",
        source="test",
        updated_at="2026-08-11T03:00:30Z",
        landing_plan=landing_plan,
    )
    controller_state = MergeTrainControllerStateRecord(
        controller_key=build_merge_train_controller_key(
            repository=REPOSITORY,
            base_branch="main",
        ),
        repository=REPOSITORY,
        base_branch="main",
        policy_key=f"{REPOSITORY}:main",
        policy_sha256=POLICY_SHA,
        status="running",
        updated_at="2026-08-11T03:00:00Z",
        lease_owner="controller-run-1",
        lease_acquired_at="2026-08-11T03:00:00Z",
        lease_expires_at="2026-08-11T03:10:00.000000Z",
        heartbeat_at="2026-08-11T03:00:00Z",
        active_action="land_batch",
        active_phase="merge_batch_entries",
        active_pull_request_number=2083,
        step_payload={
            "landing_plan_id": landing_plan.plan_id,
            "expected_effect_sha": landing_plan.candidate_sha,
        },
    )
    structural_result = MergeTrainStructuralCandidateResult(
        status="exact",
        reason_codes=("structural_single_entry_exact",),
        effective_base_sha=BASE_SHA,
        effective_base_tree_sha=OTHER_SHA,
        candidate_sha256=candidate.candidate_sha256,
        landing_plan_sha256=landing_plan.landing_plan_sha256,
        provenance_sha256=provenance.provenance_sha256,
    )
    return candidate_record, landing_record, controller_state, structural_result


class _StaticEvaluator:
    def __init__(self, evaluation: MergeAdmissionEvaluation) -> None:
        self.evaluation = evaluation

    def evaluate(self, **_: object) -> MergeAdmissionEvaluation:
        return self.evaluation


class _ProviderError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"provider rejected with {status_code}")
        self.status_code = status_code


class MergeAdmissionStoreContract:
    store: FilesystemRecordStore | PostgresRecordStore

    def test_admission_create_is_append_only_and_idempotent(self) -> None:
        test_case = cast(unittest.TestCase, self)
        admission = _merge_admission()

        stored, created = self.store.create_merge_admission_record_if_absent(admission)
        replayed, replay_created = self.store.create_merge_admission_record_if_absent(admission)

        test_case.assertTrue(created)
        test_case.assertFalse(replay_created)
        test_case.assertEqual(stored, admission)
        test_case.assertEqual(replayed, admission)
        test_case.assertEqual(
            self.store.read_merge_admission_record(admission.admission_id),
            admission,
        )
        test_case.assertEqual(
            self.store.list_unresolved_merge_admission_records(),
            (admission,),
        )

        conflicting = admission.model_copy(
            update={
                "admission_id": "",
                "admission_binding_sha256": "",
                "source": "test:conflict",
            }
        )
        conflicting = type(admission).model_validate(conflicting.model_dump(mode="json"))
        with test_case.assertRaisesRegex(ValueError, "append-only"):
            self.store.create_merge_admission_record_if_absent(conflicting)

    def test_ambiguous_outcome_reconciles_append_only_to_landed(self) -> None:
        test_case = cast(unittest.TestCase, self)
        admission = _merge_admission()
        self.store.create_merge_admission_record_if_absent(admission)
        ambiguous = _ambiguous_outcome(observed_at="2026-08-11T03:09:00Z")

        stored, created = self.store.create_merge_landing_outcome_record_if_absent(ambiguous)
        replayed, replay_created = self.store.create_merge_landing_outcome_record_if_absent(
            ambiguous
        )

        test_case.assertTrue(created)
        test_case.assertFalse(replay_created)
        test_case.assertEqual(stored, replayed)
        test_case.assertEqual(
            self.store.list_unresolved_merge_admission_records(),
            (admission,),
        )

        landed = _landed_outcome(
            observation_sequence=2,
            prior_outcome_id=ambiguous.outcome_id,
        )
        self.store.create_merge_landing_outcome_record_if_absent(landed)

        test_case.assertEqual(self.store.list_unresolved_merge_admission_records(), ())
        test_case.assertEqual(
            tuple(
                outcome.status
                for outcome in reversed(
                    self.store.list_merge_landing_outcome_records(
                        admission_id=admission.admission_id
                    )
                )
            ),
            ("reconcile_required", "landed"),
        )


class FilesystemMergeAdmissionStoreTests(MergeAdmissionStoreContract, unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.store = FilesystemRecordStore(Path(self.temporary_directory.name))


class PostgresMergeAdmissionStoreTests(MergeAdmissionStoreContract, unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        database_path = Path(self.temporary_directory.name) / "launchplane.sqlite3"
        self.store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{database_path}")
        self.store.ensure_schema()
        self.addCleanup(self.store.close)

    def test_filesystem_import_uses_outcome_sequence_not_observation_time(self) -> None:
        filesystem_store = FilesystemRecordStore(
            Path(self.temporary_directory.name) / "filesystem-state"
        )
        admission = _merge_admission()
        filesystem_store.create_merge_admission_record_if_absent(admission)
        ambiguous = _ambiguous_outcome(observed_at="2026-08-11T03:09:00Z")
        filesystem_store.create_merge_landing_outcome_record_if_absent(ambiguous)
        filesystem_store.create_merge_landing_outcome_record_if_absent(
            _landed_outcome(
                observation_sequence=2,
                prior_outcome_id=ambiguous.outcome_id,
            )
        )

        counts = cast(PostgresRecordStore, self.store).import_core_records_from_filesystem(
            filesystem_store
        )

        self.assertEqual(counts["merge_landing_outcomes"], 2)
        outcomes = self.store.list_merge_landing_outcome_records(
            admission_id=admission.admission_id
        )
        self.assertEqual(tuple(outcome.observation_sequence for outcome in outcomes), (2, 1))


class GuardedMergeAdmissionScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.store = FilesystemRecordStore(Path(self.temporary_directory.name))
        (
            self.candidate_record,
            self.landing_record,
            self.controller_state,
            self.structural_result,
        ) = _guard_records()
        self.store.write_merge_train_controller_state_record(self.controller_state)

    def _readiness(self, **updates: object):  # type: ignore[no-untyped-def]
        payload: dict[str, object] = {
            "target": _target(queue_position=1),
            "candidate_evidence": MergeReadinessCandidateEvidence.model_validate(
                {
                    **_candidate().model_dump(mode="json"),
                    "queue_position": 1,
                    "record_id": self.candidate_record.record_id,
                }
            ),
        }
        payload.update(updates)
        return _evaluate(**payload)

    def _guard(
        self,
        readiness: MergeReadinessResult | None = None,
        *,
        evaluator: MergeAdmissionEvaluator | None = None,
        admission_time_provider: Callable[[], str] | None = None,
    ) -> GuardedMergeAdmission:
        return GuardedMergeAdmission(
            record_store=self.store,
            evaluator=evaluator
            or _StaticEvaluator(
                MergeAdmissionEvaluation(
                    readiness=readiness or self._readiness(),
                    structural_result=self.structural_result,
                )
            ),
            candidate_record=self.candidate_record,
            landing_plan_record=self.landing_record,
            controller_state=self.controller_state,
            controller_state_provider=lambda: self.store.list_merge_train_controller_state_records(
                repository=REPOSITORY,
                base_branch="main",
                limit=1,
            )[0],
            admission_time_provider=admission_time_provider or (lambda: "2026-08-11T03:01:00Z"),
            trace_id="scenario-test",
        )

    def _admit(self, guard: GuardedMergeAdmission):  # type: ignore[no-untyped-def]
        entry = self.landing_record.landing_plan.entries[0]
        return guard.admit(
            entry=entry,
            observed_base_sha=BASE_SHA,
            observed_base_tree_sha=OTHER_SHA,
            observed_head_sha=HEAD_SHA,
            observed_head_tree_sha=TREE_SHA,
        )

    def test_scenario_2_persists_exact_admission_before_effect(self) -> None:
        admission = self._admit(self._guard())

        self.assertEqual(
            self.store.read_merge_admission_record(admission.admission_id),
            admission,
        )
        self.assertEqual(admission.readiness.state, "ready")

    def test_scenario_11_missing_outcome_blocks_provider_retry(self) -> None:
        guard = self._guard()
        self._admit(guard)

        with self.assertRaises(MergeAdmissionReconciliationRequiredError):
            self._admit(guard)

    def test_scenario_11_open_exact_pr_reconciliation_allows_fresh_attempt(self) -> None:
        guard = self._guard()
        first = self._admit(guard)
        entry = self.landing_record.landing_plan.entries[0]

        reconciled = guard.reconcile_existing_no_effect(
            entry=entry,
            observed_base_sha=BASE_SHA,
            observed_base_tree_sha=OTHER_SHA,
            observed_head_sha=HEAD_SHA,
            observed_head_tree_sha=TREE_SHA,
            observed_pull_request_state="open",
            observed_at="2026-08-11T03:02:00Z",
        )
        second = self._admit(guard)

        self.assertIsNotNone(reconciled)
        assert reconciled is not None
        self.assertEqual(reconciled.status, "rejected")
        self.assertEqual(reconciled.reason, "reconciliation_confirmed_no_effect")
        self.assertFalse(reconciled.provider_conclusive_rejection)
        self.assertIsNone(reconciled.provider_status_code)
        outcomes = self.store.list_merge_landing_outcome_records(admission_id=first.admission_id)
        self.assertEqual(tuple(outcome.observation_sequence for outcome in outcomes), (2, 1))
        self.assertEqual(outcomes[1].status, "reconcile_required")
        self.assertNotEqual(first.admission_id, second.admission_id)

    def test_scenario_12_policy_drift_refuses_next_admission(self) -> None:
        readiness = self._readiness(policy_fingerprints=_policy_fingerprints(drift="merge_train"))

        with self.assertRaises(MergeAdmissionDeniedError):
            self._admit(self._guard(readiness))

        self.assertEqual(self.store.list_merge_admission_records(), ())

    def test_scenario_22_lost_lease_refuses_admission(self) -> None:
        readiness = self._readiness(
            fence_evidence=_fence(
                observed_lease_owner="another-controller",
            )
        )

        with self.assertRaises(MergeAdmissionDeniedError):
            self._admit(self._guard(readiness))

        self.assertEqual(self.store.list_merge_admission_records(), ())

    def test_lease_stolen_after_evaluation_creates_no_admission(self) -> None:
        evaluation = MergeAdmissionEvaluation(
            readiness=self._readiness(),
            structural_result=self.structural_result,
        )
        test_case = self

        class LeaseStealingEvaluator:
            def evaluate(self, **_: object) -> MergeAdmissionEvaluation:
                stolen = test_case.controller_state.model_copy(
                    update={
                        "lease_owner": "controller-run-2",
                        "lease_acquired_at": "2026-08-11T03:00:30Z",
                    }
                )
                test_case.store.write_merge_train_controller_state_record(stolen)
                return evaluation

        with self.assertRaises(MergeAdmissionDeniedError):
            self._admit(self._guard(evaluator=LeaseStealingEvaluator()))

        self.assertEqual(self.store.list_merge_admission_records(), ())

    def test_lease_expiry_is_rechecked_at_actual_admission_time(self) -> None:
        times = iter(("2026-08-11T03:09:59Z", "2026-08-11T03:10:00Z"))

        with self.assertRaises(MergeAdmissionDeniedError):
            self._admit(self._guard(admission_time_provider=lambda: next(times)))

        self.assertEqual(self.store.list_merge_admission_records(), ())

    def test_progress_record_uses_stable_landing_plan_lineage(self) -> None:
        guard = self._guard()
        first = self._admit(guard)
        guard.record_provider_failure(
            admission=first,
            error=_ProviderError(405),
            observed_at="2026-08-11T03:02:00Z",
        )
        progress_record = self.landing_record.model_copy(
            update={"record_id": "landing-record-progress-2"}
        )
        guard.update_landing_plan_record(progress_record)

        second = self._admit(guard)

        self.assertEqual(second.attempt_sequence, 2)
        self.assertEqual(second.landing_plan_id, first.landing_plan_id)
        self.assertEqual(second.landing_plan_record_id, progress_record.record_id)
        self.assertNotEqual(second.attempt_id, first.attempt_id)

    def test_scenario_23_rejection_requires_fresh_admission_for_retry(self) -> None:
        guard = self._guard()
        first = self._admit(guard)
        rejected = guard.record_provider_failure(
            admission=first,
            error=_ProviderError(405),
            observed_at="2026-08-11T03:02:00Z",
        )

        second = self._admit(guard)

        self.assertEqual(rejected.status, "rejected")
        self.assertNotEqual(first.admission_id, second.admission_id)
        self.assertEqual(second.attempt_sequence, 2)


if __name__ == "__main__":
    unittest.main()
