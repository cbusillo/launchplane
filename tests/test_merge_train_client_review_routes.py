import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from control_plane.merge_admission import MergeAdmissionDeniedError
from control_plane.merge_train_batch_candidate import (
    MergeTrainBatchCandidateRunOnceEnvelope,
    execute_merge_train_batch_candidate_run_once,
)
from control_plane.merge_train_branch_refresh import (
    require_merge_train_client_review_read_store,
)
from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.merge_train_run_once import (
    MergeTrainRunOnceEnvelope,
    execute_merge_train_run_once,
)
from control_plane.merge_train_scheduler import _run_level1
from tests.merge_train_policy_fixtures import (
    build_test_merge_train_policy,
    build_test_merge_train_policy_record,
)
from tests.test_merge_train_github import (
    _check_run,
    _combined_status,
    _github_branch,
    _github_pull_request,
    _label_events,
)

REPOSITORY = "cbusillo/sellyouroutboard"
CLIENT_STATUS = "launchplane/owner-review"


def _review_store(*, review_label: str = "owner-review") -> Any:
    profile = SimpleNamespace(
        is_active=True,
        repository=REPOSITORY,
        owner=SimpleNamespace(review_label=review_label),
    )
    return SimpleNamespace(
        list_product_profile_records=lambda: (profile,),
        list_merge_train_branch_refresh_records=lambda **_: (),
    )


def _labelled_pull_request_transport(
    client_statuses: tuple[dict[str, object], ...],
) -> RecordingMergeTrainGitHubTransport:
    pull = _github_pull_request(42)
    pull["labels"] = [{"name": "ready-to-merge"}, {"name": "owner-review"}]
    return RecordingMergeTrainGitHubTransport(
        responses=(
            _github_branch(),
            [pull],
            pull,
            {"permission": "admin"},
            _label_events(),
            _combined_status(statuses=({"context": "ci", "state": "success"}, *client_statuses)),
            {"check_runs": [_check_run("completed", "success")]},
        )
    )


CLIENT_REVIEW_CASES: tuple[tuple[str, tuple[dict[str, object], ...], str], ...] = (
    ("missing current-head review waits", (), "wait_for_checks"),
    (
        "requested changes block",
        ({"context": CLIENT_STATUS, "state": "failure"},),
        "block",
    ),
    (
        "accepted review keeps normal gates",
        ({"context": CLIENT_STATUS, "state": "success"},),
        "merge",
    ),
)


class LegacyRunOnceClientReviewTests(unittest.TestCase):
    def test_run_once_reads_active_profile_before_choosing_an_action(self) -> None:
        for name, statuses, expected_action in CLIENT_REVIEW_CASES:
            with self.subTest(name):
                transport = _labelled_pull_request_transport(statuses)
                with patch(
                    "control_plane.merge_train_run_once.UrllibMergeTrainGitHubTransport",
                    return_value=transport,
                ):
                    result = execute_merge_train_run_once(
                        request=MergeTrainRunOnceEnvelope(repository=REPOSITORY),
                        policy=build_test_merge_train_policy(),
                        policy_sha256="policy-sha",
                        token="token",
                        trace_id="trace",
                        recorded_at="2026-10-03T18:00:00Z",
                        review_store=_review_store(),
                    )
                dry_run = cast(dict[str, Any], result.accepted_result["dry_run_result"])
                self.assertEqual(dry_run["intended_next_action"], expected_action)

    def test_mutating_run_once_does_not_merge_without_current_head_review(self) -> None:
        transport = _labelled_pull_request_transport(())
        with patch(
            "control_plane.merge_train_run_once.UrllibMergeTrainGitHubTransport",
            return_value=transport,
        ):
            execute_merge_train_run_once(
                request=MergeTrainRunOnceEnvelope(repository=REPOSITORY, mutate=True),
                policy=build_test_merge_train_policy(),
                policy_sha256="policy-sha",
                token="token",
                trace_id="trace",
                recorded_at="2026-10-03T18:00:00Z",
                review_store=_review_store(),
            )
        self.assertFalse(
            [request for request in transport.requests if request.path.endswith("/merge")]
        )


class StandaloneCandidatePlanningClientReviewTests(unittest.TestCase):
    def test_plan_mode_reports_the_client_review_requirement(self) -> None:
        for name, statuses, expected_action in CLIENT_REVIEW_CASES:
            with self.subTest(name):
                batch_store = Mock()
                with patch(
                    "control_plane.merge_train_batch_candidate.UrllibMergeTrainGitHubTransport",
                    return_value=_labelled_pull_request_transport(statuses),
                ):
                    result = execute_merge_train_batch_candidate_run_once(
                        request=MergeTrainBatchCandidateRunOnceEnvelope(repository=REPOSITORY),
                        policy=build_test_merge_train_policy(),
                        policy_sha256="policy-sha",
                        token="token",
                        trace_id="trace",
                        recorded_at="2026-10-03T18:00:00Z",
                        batch_store=cast(Any, batch_store),
                        stack_collapse_store=cast(Any, Mock()),
                        review_store=_review_store(),
                    )
                dry_run = cast(dict[str, Any], result.accepted_result["dry_run_result"])
                self.assertEqual(dry_run["intended_next_action"], expected_action)
                self.assertEqual(
                    batch_store.write_merge_train_batch_candidate_record.called,
                    expected_action == "merge",
                )


class ClientReviewProfileStoreTests(unittest.TestCase):
    def test_store_without_profiles_refuses_the_route(self) -> None:
        store = SimpleNamespace(list_merge_train_branch_refresh_records=lambda **_: ())
        with self.assertRaises(MergeAdmissionDeniedError) as refused:
            require_merge_train_client_review_read_store(store, route="Merge train run-once")
        self.assertEqual(refused.exception.reason_code, "client_review_profiles_unavailable")

    def test_scheduled_level1_fails_before_reading_github_without_profiles(self) -> None:
        policy_record = build_test_merge_train_policy_record()
        repository_policy = policy_record.policy.find_repository_policy(
            repository=REPOSITORY, base_branch="main"
        )
        with patch(
            "control_plane.merge_train_scheduler.execute_recorded_merge_train_run_once"
        ) as run_once:
            result = _run_level1(
                record_store=SimpleNamespace(),
                policy_record=policy_record,
                repository_policy=repository_policy,
                token="token",
                trace_id="trace",
                now=lambda: "2026-10-03T18:00:00Z",
            )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason_code, "client_review_profiles_unavailable")
        run_once.assert_not_called()


if __name__ == "__main__":
    unittest.main()
