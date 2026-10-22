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
        self.client.prepare_candidate_check_retry.side_effect = lambda *, candidate: (
            lambda before_write: self.client.retry_candidate_checks(
                candidate=candidate, before_write=before_write
            )
        )

    def recover(
        self, *, mutate: bool = True, recorded_at: str = "2026-10-10T12:01:00Z"
    ) -> dict[str, Any]:
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
                recorded_at=recorded_at,
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
        self.client.retry_candidate_checks.side_effect = None
        result = self.recover()
        self.assertEqual(result["reason_code"], "candidate_checks_retry_pending")
        self.assertEqual(
            sum(
                not call.kwargs.get("resume", False)
                for call in self.client.retry_candidate_checks.call_args_list
            ),
            1,
        )
        self.client.add_pull_request_label.assert_not_called()
        self.client.candidate_check_retry_finished.return_value = True
        result = self.recover()
        self.assertEqual(result["reason_code"], "candidate_failure_isolated")
        self.client.add_pull_request_label.assert_called_once()
        self.assertEqual(
            sum(
                not call.kwargs.get("resume", False)
                for call in self.client.retry_candidate_checks.call_args_list
            ),
            1,
        )

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
        self.assertEqual(
            sum(
                not call.kwargs.get("resume", False)
                for call in self.client.retry_candidate_checks.call_args_list
            ),
            1,
        )

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
        self.client.retry_candidate_checks.side_effect = None
        result = self.recover()
        self.assertEqual(result["reason_code"], "candidate_checks_retry_pending")
        self.assertEqual(
            sum(
                not call.kwargs.get("resume", False)
                for call in self.client.retry_candidate_checks.call_args_list
            ),
            1,
        )
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
        self.assertEqual(
            sum(
                not call.kwargs.get("resume", False)
                for call in self.client.retry_candidate_checks.call_args_list
            ),
            1,
        )

    def test_unobserved_retry_becomes_visible_as_unavailable_without_blame(self) -> None:
        self.recover()
        self.record = next(
            record
            for record in self.store.list_merge_train_batch_candidate_records(status="active")
            if record.candidate.check_rerun_attempts
        )
        result = self.recover(recorded_at="2026-10-10T13:00:00Z")
        self.assertEqual(result["reason_code"], "candidate_check_retry_unavailable")
        self.client.add_pull_request_label.assert_not_called()
        self.assertEqual(
            sum(
                not call.kwargs.get("resume", False)
                for call in self.client.retry_candidate_checks.call_args_list
            ),
            1,
        )

    def test_recovery_lineage_cannot_start_another_same_sha_retry(self) -> None:
        self.record = self.record.model_copy(
            update={
                "candidate": self.record.candidate.model_copy(
                    update={"check_retry_of": "previous-failure"}
                )
            }
        )
        result = self.recover()
        self.assertEqual(result["reason_code"], "candidate_failure_isolated")
        self.client.retry_candidate_checks.assert_not_called()

    def test_preparation_failure_preserves_the_unused_retry(self) -> None:
        self.client.prepare_candidate_check_retry.side_effect = MergeTrainGitHubError(
            "read failed", status_code=503
        )
        before = self.store.list_merge_train_batch_candidate_records()
        result = self.recover()
        self.assertEqual(result["reason_code"], "candidate_check_retry_unavailable")
        self.assertEqual(self.store.list_merge_train_batch_candidate_records(), before)
        self.client.retry_candidate_checks.assert_not_called()
        self.client.add_pull_request_label.assert_not_called()

    def test_inconclusive_reserved_retry_names_its_pending_state(self) -> None:
        self.recover()
        self.record = next(
            record
            for record in self.store.list_merge_train_batch_candidate_records(status="active")
            if record.candidate.check_rerun_attempts
        )
        self.client.read_candidate_check_failures.return_value = replace(
            TEST_FAILURE, checks_pending=True
        )
        result = self.recover()
        self.assertEqual(result["reason_code"], "candidate_checks_retry_pending")
        self.client.add_pull_request_label.assert_not_called()

    def test_delayed_restart_restores_absent_ref_before_reading_failure_evidence(self) -> None:
        self.recover()
        self.record = next(
            record
            for record in self.store.list_merge_train_batch_candidate_records(status="active")
            if record.candidate.check_rerun_attempts
        )
        transport = Mock()
        ref: dict[str, str | None] = {"sha": None}

        def request(*, method: str, path: str, body: dict[str, object] | None = None) -> object:
            if method == "GET" and "/branches/" in path:
                if ref["sha"] is None:
                    raise MergeTrainGitHubError("absent", status_code=404)
                return {"commit": {"sha": ref["sha"]}}
            if method == "POST" and path.endswith("/git/refs"):
                assert body is not None
                ref["sha"] = str(body["sha"])
                return {}
            raise AssertionError(f"Unexpected write: {method} {path}")

        transport.request.side_effect = request
        publisher = GitHubMergeTrainClient(transport=transport)
        self.client.retry_candidate_checks.side_effect = publisher.retry_candidate_checks
        for failure in (
            replace(TEST_FAILURE, checks_pending=True),
            replace(TEST_FAILURE, checks_disagree=True),
            TEST_FAILURE,
        ):
            with self.subTest(failure=failure):
                ref["sha"] = None
                self.client.read_candidate_check_failures.return_value = failure
                self.client.candidate_check_retry_finished.side_effect = MergeTrainGitHubError(
                    "history unavailable", status_code=503
                )
                self.recover(recorded_at="2026-10-10T13:00:00Z")
                self.assertEqual(ref["sha"], self.record.candidate.candidate_sha)
        self.client.add_pull_request_label.assert_not_called()


class CandidateCheckRetryProviderTests(TestCase):
    def setUp(self) -> None:
        from tests.test_merge_train_github import _batch_candidate

        self.candidate = _batch_candidate().model_copy(
            update={"candidate_sha": "candidate-sha", "check_rerun_attempts": {41: 1}}
        )
        self.execution: dict[str, Any] = {
            "id": 41,
            "workflow_id": 7,
            "run_number": 1,
            "run_attempt": 1,
            "head_sha": self.candidate.candidate_sha,
            "head_branch": self.candidate.candidate_ref.removeprefix("refs/heads/"),
            "event": "push",
            "repository": {"full_name": self.candidate.repository},
            "head_repository": {"full_name": self.candidate.repository, "id": 9},
            "check_suite_id": 10,
            "status": "completed",
            "conclusion": "failure",
        }
        self.transport = Mock()
        self.ref_sha: str | None = self.candidate.candidate_sha
        self.fail_create = False
        self.lose_create_reply = False
        self.inventory = [self.execution]
        self.transport.request.side_effect = self.publication_request
        self.client = GitHubMergeTrainClient(transport=self.transport)

    def publication_request(
        self, *, method: str, path: str, body: dict[str, object] | None = None
    ) -> object:
        if method == "GET" and "/branches/" in path:
            if self.ref_sha is None:
                raise MergeTrainGitHubError("ref absent", status_code=404)
            return {"commit": {"sha": self.ref_sha}}
        if method == "DELETE" and "/git/refs/" in path:
            self.ref_sha = None
            return None
        if method == "POST" and path.endswith("/git/refs"):
            assert body is not None
            if self.fail_create:
                self.fail_create = False
                raise MergeTrainGitHubError("create unavailable", status_code=503)
            self.ref_sha = str(body["sha"])
            if self.lose_create_reply:
                self.lose_create_reply = False
                raise MergeTrainGitHubError("create reply lost", status_code=503)
            return {"object": {"sha": self.ref_sha}}
        if method == "GET" and "/actions/runs?" in path:
            return {"workflow_runs": self.inventory, "total_count": len(self.inventory)}
        if method == "GET" and "/actions/runs/" in path:
            return self.execution
        raise AssertionError(f"Unsupported existing-token effect: {method} {path}")

    def test_republishes_the_exact_sha_with_existing_contents_permission(self) -> None:
        checkpoint = Mock()
        self.client.retry_candidate_checks(candidate=self.candidate, before_write=checkpoint)
        self.assertEqual(self.ref_sha, self.candidate.candidate_sha)
        writes = [
            call.kwargs
            for call in self.transport.request.call_args_list
            if call.kwargs["method"] != "GET"
        ]
        self.assertEqual([call["method"] for call in writes], ["DELETE", "POST"])
        self.assertEqual(
            writes[1]["body"],
            {"ref": self.candidate.candidate_ref, "sha": self.candidate.candidate_sha},
        )
        self.assertEqual(checkpoint.call_count, len(writes))

    def test_prepared_retry_has_no_prerequisite_reads_before_its_first_write(self) -> None:
        publish = self.client.prepare_candidate_check_retry(candidate=self.candidate)
        self.transport.reset_mock()
        publish(Mock())
        self.assertEqual(self.transport.request.call_args_list[0].kwargs["method"], "DELETE")
        self.assertEqual(self.ref_sha, self.candidate.candidate_sha)

    def test_moved_or_noncanonical_ref_is_never_overwritten(self) -> None:
        for moved in (False, True):
            with self.subTest(moved=moved):
                self.transport.reset_mock()
                self.ref_sha = "unexpected-sha" if moved else self.candidate.candidate_sha
                candidate = (
                    self.candidate
                    if moved
                    else self.candidate.model_copy(update={"candidate_ref": "refs/heads/main"})
                )
                with self.assertRaises(MergeTrainGitHubError):
                    self.client.retry_candidate_checks(candidate=candidate, before_write=Mock())
                self.assertTrue(
                    all(
                        call.kwargs["method"] == "GET"
                        for call in self.transport.request.call_args_list
                    )
                )

    def test_partial_publication_resumes_without_another_delete_or_duplicate_push(self) -> None:
        for reply_lost in (False, True):
            with self.subTest(reply_lost=reply_lost):
                self.transport.reset_mock()
                self.ref_sha = self.candidate.candidate_sha
                self.fail_create = not reply_lost
                self.lose_create_reply = reply_lost
                with self.assertRaises(MergeTrainGitHubError):
                    self.client.retry_candidate_checks(
                        candidate=self.candidate, before_write=Mock()
                    )
                self.client.retry_candidate_checks(
                    candidate=self.candidate, before_write=Mock(), resume=True
                )
                self.assertEqual(self.ref_sha, self.candidate.candidate_sha)
                calls_before = len(self.transport.request.call_args_list)
                self.client.retry_candidate_checks(
                    candidate=self.candidate, before_write=Mock(), resume=True
                )
                self.assertEqual(len(self.transport.request.call_args_list), calls_before + 1)
                self.assertEqual(
                    sum(
                        call.kwargs["method"] == "DELETE"
                        for call in self.transport.request.call_args_list
                    ),
                    1,
                )

    def test_unknown_or_nonpush_execution_skips_retry(self) -> None:
        check = {
            "id": 71,
            "head_sha": self.candidate.candidate_sha,
            "app": {"id": 19, "slug": "github-actions"},
            "check_suite": {"id": 10},
            "details_url": f"https://github.com/{self.candidate.repository}/actions/runs/41/job/71",
            "status": "completed",
            "conclusion": "failure",
        }
        original = self.execution.copy()
        for change in (
            {"head_sha": "another-sha"},
            {"head_branch": "main"},
            {"id": 42},
            {"event": "pull_request"},
            {"event": "workflow_dispatch"},
            {"head_repository": {"full_name": "fork/repo"}},
            {"run_attempt": None},
        ):
            with self.subTest(change=change):
                self.execution = {**original, **change}
                self.assertEqual(
                    self.client._candidate_check_retry_runs(
                        candidate=self.candidate, checks=[check]
                    ),
                    (),
                )
        self.execution = original
        self.assertEqual(
            self.client._candidate_check_retry_runs(candidate=self.candidate, checks=[check]),
            ((41, 1),),
        )

    def test_original_or_pending_execution_does_not_prove_repeat_failure(self) -> None:
        for status, finished in (("queued", False), ("in_progress", False), ("completed", True)):
            with self.subTest(status=status):
                self.inventory = [
                    self.execution,
                    {
                        **self.execution,
                        "id": 42,
                        "run_number": 2,
                        "check_suite_id": 20,
                        "status": status,
                    },
                ]
                self.assertEqual(
                    self.client.candidate_check_retry_finished(candidate=self.candidate), finished
                )
        self.inventory = [self.execution]
        self.assertFalse(self.client.candidate_check_retry_finished(candidate=self.candidate))

    def test_missing_actions_read_keeps_existing_failure_diagnostics(self) -> None:
        check = {
            "id": 71,
            "name": "ci-gate",
            "head_sha": self.candidate.candidate_sha,
            "app": {"id": 19, "slug": "github-actions"},
            "check_suite": {"id": 10},
            "details_url": f"https://github.com/{self.candidate.repository}/actions/runs/41/job/71",
            "status": "completed",
            "conclusion": "failure",
        }

        def request(**kwargs: Any) -> object:
            if "/annotations?" in kwargs["path"]:
                return []
            raise MergeTrainGitHubError("Actions read unavailable", status_code=403)

        with (
            patch(
                "control_plane.merge_train_github._required_branch_checks",
                return_value=(("ci-gate", 19),),
            ),
            patch(
                "control_plane.merge_train_github._list_check_runs",
                return_value={
                    "check_runs": [
                        check,
                        {
                            **check,
                            "id": 72,
                            "conclusion": "success",
                            "check_suite": {"id": 20},
                            "details_url": f"https://github.com/{self.candidate.repository}/actions/runs/42/job/72",
                        },
                    ],
                    "actions_observation_error": "actions_read_not_granted",
                },
            ),
            patch(
                "control_plane.merge_train_github._list_commit_statuses",
                return_value={"statuses": []},
            ),
        ):
            self.transport.request.side_effect = request
            failures = self.client.read_candidate_check_failures(candidate=self.candidate)
        self.assertTrue(failures.conclusive)
        self.assertEqual(failures.retry_runs, ())
        self.assertEqual(failures.check_names, ("ci-gate",))
        self.assertFalse(failures.checks_disagree)

    def test_transient_run_read_is_unavailable_instead_of_skipping_retry(self) -> None:
        check = {
            "id": 71,
            "head_sha": self.candidate.candidate_sha,
            "app": {"id": 19, "slug": "github-actions"},
            "check_suite": {"id": 10},
            "details_url": f"https://github.com/{self.candidate.repository}/actions/runs/41/job/71",
            "status": "completed",
            "conclusion": "failure",
        }
        for error in (
            MergeTrainGitHubError("temporary", status_code=503),
            MergeTrainGitHubError("transport unavailable"),
            MergeTrainGitHubError("quota", status_code=403, rate_limited=True),
        ):
            with self.subTest(error=error):
                self.transport.request.side_effect = error
                with self.assertRaises(MergeTrainGitHubError):
                    self.client._candidate_check_retry_runs(
                        candidate=self.candidate, checks=[check]
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
                        "check_runs": [failed, {**passed, **change}],
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
