import unittest

from control_plane.contracts.merge_train_admission import evaluate_merge_train_admission
from control_plane.contracts.merge_train_policy import build_sellyouroutboard_main_merge_train_policy
from control_plane.contracts.merge_train_run_record import MergeTrainRunRecord
from control_plane.contracts.merge_train_run_record import build_merge_train_run_record
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.merge_train import MergeTrainPullRequestSnapshot
from control_plane.merge_train import MergeTrainCheckStatus
from control_plane.merge_train import build_merge_train_dry_run_result
from control_plane.merge_train_admission import evaluate_merge_train_admission_from_store
from control_plane.workflows.merge_train_worker import MergeTrainWorkerClients
from control_plane.workflows.merge_train_worker import run_merge_train_worker_step


class _FakeMergeClient:
    def merge_pull_request(
        self,
        *,
        repository: str,
        pull_request_number: int,
        head_sha: str,
        merge_method: str,
    ) -> str:
        return f"merge-{pull_request_number}"


class _NoopClient:
    def add_pull_request_label(
        self, *, repository: str, pull_request_number: int, label: str
    ) -> None:
        return None

    def update_pull_request_branch(
        self, *, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> None:
        return None


class _RunHistoryStore:
    def __init__(self, latest_run: MergeTrainRunRecord | None) -> None:
        self.latest_run = latest_run
        self.requests: list[tuple[str, str]] = []

    def latest_merge_train_run_record(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainRunRecord | None:
        self.requests.append((repository, base_branch))
        return self.latest_run


class MergeTrainAdmissionTests(unittest.TestCase):
    def test_admits_when_no_prior_run_exists(self) -> None:
        decision = evaluate_merge_train_admission(
            repository="cbusillo/sellyouroutboard",
            base_branch="main",
            requested_at="2026-05-09T02:10:00Z",
            latest_run=None,
        )

        self.assertTrue(decision.admitted)
        self.assertEqual(decision.reason_code, "no_prior_run")

    def test_dry_run_history_does_not_throttle_scheduler(self) -> None:
        latest_run = _run_record(recorded_at="2026-05-09T02:09:59Z")

        decision = evaluate_merge_train_admission(
            repository="cbusillo/sellyouroutboard",
            base_branch="main",
            requested_at="2026-05-09T02:10:00Z",
            latest_run=latest_run,
        )

        self.assertTrue(decision.admitted)
        self.assertEqual(decision.reason_code, "dry_run_history_only")
        self.assertEqual(decision.latest_run_id, latest_run.run_id)

    def test_reread_required_mutation_is_admitted_immediately(self) -> None:
        latest_run = _run_record(
            recorded_at="2026-05-09T02:09:59Z",
            mutation="merge",
        )

        decision = evaluate_merge_train_admission(
            repository="cbusillo/sellyouroutboard",
            base_branch="main",
            requested_at="2026-05-09T02:10:00Z",
            latest_run=latest_run,
        )

        self.assertTrue(decision.admitted)
        self.assertEqual(decision.reason_code, "reread_required")

    def test_poll_required_mutation_defers_until_poll_interval_elapsed(self) -> None:
        latest_run = _run_record(
            recorded_at="2026-05-09T02:00:00Z",
            mutation="wait",
        )

        pending_decision = evaluate_merge_train_admission(
            repository="cbusillo/sellyouroutboard",
            base_branch="main",
            requested_at="2026-05-09T02:00:30Z",
            latest_run=latest_run,
            poll_interval_seconds=60,
        )
        elapsed_decision = evaluate_merge_train_admission(
            repository="cbusillo/sellyouroutboard",
            base_branch="main",
            requested_at="2026-05-09T02:01:00Z",
            latest_run=latest_run,
            poll_interval_seconds=60,
        )

        self.assertFalse(pending_decision.admitted)
        self.assertEqual(pending_decision.reason_code, "poll_interval_pending")
        self.assertEqual(pending_decision.next_allowed_at, "2026-05-09T02:01:00Z")
        self.assertTrue(elapsed_decision.admitted)
        self.assertEqual(elapsed_decision.reason_code, "poll_interval_elapsed")

    def test_non_reread_mutation_defers_until_backoff_elapsed(self) -> None:
        latest_run = _run_record(
            recorded_at="2026-05-09T02:00:00Z",
            mutation="block",
        )

        pending_decision = evaluate_merge_train_admission(
            repository="cbusillo/sellyouroutboard",
            base_branch="main",
            requested_at="2026-05-09T02:01:00Z",
            latest_run=latest_run,
            backoff_seconds=300,
        )
        elapsed_decision = evaluate_merge_train_admission(
            repository="cbusillo/sellyouroutboard",
            base_branch="main",
            requested_at="2026-05-09T02:05:00Z",
            latest_run=latest_run,
            backoff_seconds=300,
        )

        self.assertFalse(pending_decision.admitted)
        self.assertEqual(pending_decision.reason_code, "backoff_pending")
        self.assertEqual(pending_decision.next_allowed_at, "2026-05-09T02:05:00Z")
        self.assertTrue(elapsed_decision.admitted)
        self.assertEqual(elapsed_decision.reason_code, "backoff_elapsed")

    def test_rejects_latest_run_for_different_repository_scope(self) -> None:
        latest_run = _run_record(recorded_at="2026-05-09T02:00:00Z")

        with self.assertRaisesRegex(ValueError, "scope does not match"):
            evaluate_merge_train_admission(
                repository="cbusillo/other",
                base_branch="main",
                requested_at="2026-05-09T02:00:00Z",
                latest_run=latest_run,
            )

    def test_reads_latest_run_from_store(self) -> None:
        latest_run = _run_record(recorded_at="2026-05-09T02:00:00Z")
        store = _RunHistoryStore(latest_run)

        decision = evaluate_merge_train_admission_from_store(
            store=store,
            repository="cbusillo/sellyouroutboard",
            base_branch="main",
            requested_at="2026-05-09T02:01:00Z",
        )

        self.assertTrue(decision.admitted)
        self.assertEqual(store.requests, [("cbusillo/sellyouroutboard", "main")])


def _run_record(
    *,
    recorded_at: str,
    mutation: str = "",
) -> MergeTrainRunRecord:
    policy = build_sellyouroutboard_main_merge_train_policy()
    pull_request = _pull_request(42)
    if mutation == "wait":
        pull_request = _pull_request(42, required_checks_status="pending")
    elif mutation == "block":
        pull_request = _pull_request(42, required_checks_status="fail")
    snapshot = MergeTrainDryRunSnapshot(
        repository="cbusillo/sellyouroutboard",
        base_branch="main",
        pull_requests=(pull_request,),
    )
    dry_run_result = build_merge_train_dry_run_result(policy=policy, snapshot=snapshot)
    worker_step_result = None
    if mutation:
        noop_client = _NoopClient()
        worker_step_result = run_merge_train_worker_step(
            policy=policy,
            snapshot=snapshot,
            clients=MergeTrainWorkerClients(
                label_client=noop_client,
                branch_client=noop_client,
                merge_client=_FakeMergeClient(),
            ),
        )
    return build_merge_train_run_record(
        recorded_at=recorded_at,
        trace_id="launchplane_req_admission_test",
        policy_sha256=policy.policy_sha256,
        snapshot=snapshot,
        dry_run_result=dry_run_result,
        worker_step_result=worker_step_result,
    )


def _pull_request(
    number: int,
    *,
    required_checks_status: MergeTrainCheckStatus = "pass",
) -> MergeTrainPullRequestSnapshot:
    return MergeTrainPullRequestSnapshot(
        number=number,
        url=f"https://github.com/cbusillo/sellyouroutboard/pull/{number}",
        title=f"Pull request {number}",
        created_at="2026-05-09T01:00:00Z",
        labels=("ready-to-merge",),
        actor_role="repo_owner",
        head_sha=f"head-{number}",
        base_sha="base-main",
        mergeable="mergeable",
        required_checks_status=required_checks_status,
    )


if __name__ == "__main__":
    unittest.main()
