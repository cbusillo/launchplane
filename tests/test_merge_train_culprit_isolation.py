import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import Mock, patch

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchCandidateRecord,
    build_merge_train_batch_candidate,
    build_merge_train_batch_candidate_record,
)
from control_plane.merge_train import (
    MergeTrainDryRunSnapshot,
    MergeTrainPullRequestSnapshot,
    build_merge_train_dry_run_result,
)
from control_plane.merge_train_candidate_failures import (
    CandidateCheckFailure,
    CandidateCheckFailures,
    classify_candidate_check_failures,
)
from control_plane.merge_train_controller_feedback import build_feedback_payloads
from control_plane.merge_train_controller_run_once import try_reflow_failed_merge_train_candidate
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    RecordingMergeTrainGitHubTransport,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from tests.support.merge_train import _FakeExpandedMergeTrainSnapshotReader

TEST_FAILURE = CandidateCheckFailures(
    checks=(
        CandidateCheckFailure(
            name="test_shard (0)",
            conclusion="failure",
            url="https://github.com/cbusillo/sellyouroutboard/actions/runs/1/job/2",
            infrastructure=False,
            failed_tests=("tests.test_widget.WidgetTests.test_renders",),
        ),
    )
)
RUNNER_LOSS = CandidateCheckFailures(
    checks=(
        CandidateCheckFailure(name="ci-gate", conclusion="cancelled", url="", infrastructure=True),
    )
)


class _TrainSimulation:
    """Drive failed-candidate recovery against a queue, with CI outcomes chosen by the test.

    A candidate passes unless it contains a culprit; passing members land and
    move the base, as the landing path would.
    """

    def __init__(self, test: unittest.TestCase, store: FilesystemRecordStore, count: int) -> None:
        self.test = test
        self.store = store
        self.policy = build_test_merge_train_policy()
        template = (
            _FakeExpandedMergeTrainSnapshotReader(transport=object())
            .read_merge_train_snapshot(repository="cbusillo/sellyouroutboard", base_branch="main")
            .pull_requests[1]
        )
        self.snapshot = MergeTrainDryRunSnapshot(
            repository="cbusillo/sellyouroutboard",
            base_branch="main",
            base_sha="base-0",
            pull_requests=tuple(
                template.model_copy(
                    update={
                        "number": number,
                        "head_sha": f"head-{number}",
                        "head_ref": f"feature-{number}",
                        "created_at": f"2026-10-10T12:0{number}:00Z",
                    }
                )
                for number in range(1, count + 1)
            ),
        )
        self.landed: list[int] = []
        self.blocked: list[int] = []
        self.candidates: list[tuple[int, ...]] = []
        self.reason_codes: list[str] = []

    def _plan_from_queue(self) -> MergeTrainBatchCandidateRecord:
        candidate = build_merge_train_batch_candidate(
            dry_run_result=build_merge_train_dry_run_result(
                policy=self.policy, snapshot=self.snapshot, batch_landing=True
            ),
            base_sha=self.snapshot.base_sha,
            policy_sha256="policy-digest",
            created_at="2026-10-10T12:00:00Z",
        )
        record = build_merge_train_batch_candidate_record(
            candidate=candidate, source="test", updated_at="2026-10-10T12:00:00Z"
        )
        self.store.write_merge_train_batch_candidate_record(record)
        return record

    def _land(self, record: MergeTrainBatchCandidateRecord) -> None:
        # Completed landings leave no active candidate behind.
        self.store.write_merge_train_batch_candidate_record(
            record.model_copy(update={"status": "superseded"})
        )
        numbers = {entry.pull_request_number for entry in record.candidate.entries}
        self.landed.extend(sorted(numbers))
        self.snapshot = self.snapshot.model_copy(
            update={
                "base_sha": f"base-{len(self.landed)}",
                "pull_requests": tuple(
                    pull_request
                    for pull_request in self.snapshot.pull_requests
                    if pull_request.number not in numbers
                ),
            }
        )

    def _block(self, number: int) -> None:
        self.blocked.append(number)
        self.snapshot = self.snapshot.model_copy(
            update={
                "pull_requests": tuple(
                    pull_request.model_copy(
                        update={"labels": (*pull_request.labels, "merge-blocked")}
                    )
                    if pull_request.number == number
                    else pull_request
                    for pull_request in self.snapshot.pull_requests
                )
            }
        )

    def _eligible(self) -> list[MergeTrainPullRequestSnapshot]:
        return [pr for pr in self.snapshot.pull_requests if "merge-blocked" not in pr.labels]

    def run(
        self,
        *,
        culprits: set[int],
        infrastructure_failure: Any = lambda: None,
        max_passes: int = 20,
    ) -> None:
        record: MergeTrainBatchCandidateRecord | None = None
        for _ in range(max_passes):
            if record is None:
                if not self._eligible():
                    return
                record = self._plan_from_queue()
            candidate = record.candidate
            self.candidates.append(tuple(entry.pull_request_number for entry in candidate.entries))
            members = {entry.pull_request_number for entry in candidate.entries}
            failure = infrastructure_failure() or (TEST_FAILURE if culprits & members else None)
            if failure is None:
                self._land(record)
                record = None
                continue
            failed = candidate.model_copy(
                update={
                    "candidate_sha": f"candidate-{len(self.candidates)}",
                    "status": "failed",
                    "required_checks_status": "fail",
                }
            )
            failed_record = build_merge_train_batch_candidate_record(
                candidate=failed, source="test", updated_at="2026-10-10T12:00:00Z"
            )
            self.store.write_merge_train_batch_candidate_record(failed_record)
            client = Mock()
            client.read_merge_train_snapshot.return_value = self.snapshot
            client.observe_batch_candidate_checks.return_value = failed
            client.read_candidate_check_failures.return_value = failure
            with patch(
                "control_plane.merge_train_controller_run_once.changed_closed_batch_body",
                return_value=False,
            ):
                result = try_reflow_failed_merge_train_candidate(
                    candidate_store=self.store,
                    stack_collapse_store=self.store,
                    active_candidate_record=failed_record,
                    policy=self.policy,
                    policy_sha256="policy-digest",
                    transport=object(),  # type: ignore[arg-type]
                    github_client=cast(GitHubMergeTrainClient, client),
                    repository=self.snapshot.repository,
                    base_branch=self.snapshot.base_branch,
                    merge_method="merge",
                    recorded_at="2026-10-10T12:01:00Z",
                    trace_id="isolation",
                    mutate=True,
                    lease=Mock(),
                )
            assert result is not None
            self.reason_codes.append(str(result.get("reason_code")))
            if result["controller_action"] == "block":
                labelled = [
                    call.kwargs["pull_request_number"]
                    for call in client.add_pull_request_label.call_args_list
                ]
                self.test.assertEqual(len(labelled), 1)
                self._block(labelled[0])
                self.test.assertEqual(
                    self.store.list_merge_train_batch_candidate_records(status="active"), ()
                )
                record = None
                continue
            self.test.assertEqual(result["controller_action"], "plan_candidate", result)
            record = next(
                active
                for active in self.store.list_merge_train_batch_candidate_records(status="active")
                if active.record_id == result["merge_train_batch_candidate_record_id"]
            )
        self.test.fail("simulation did not settle")


class CulpritIsolationTests(unittest.TestCase):
    def test_single_culprit_is_blocked_and_the_rest_land(self) -> None:
        with TemporaryDirectory() as directory:
            train = _TrainSimulation(self, FilesystemRecordStore(state_dir=Path(directory)), 4)
            train.run(culprits={3})
        self.assertEqual(train.blocked, [3])
        self.assertEqual(sorted(train.landed), [1, 2, 4])
        self.assertEqual(train.candidates[:2], [(1, 2, 3, 4), (1, 2)])

    def test_two_culprits_are_each_blocked_and_the_rest_land(self) -> None:
        with TemporaryDirectory() as directory:
            train = _TrainSimulation(self, FilesystemRecordStore(state_dir=Path(directory)), 6)
            train.run(culprits={2, 5})
        self.assertEqual(sorted(train.blocked), [2, 5])
        self.assertEqual(sorted(train.landed), [1, 3, 4, 6])

    def test_infrastructure_failure_is_retried_without_blame(self) -> None:
        outcomes = iter((RUNNER_LOSS, None))
        with TemporaryDirectory() as directory:
            train = _TrainSimulation(self, FilesystemRecordStore(state_dir=Path(directory)), 3)
            train.run(culprits=set(), infrastructure_failure=lambda: next(outcomes, None))
            retry = next(
                record.candidate
                for record in train.store.list_merge_train_batch_candidate_records()
                if record.candidate.infrastructure_retry_of
            )
        self.assertEqual(train.blocked, [])
        self.assertEqual(sorted(train.landed), [1, 2, 3])
        self.assertEqual(train.candidates, [(1, 2, 3), (1, 2, 3)])
        self.assertEqual(train.reason_codes, ["infrastructure_retry"])
        self.assertTrue(retry.candidate_ref.endswith("-infrastructure-retry"))

    def test_repeated_infrastructure_failure_falls_back_to_isolation(self) -> None:
        with TemporaryDirectory() as directory:
            train = _TrainSimulation(self, FilesystemRecordStore(state_dir=Path(directory)), 2)
            train.run(culprits={2}, infrastructure_failure=lambda: RUNNER_LOSS)
        self.assertEqual(train.reason_codes[:2], ["infrastructure_retry", "failure_isolation"])
        self.assertIn(2, train.blocked)

    def test_dry_run_reports_isolation_without_writing(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            train = _TrainSimulation(self, store, 2)
            record = train._plan_from_queue()
            failed_record = build_merge_train_batch_candidate_record(
                candidate=record.candidate.model_copy(
                    update={
                        "candidate_sha": "candidate-1",
                        "status": "failed",
                        "required_checks_status": "fail",
                    }
                ),
                source="test",
                updated_at="2026-10-10T12:00:00Z",
            )
            store.write_merge_train_batch_candidate_record(failed_record)
            client = Mock()
            client.read_merge_train_snapshot.return_value = train.snapshot
            client.read_candidate_check_failures.return_value = TEST_FAILURE
            with patch(
                "control_plane.merge_train_controller_run_once.changed_closed_batch_body",
                return_value=False,
            ):
                result = try_reflow_failed_merge_train_candidate(
                    candidate_store=store,
                    stack_collapse_store=store,
                    active_candidate_record=failed_record,
                    policy=train.policy,
                    policy_sha256="policy-digest",
                    transport=object(),  # type: ignore[arg-type]
                    github_client=cast(GitHubMergeTrainClient, client),
                    repository=train.snapshot.repository,
                    base_branch=train.snapshot.base_branch,
                    merge_method="merge",
                    recorded_at="2026-10-10T12:01:00Z",
                    trace_id="isolation",
                    mutate=False,
                    lease=Mock(),
                )
            records = store.list_merge_train_batch_candidate_records()
        assert result is not None
        self.assertEqual(result["reason_code"], "failure_isolation")
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(len(records), 2)
        feedback = {
            payload["pull_request_number"]: payload["message"]
            for payload in build_feedback_payloads(response={"result": result})
            if payload["event"] == "blocked"
        }
        self.assertEqual(list(feedback), [2])
        self.assertIn("retests the pull requests ahead of it", cast(str, feedback[2]))

    def test_unreadable_failure_evidence_keeps_the_batch_stopped(self) -> None:
        from control_plane.merge_train_github import MergeTrainGitHubError

        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            train = _TrainSimulation(self, store, 2)
            record = train._plan_from_queue()
            failed_record = build_merge_train_batch_candidate_record(
                candidate=record.candidate.model_copy(
                    update={
                        "candidate_sha": "candidate-1",
                        "status": "failed",
                        "required_checks_status": "fail",
                    }
                ),
                source="test",
                updated_at="2026-10-10T12:00:00Z",
            )
            store.write_merge_train_batch_candidate_record(failed_record)
            client = Mock()
            client.read_merge_train_snapshot.return_value = train.snapshot
            client.read_candidate_check_failures.side_effect = MergeTrainGitHubError(
                "unavailable", status_code=503
            )
            with patch(
                "control_plane.merge_train_controller_run_once.changed_closed_batch_body",
                return_value=False,
            ):
                result = try_reflow_failed_merge_train_candidate(
                    candidate_store=store,
                    stack_collapse_store=store,
                    active_candidate_record=failed_record,
                    policy=train.policy,
                    policy_sha256="policy-digest",
                    transport=object(),  # type: ignore[arg-type]
                    github_client=cast(GitHubMergeTrainClient, client),
                    repository=train.snapshot.repository,
                    base_branch=train.snapshot.base_branch,
                    merge_method="merge",
                    recorded_at="2026-10-10T12:01:00Z",
                    trace_id="isolation",
                    mutate=True,
                    lease=Mock(),
                )
        assert result is not None
        self.assertEqual(result["controller_action"], "candidate_failed")
        client.add_pull_request_label.assert_not_called()


def _check_run(
    check_run_id: int, name: str, conclusion: str, *, status: str = "completed"
) -> dict[str, object]:
    return {
        "id": check_run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.com/o/r/actions/runs/1/job/{check_run_id}",
    }


def _exit_code() -> dict[str, object]:
    return {"annotation_level": "failure", "message": "Process completed with exit code 1."}


class CandidateFailureClassificationTests(unittest.TestCase):
    def _classify(
        self,
        check_runs: list[dict[str, object]],
        annotations: dict[int, list[dict[str, object]]],
        statuses: list[dict[str, object]] | None = None,
    ) -> CandidateCheckFailures:
        return classify_candidate_check_failures(
            check_runs=check_runs,
            commit_statuses=statuses or [],
            read_annotations=lambda check_run_id: annotations.get(check_run_id, []),
        )

    def test_unacquired_hosted_runner_is_infrastructure(self) -> None:
        # Shape of the October 5 train run whose hosted jobs never got a runner.
        failures = self._classify(
            [
                _check_run(1, "audit_policy", "cancelled"),
                _check_run(2, "ci-gate", "cancelled"),
                _check_run(3, "test", "success"),
            ],
            {},
        )
        self.assertTrue(failures.infrastructure_only)
        self.assertEqual(failures.check_names, ("audit_policy", "ci-gate"))

    def test_runner_shutdown_with_dependent_gate_failures_is_infrastructure(self) -> None:
        failures = self._classify(
            [
                _check_run(1, "test_shard (3)", "failure"),
                _check_run(2, "test", "failure"),
                _check_run(3, "ci-gate", "failure"),
            ],
            {
                1: [
                    {
                        "annotation_level": "failure",
                        "message": "The runner has received a shutdown signal. This can "
                        "happen when the runner service is stopped.",
                    }
                ],
                2: [_exit_code()],
                3: [_exit_code()],
            },
        )
        self.assertTrue(failures.infrastructure_only)

    def test_named_test_failure_is_blamed_even_beside_cancelled_jobs(self) -> None:
        failures = self._classify(
            [
                _check_run(1, "test_shard (0)", "failure"),
                _check_run(2, "test_shard (1)", "cancelled"),
                _check_run(3, "ci-gate", "failure"),
            ],
            {
                1: [
                    _exit_code(),
                    {
                        "annotation_level": "failure",
                        "title": "Failed test",
                        "message": "tests.test_widget.WidgetTests.test_renders",
                    },
                ],
                3: [_exit_code()],
            },
        )
        self.assertFalse(failures.infrastructure_only)
        self.assertEqual(failures.failed_tests, ("tests.test_widget.WidgetTests.test_renders",))

    def test_ordinary_failure_and_failing_status_are_not_infrastructure(self) -> None:
        self.assertFalse(
            self._classify(
                [_check_run(1, "ci-gate", "failure")], {1: [_exit_code()]}
            ).infrastructure_only
        )
        failures = self._classify(
            [_check_run(1, "lint", "cancelled")],
            {},
            statuses=[{"context": "external/gate", "state": "failure"}],
        )
        self.assertFalse(failures.infrastructure_only)
        self.assertIn("external/gate", failures.check_names)

    def test_untrusted_names_and_test_ids_are_not_echoed(self) -> None:
        failures = self._classify(
            [_check_run(1, "gate `@someone` [x](y)", "failure")],
            {
                1: [
                    {
                        "annotation_level": "failure",
                        "title": "Failed test",
                        "message": "see https://example.invalid @someone",
                    }
                ]
            },
        )
        self.assertEqual(failures.check_names, ("unnamed check",))
        self.assertEqual(failures.failed_tests, ())

    def test_client_reads_annotations_only_for_failed_runs(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                {
                    "total_count": 2,
                    "check_runs": [
                        _check_run(11, "test_shard (0)", "failure"),
                        _check_run(12, "audit_policy", "cancelled"),
                    ],
                },
                {"total_count": 0, "statuses": []},
                [
                    {
                        "annotation_level": "failure",
                        "title": "Failed test",
                        "message": "tests.test_widget.WidgetTests.test_renders",
                    }
                ],
            )
        )
        candidate = MergeTrainBatchCandidate.model_validate(
            {
                "batch_id": "batch",
                "repository": "cbusillo/sellyouroutboard",
                "base_branch": "main",
                "base_sha": "base",
                "policy_key": "key",
                "policy_sha256": "digest",
                "candidate_ref": "refs/heads/launchplane/train/batch",
                "candidate_sha": "candidate-sha",
                "entries": [{"pull_request_number": 1, "position": 1, "head_sha": "head-1"}],
                "created_at": "2026-10-10T12:00:00Z",
                "updated_at": "2026-10-10T12:00:00Z",
            }
        )
        failures = GitHubMergeTrainClient(transport=transport).read_candidate_check_failures(
            candidate=candidate
        )
        annotation_paths = [
            request.path for request in transport.requests if "/annotations" in request.path
        ]
        self.assertEqual(
            annotation_paths,
            ["/repos/cbusillo/sellyouroutboard/check-runs/11/annotations?per_page=50"],
        )
        self.assertFalse(failures.infrastructure_only)
        self.assertEqual(failures.check_names, ("test_shard (0)", "audit_policy"))


if __name__ == "__main__":
    unittest.main()
