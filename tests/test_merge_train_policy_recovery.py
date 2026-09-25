from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.merge_readiness import MergeReadinessCandidateEvidence
from control_plane.merge_admission import GuardedMergeAdmission, MergeAdmissionEvaluation
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.merge_train_admission import build_merge_train_controller_status_read_model
from control_plane.merge_train_controller_run_once import (
    MergeTrainControllerRunOnceEnvelope,
    MergeTrainControllerRunOnceResult,
    execute_merge_train_controller_with_client,
)
from control_plane.merge_train_github import GitHubMergeTrainClient, MergeTrainGitHubError
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.test_merge_admission_live import _queued_pull_request
from tests.test_merge_admission_records import _StaticEvaluator, _guard_records
from tests.test_merge_train_github import _landing_plan
from tests.test_merge_readiness import (
    BASE_SHA,
    HEAD_SHA,
    OTHER_SHA,
    REPOSITORY,
    TREE_SHA,
    _candidate,
    _evaluate,
    _target,
)


class _RecoveryTransport:
    def __init__(self) -> None:
        self.base_sha = BASE_SHA
        self.head_sha = HEAD_SHA
        self.pr_state = "open"
        self.unavailable = False
        self.calls: list[str] = []

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        if method != "GET" or body is not None:
            raise AssertionError("Policy recovery must not mutate GitHub")
        self.calls.append(path)
        if self.unavailable:
            raise MergeTrainGitHubError("Provider read unavailable", status_code=503)
        if path.endswith("/branches/main"):
            return {"commit": {"sha": self.base_sha, "commit": {"tree": {"sha": OTHER_SHA}}}}
        if path.endswith(f"/git/commits/{HEAD_SHA}"):
            return {"sha": HEAD_SHA, "tree": {"sha": TREE_SHA}, "parents": []}
        if path.endswith("/pulls/2083"):
            return {
                "state": self.pr_state,
                "head": {"sha": self.head_sha},
                "base": {"ref": "main", "sha": self.base_sha},
            }
        raise AssertionError(f"Unexpected recovery read: {path}")


class _NoAdmissionEvaluator:
    def evaluate(self, **_: object) -> MergeAdmissionEvaluation:
        raise AssertionError("Retirement must not admit a merge under the old policy")


class MergeTrainPolicyRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = FilesystemRecordStore(state_dir=Path(temporary.name))
        self.candidate, self.landing, controller, structural = _guard_records()
        self.store.write_merge_train_batch_candidate_record(self.candidate)
        self.store.write_merge_train_batch_landing_plan_record(self.landing)
        self.store.write_merge_train_controller_state_record(controller)
        readiness = _evaluate(
            target=_target(queue_position=1),
            candidate_evidence=MergeReadinessCandidateEvidence.model_validate(
                {
                    **_candidate().model_dump(),
                    "queue_position": 1,
                    "record_id": self.candidate.record_id,
                }
            ),
        )
        self.guard = GuardedMergeAdmission(
            record_store=self.store,
            evaluator=_StaticEvaluator(
                MergeAdmissionEvaluation(readiness=readiness, structural_result=structural)
            ),
            candidate_record=self.candidate,
            landing_plan_record=self.landing,
            controller_state=controller,
            trace_id="original-attempt",
            admission_time_provider=lambda: "2026-08-11T03:01:00Z",
        )
        self.admission = self.guard.admit(
            entry=self.landing.landing_plan.entries[0],
            observed_base_sha=BASE_SHA,
            observed_base_tree_sha=OTHER_SHA,
            observed_head_sha=HEAD_SHA,
            observed_head_tree_sha=TREE_SHA,
        )
        self.store.write_merge_train_controller_state_record(
            controller.model_copy(
                update={
                    "status": "reconcile_required",
                    "lease_owner": "",
                    "lease_acquired_at": "",
                    "lease_expires_at": "",
                    "heartbeat_at": "",
                    "active_phase": "merge_pull_request",
                    "active_record_id": self.landing.record_id,
                    "reconciliation_status": "required",
                    "reconciliation_detail": "operator_required:github_request_rejected",
                }
            )
        )
        self.policy = build_test_merge_train_policy_record(repository=REPOSITORY)
        self.transport = _RecoveryTransport()
        self.client = GitHubMergeTrainClient(transport=self.transport)
        self.attempt = 0

    def _run(self, *, mutate: bool = True) -> MergeTrainControllerRunOnceResult:
        self.attempt += 1
        return execute_merge_train_controller_with_client(
            request=MergeTrainControllerRunOnceEnvelope(repository=REPOSITORY, mutate=mutate),
            policy=self.policy.policy,
            policy_sha256=self.policy.policy_sha256,
            repository_policy=self.policy.policy.policies[0],
            github_client=self.client,
            trace_id=f"policy-recovery-{self.attempt}",
            recorded_at="2026-08-11T03:03:00Z",
            candidate_store=self.store,
            landing_store=self.store,
            stack_collapse_store=self.store,
            controller_state_store=self.store,
            admission_store=self.store,
            admission_evaluator=_NoAdmissionEvaluator(),
        )

    def _record_failure(self, status: int) -> None:
        self.guard.record_provider_failure(
            admission=self.admission,
            error=MergeTrainGitHubError("Provider refused merge", status_code=status),
            observed_at="2026-08-11T03:02:00Z",
        )

    def test_rejected_old_policy_plan_retires_and_requires_a_fresh_candidate(self) -> None:
        self._record_failure(405)
        old_outcome = self.store.list_merge_landing_outcome_records()[0]
        result = self._run()

        self.assertEqual(result.accepted_result["controller_action"], "retire_stale_landing")
        self.assertEqual(self.store.list_merge_landing_outcome_records(), (old_outcome,))
        self.assertEqual(self.store.list_merge_admission_records(), (self.admission,))
        self.assertEqual(self.store.list_merge_train_controller_state_records()[0].status, "idle")
        self.assertEqual(
            self.store.list_merge_train_batch_candidate_records()[0].status, "superseded"
        )
        self.assertIn(self.landing, self.store.list_merge_train_batch_landing_plan_records())

        snapshot = MergeTrainDryRunSnapshot(
            repository=REPOSITORY,
            base_branch="main",
            base_sha=BASE_SHA,
            pull_requests=(
                _queued_pull_request(
                    number=2083, head_sha=HEAD_SHA, created_at="2026-08-11T01:00:00Z"
                ),
            ),
        )
        with patch.object(self.client, "read_merge_train_snapshot", return_value=snapshot):
            fresh = self._run()
            next_action = self._run(mutate=False)
        self.assertEqual(fresh.accepted_result["controller_action"], "plan_candidate")
        self.assertEqual(next_action.accepted_result["controller_action"], "build_candidate")
        active = self.store.list_merge_train_batch_candidate_records(status="active")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].candidate.policy_sha256, self.policy.policy_sha256)
        self.assertEqual(active[0].candidate.candidate_sha, "")

    def test_ambiguous_attempt_gets_append_only_no_effect_evidence(self) -> None:
        self._record_failure(500)
        self._run()
        outcomes = self.store.list_merge_landing_outcome_records()
        self.assertEqual(
            [outcome.status for outcome in outcomes], ["rejected", "reconcile_required"]
        )
        self.assertEqual(outcomes[0].reason, "reconciliation_confirmed_no_effect")
        self.assertEqual(outcomes[0].prior_outcome_id, outcomes[1].outcome_id)

    def test_confirmed_rejection_allows_unrelated_base_movement(self) -> None:
        self._record_failure(405)
        original_outcome = self.store.list_merge_landing_outcome_records()[0]
        self.transport.base_sha = "9" * 40
        result = self._run()
        self.assertEqual(result.accepted_result["controller_action"], "retire_stale_landing")
        self.assertEqual(self.store.list_merge_train_controller_state_records()[0].status, "idle")
        self.assertEqual(self.store.list_merge_landing_outcome_records(), (original_outcome,))

    def test_old_stale_record_cannot_suppress_same_sha_candidate_under_new_policy(self) -> None:
        self._record_failure(405)
        self._run()
        fresh_candidate, _, _, _ = _guard_records(policy_sha256=self.policy.policy_sha256)
        fresh_candidate = fresh_candidate.model_copy(update={"record_id": "new-policy-candidate"})
        self.store.write_merge_train_batch_candidate_record(fresh_candidate)
        snapshot = MergeTrainDryRunSnapshot(
            repository=REPOSITORY,
            base_branch="main",
            base_sha=BASE_SHA,
            pull_requests=(
                _queued_pull_request(
                    number=2083, head_sha=HEAD_SHA, created_at="2026-08-11T01:00:00Z"
                ),
            ),
        )
        with patch.object(self.client, "read_merge_train_snapshot", return_value=snapshot):
            result = self._run(mutate=False)
        self.assertEqual(result.accepted_result["controller_action"], "plan_landing")

    def test_dry_run_inspects_an_idle_stale_plan_without_writing_records(self) -> None:
        self._record_failure(500)
        controller = self.store.list_merge_train_controller_state_records()[0]
        controller = controller.model_copy(
            update={
                "status": "idle",
                "active_action": "",
                "active_phase": "",
                "active_record_id": "",
                "active_pull_request_number": None,
                "step_payload": {},
                "reconciliation_status": "clean",
                "reconciliation_detail": "",
            }
        )
        self.store.write_merge_train_controller_state_record(controller)
        before = self.store.list_merge_landing_outcome_records()
        result = self._run(mutate=False)
        self.assertEqual(result.accepted_result["controller_action"], "retire_stale_landing")
        self.assertEqual(self.store.list_merge_landing_outcome_records(), before)
        self.assertEqual(self.store.list_merge_train_controller_state_records(), (controller,))
        self.assertEqual(self.store.list_merge_train_batch_landing_plan_records(), (self.landing,))

    def test_partial_landing_cannot_be_declared_unlanded(self) -> None:
        plan = _landing_plan()
        partial = plan.model_copy(
            update={
                "entries": (
                    plan.entries[0].model_copy(update={"status": "merged"}),
                    plan.entries[1],
                )
            }
        )
        with self.assertRaisesRegex(MergeTrainGitHubError, "no completed entries"):
            self.client.verify_unlanded_batch(landing_plan=partial)
        self.assertEqual(self.transport.calls, [])

    def test_changed_or_unreadable_provider_state_preserves_the_recovery_fence(self) -> None:
        self._record_failure(500)
        checkpoint = self.store.list_merge_train_controller_state_records()[0]
        for field, value in (
            ("base_sha", "9" * 40),
            ("head_sha", "9" * 40),
            ("pr_state", "closed"),
            ("unavailable", True),
        ):
            with self.subTest(field=field):
                previous = getattr(self.transport, field)
                setattr(self.transport, field, value)
                with self.assertRaises(MergeTrainGitHubError):
                    self._run()
                setattr(self.transport, field, previous)
                self.assertEqual(
                    self.store.list_merge_train_batch_landing_plan_records(), (self.landing,)
                )
                self.assertEqual(
                    self.store.list_merge_train_batch_candidate_records()[0].status, "active"
                )
                self.assertEqual(
                    self.store.list_merge_train_controller_state_records()[0].status,
                    "reconcile_required",
                )
                current = self.store.list_merge_train_controller_state_records()[0]
                self.assertEqual(current.active_phase, checkpoint.active_phase)
                self.assertEqual(
                    current.active_pull_request_number, checkpoint.active_pull_request_number
                )
                self.assertEqual(current.step_payload, checkpoint.step_payload)
                self.assertEqual(
                    self.store.list_merge_landing_outcome_records()[0].status, "reconcile_required"
                )

    def test_interruption_after_retirement_resumes_without_another_provider_effect(self) -> None:
        self._record_failure(405)
        with patch.object(
            self.store,
            "write_merge_train_batch_candidate_record",
            side_effect=OSError("interrupted"),
        ):
            with self.assertRaises(OSError):
                self._run()
        status = build_merge_train_controller_status_read_model(
            store=self.store,
            repository=REPOSITORY,
            base_branch="main",
            generated_at="2026-08-11T03:05:00Z",
            current_policy_key=self.policy.policy.policies[0].policy_key,
            current_policy_sha256=self.policy.policy_sha256,
        )
        (diagnostic,) = status.reconciliation_diagnostics
        self.assertEqual(diagnostic.binding_detail, "plan_binding_changed")
        provider_reads = len(self.transport.calls)
        self._run()
        self.assertEqual(len(self.transport.calls), provider_reads)
        self.assertEqual(self.store.list_merge_train_controller_state_records()[0].status, "idle")
        self.assertEqual(
            self.store.list_merge_train_batch_candidate_records()[0].status, "superseded"
        )


if __name__ == "__main__":
    unittest.main()
