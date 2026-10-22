from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest import TestCase
from unittest.mock import Mock, patch

from control_plane.contracts.merge_train_batch import build_merge_train_batch_candidate_record
from control_plane.merge_train_controller_run_once import try_reflow_failed_merge_train_candidate
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    MergeTrainBaseCheckObservation,
    MergeTrainGitHubError,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.test_merge_train_culprit_isolation import TEST_FAILURE, _TrainSimulation


class CandidateCheckRetryTests(TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        self.store = FilesystemRecordStore(state_dir=Path(directory))
        self.train = _TrainSimulation(self, self.store, 1)
        planned = self.train._plan_from_queue()
        self.record = build_merge_train_batch_candidate_record(
            candidate=planned.candidate.model_copy(
                update={
                    "candidate_sha": "candidate-sha",
                    "status": "failed",
                    "required_checks_status": "fail",
                }
            ),
            source="service:controller:test",
            updated_at="2026-10-10T12:00:00Z",
        )
        self.store.write_merge_train_batch_candidate_record(self.record)
        self.client = Mock()
        self.client.read_merge_train_snapshot.return_value = self.train.snapshot
        self.client.observe_batch_candidate_checks.return_value = self.record.candidate
        self.client.read_candidate_check_failures.return_value = replace(
            TEST_FAILURE, retry_runs=((41, 1),)
        )
        self.client.read_base_check_observation.return_value = MergeTrainBaseCheckObservation(
            "pass"
        )
        self.client.candidate_block_label_events.return_value = []
        self.client.candidate_block_application.return_value = {"status": "applied", "event_id": 1}
        self.client.candidate_check_retry_finished.return_value = False

    def recover(self, *, mutate: bool = True) -> dict[str, Any]:
        lease = Mock()
        lease.record.updated_at = "2026-10-10T12:01:00Z"
        lease.record.step_payload = {}
        lease.record.ordinary_job_binding = None
        with patch(
            "control_plane.merge_train_controller_run_once.changed_closed_batch_body",
            return_value=False,
        ):
            result = try_reflow_failed_merge_train_candidate(
                candidate_store=self.store,
                stack_collapse_store=self.store,
                active_candidate_record=self.record,
                policy=self.train.policy,
                policy_sha256="policy-digest",
                github_client=cast(GitHubMergeTrainClient, self.client),
                repository=self.train.snapshot.repository,
                base_branch=self.train.snapshot.base_branch,
                merge_method="merge",
                recorded_at="2026-10-10T12:01:00Z",
                trace_id="check-retry",
                mutate=mutate,
                lease=lease,
            )
        assert result is not None
        return result

    def test_retry_is_persisted_before_dispatch_and_survives_restart(self) -> None:
        def dispatch(**kwargs: object) -> None:
            records = self.store.list_merge_train_batch_candidate_records(status="active")
            retried = next(record for record in records if record.candidate.check_rerun_attempts)
            self.assertEqual(retried.candidate.candidate_sha, self.record.candidate.candidate_sha)
            self.assertEqual(retried.candidate.check_rerun_attempts, {41: 1})

        self.client.retry_candidate_checks.side_effect = dispatch
        result = self.recover()
        self.assertEqual(result["reason_code"], "candidate_checks_retry_requested")
        self.client.add_pull_request_label.assert_not_called()
        self.store = FilesystemRecordStore(state_dir=self.store.state_dir)
        self.record = next(
            record
            for record in self.store.list_merge_train_batch_candidate_records(status="active")
            if record.record_id == result["merge_train_batch_candidate_record_id"]
        )
        result = self.recover()
        self.assertEqual(result["reason_code"], "candidate_checks_retry_pending")
        self.client.retry_candidate_checks.assert_called_once()
        self.client.add_pull_request_label.assert_not_called()
        self.client.candidate_check_retry_finished.return_value = True
        result = self.recover()
        self.assertEqual(result["reason_code"], "candidate_failure_isolated")
        self.client.add_pull_request_label.assert_called_once()
        self.client.retry_candidate_checks.assert_called_once()

    def test_disagreeing_runs_are_transient_even_after_retry_fails(self) -> None:
        self.recover()
        self.record = next(
            record
            for record in self.store.list_merge_train_batch_candidate_records(status="active")
            if record.candidate.check_rerun_attempts
        )
        self.client.read_candidate_check_failures.return_value = replace(
            TEST_FAILURE, retry_runs=((41, 2),), checks_disagree=True
        )
        self.client.candidate_check_retry_finished.return_value = True
        result = self.recover()
        self.assertEqual(result["reason_code"], "candidate_checks_disagree")
        self.client.add_pull_request_label.assert_not_called()
        self.client.retry_candidate_checks.assert_called_once()

    def test_dry_run_does_not_consume_retry_or_dispatch(self) -> None:
        before = self.store.list_merge_train_batch_candidate_records()
        result = self.recover(mutate=False)
        self.assertEqual(result["reason_code"], "candidate_checks_retry_requested")
        self.assertEqual(self.store.list_merge_train_batch_candidate_records(), before)
        self.client.retry_candidate_checks.assert_not_called()
        self.client.add_pull_request_label.assert_not_called()

    def test_unknown_retry_outcome_is_not_sent_again(self) -> None:
        self.client.retry_candidate_checks.side_effect = MergeTrainGitHubError("reply lost")
        with self.assertRaises(MergeTrainGitHubError):
            self.recover()
        self.record = next(
            record
            for record in self.store.list_merge_train_batch_candidate_records(status="active")
            if record.candidate.check_rerun_attempts
        )
        result = self.recover()
        self.assertEqual(result["reason_code"], "candidate_checks_retry_pending")
        self.client.retry_candidate_checks.assert_called_once()
        self.client.add_pull_request_label.assert_not_called()

    def test_admin_hold_is_never_retried_or_attributed_to_a_member(self) -> None:
        self.client.read_candidate_check_failures.return_value = replace(
            TEST_FAILURE,
            checks=(replace(TEST_FAILURE.checks[0], conclusion="action_required"),),
            retry_runs=((41, 1),),
        )
        result = self.recover()
        self.assertEqual(result["reason_code"], "candidate_checks_admin_hold")
        self.client.retry_candidate_checks.assert_not_called()
        self.client.add_pull_request_label.assert_not_called()

    def test_passing_retry_rejoins_normal_candidate_observation(self) -> None:
        self.recover()
        self.record = next(
            record
            for record in self.store.list_merge_train_batch_candidate_records(status="active")
            if record.candidate.check_rerun_attempts
        )
        self.client.observe_batch_candidate_checks.return_value = self.record.candidate.model_copy(
            update={"required_checks_status": "pass", "status": "passed"}
        )
        self.client.transport.request.return_value = []
        result = self.recover()
        self.assertEqual(result["controller_action"], "observe_candidate")
        self.assertEqual(result["candidate"]["required_checks_status"], "pass")
        self.client.add_pull_request_label.assert_not_called()
        self.client.retry_candidate_checks.assert_called_once()


class CandidateCheckRetryProviderTests(TestCase):
    def setUp(self) -> None:
        from tests.test_merge_train_github import _batch_candidate

        self.candidate = _batch_candidate().model_copy(
            update={"candidate_sha": "candidate-sha", "check_rerun_attempts": {41: 1}}
        )
        self.execution: dict[str, Any] = {
            "id": 41,
            "run_attempt": 1,
            "head_sha": self.candidate.candidate_sha,
            "head_branch": self.candidate.candidate_ref.removeprefix("refs/heads/"),
            "event": "push",
            "repository": {"full_name": self.candidate.repository},
            "head_repository": {"full_name": self.candidate.repository},
            "check_suite_id": 10,
            "status": "completed",
            "conclusion": "failure",
        }
        self.transport = Mock()
        self.transport.request.return_value = self.execution
        self.client = GitHubMergeTrainClient(transport=self.transport)

    def test_retries_only_failed_jobs_of_the_bound_run(self) -> None:
        self.client.retry_candidate_checks(candidate=self.candidate)
        self.assertEqual(
            self.transport.request.call_args.kwargs,
            {
                "method": "POST",
                "path": f"/repos/{self.candidate.repository}/actions/runs/41/rerun-failed-jobs",
            },
        )

    def test_changed_or_unproved_execution_never_dispatches(self) -> None:
        for change in (
            {"head_sha": "another-sha"},
            {"head_branch": "main"},
            {"id": 42},
            {"event": "workflow_dispatch"},
            {"repository": {"full_name": "other/repository"}},
            {"head_repository": {"full_name": "fork/repository"}},
            {"run_attempt": 2},
            {"run_attempt": None},
            {"status": "waiting"},
            {"conclusion": "action_required"},
        ):
            with self.subTest(change=change):
                self.transport.reset_mock()
                self.transport.request.return_value = {**self.execution, **change}
                with self.assertRaises(MergeTrainGitHubError):
                    self.client.retry_candidate_checks(candidate=self.candidate)
                self.assertTrue(
                    all(
                        call.kwargs["method"] == "GET"
                        for call in self.transport.request.call_args_list
                    )
                )

    def test_original_or_pending_execution_does_not_prove_repeat_failure(self) -> None:
        for attempt, status, finished in (
            (1, "completed", False),
            (2, "queued", False),
            (2, "in_progress", False),
            (2, "completed", True),
        ):
            with self.subTest(attempt=attempt, status=status):
                self.transport.request.return_value = {
                    **self.execution,
                    "run_attempt": attempt,
                    "status": status,
                }
                self.assertEqual(
                    self.client.candidate_check_retry_finished(candidate=self.candidate), finished
                )

    def test_disagreement_requires_success_with_matching_app_and_sha(self) -> None:
        failed: dict[str, Any] = {
            "id": 71,
            "name": "ci-gate",
            "head_sha": self.candidate.candidate_sha,
            "app": {"id": 19, "slug": "github-actions"},
            "check_suite": {"id": 10},
            "details_url": f"https://github.com/{self.candidate.repository}/actions/runs/41/job/71",
            "status": "completed",
            "conclusion": "failure",
        }
        passed = {
            **failed,
            "id": 72,
            "check_suite": {"id": 20},
            "details_url": f"https://github.com/{self.candidate.repository}/actions/runs/42/job/72",
            "conclusion": "success",
        }
        for change, disagrees in (
            ({}, True),
            (
                {
                    "id": 70,
                    "check_suite": {"id": 10},
                    "details_url": f"https://github.com/{self.candidate.repository}/actions/runs/41/job/70",
                },
                True,
            ),
            ({"app": {"id": 99, "slug": "github-actions"}}, False),
            ({"head_sha": "another-sha"}, False),
            ({"conclusion": "skipped"}, False),
            ({"name": "optional-check"}, False),
            ({"check_suite": {}}, False),
        ):
            with (
                self.subTest(change=change),
                patch(
                    "control_plane.merge_train_github._required_branch_checks",
                    return_value=(("ci-gate", 19),),
                ),
                patch(
                    "control_plane.merge_train_github._list_check_runs",
                    return_value={
                        "check_runs": [failed],
                        "check_runs_history": [failed, {**passed, **change}],
                    },
                ),
                patch(
                    "control_plane.merge_train_github._list_commit_statuses",
                    return_value={"statuses": []},
                ),
            ):
                self.transport.request.side_effect = lambda **kwargs: (
                    [] if "/annotations?" in kwargs["path"] else self.execution
                )
                failures = self.client.read_candidate_check_failures(candidate=self.candidate)
                self.assertEqual(failures.checks_disagree, disagrees)
                self.assertEqual(failures.retry_runs, ((41, 1),))
