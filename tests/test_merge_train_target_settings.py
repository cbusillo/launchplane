from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from unittest.mock import Mock

from control_plane.contracts.merge_train_batch import build_merge_train_batch_candidate_record
from control_plane.merge_train_controller_feedback import build_feedback_payloads
from control_plane.merge_train_controller_run_once import (
    MergeTrainControllerRunOnceEnvelope,
    execute_merge_train_controller_with_client,
)
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    MergeTrainGitHubPolicyUnavailableError,
    _required_branch_checks,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.support.merge_train_github import RecordingMergeTrainGitHubTransport
from tests.test_merge_train_culprit_isolation import _TrainSimulation
from tests.test_merge_train_policy_recovery import _NoAdmissionEvaluator

SETTING_GAP = "Require status checks on main."


def _unprotected_branch_error() -> MergeTrainGitHubPolicyUnavailableError:
    try:
        _required_branch_checks(
            transport=RecordingMergeTrainGitHubTransport(responses=({"protected": False},)),
            repository_path="cbusillo/sellyouroutboard",
            base_branch="main",
        )
    except MergeTrainGitHubPolicyUnavailableError as error:
        return error
    raise AssertionError("an unprotected branch must have no required checks")


class TargetSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = FilesystemRecordStore(Path(temporary.name))
        self.train = _TrainSimulation(self, self.store, 2)
        self.policy = build_test_merge_train_policy_record(
            repository=self.train.snapshot.repository
        )
        self.client = Mock()
        self.client.target_setting_gaps.return_value = (SETTING_GAP,)

    def run_pass(self) -> dict[str, object]:
        return execute_merge_train_controller_with_client(
            request=MergeTrainControllerRunOnceEnvelope(
                repository=self.train.snapshot.repository, mutate=True
            ),
            policy=self.policy.policy,
            policy_sha256=self.policy.policy_sha256,
            repository_policy=self.policy.policy.policies[0],
            github_client=cast(GitHubMergeTrainClient, self.client),
            trace_id="target-settings",
            recorded_at="2026-10-24T17:00:00Z",
            candidate_store=self.store,
            landing_store=self.store,
            stack_collapse_store=self.store,
            controller_state_store=self.store,
            admission_store=self.store,
            admission_evaluator=_NoAdmissionEvaluator(),
        ).accepted_result

    def assert_blocked_on_the_setting(self, result: dict[str, object], numbers: list[int]) -> None:
        self.assertEqual(result["controller_action"], "target_not_ready")
        feedback = build_feedback_payloads(response={"result": result})
        self.assertEqual([item["pull_request_number"] for item in feedback], numbers)
        self.assertTrue(all(item["event"] == "blocked" for item in feedback))
        self.assertTrue(all(SETTING_GAP in str(item["message"]) for item in feedback))
        state = self.store.list_merge_train_controller_state_records()[0]
        assert state.last_result is not None
        self.assertEqual(state.last_result.next_actor, "repository_agent")
        self.assertEqual(state.last_result.next_observation, SETTING_GAP)

    def test_ready_pull_requests_waiting_on_missing_required_checks_are_blocked(self) -> None:
        error = str(_unprotected_branch_error())
        self.client.read_merge_train_snapshot.return_value = self.train.snapshot.model_copy(
            update={
                "pull_requests": tuple(
                    pull_request.model_copy(
                        update={
                            "required_checks_status": "unknown",
                            "required_checks_observation_error": error,
                            "is_draft": pull_request.number == 2,
                        }
                    )
                    for pull_request in self.train.snapshot.pull_requests
                )
            }
        )

        self.assert_blocked_on_the_setting(self.run_pass(), [1])

    def test_batch_observation_without_required_checks_keeps_the_batch_and_blocks(self) -> None:
        self.write_observed_candidate()

        self.assert_blocked_on_the_setting(self.run_pass(), [1, 2])
        self.client.close_batch_pull_request.assert_not_called()
        self.assertTrue(self.store.list_merge_train_batch_candidate_records(status="active"))

    def write_observed_candidate(self) -> None:
        record = self.train._plan_from_queue()
        candidate = record.candidate.model_copy(
            update={
                "policy_sha256": self.policy.policy_sha256,
                "candidate_sha": "candidate-sha",
                "status": "ready_for_checks",
            }
        )
        self.store.write_merge_train_batch_candidate_record(
            build_merge_train_batch_candidate_record(
                candidate=candidate, source="test", updated_at=candidate.updated_at
            )
        )
        self.client.read_merge_train_snapshot.return_value = self.train.snapshot
        self.client.observe_batch_candidate_checks.side_effect = _unprotected_branch_error()

    def test_unexplained_missing_checks_error_still_records_reconciliation(self) -> None:
        self.client.target_setting_gaps.return_value = ()
        self.write_observed_candidate()

        with self.assertRaises(MergeTrainGitHubPolicyUnavailableError):
            self.run_pass()
        state = self.store.list_merge_train_controller_state_records()[0]
        self.assertEqual(state.reconciliation_status, "required")
        self.assertFalse(state.lease_owner)


if __name__ == "__main__":
    unittest.main()
