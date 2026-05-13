import unittest

from control_plane.merge_train import MergeTrainCheckStatus
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.merge_train import MergeTrainMergeableState
from control_plane.merge_train import MergeTrainPullRequestSnapshot
from control_plane.merge_train_github import MergeTrainGitHubStaleHeadError
from control_plane.workflows.merge_train_worker import MergeTrainWorkerClients
from control_plane.workflows.merge_train_worker import run_merge_train_worker_step
from tests.merge_train_policy_fixtures import build_test_merge_train_policy


class _FakeLabelClient:
    def __init__(self) -> None:
        self.applied_labels: list[tuple[str, int, str]] = []

    def add_pull_request_label(
        self, *, repository: str, pull_request_number: int, label: str
    ) -> None:
        self.applied_labels.append((repository, pull_request_number, label))


class _FakeBranchClient:
    def __init__(self) -> None:
        self.updated_branches: list[tuple[str, int, str]] = []

    def update_pull_request_branch(
        self, *, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> None:
        self.updated_branches.append((repository, pull_request_number, expected_head_sha))


class _FakeMergeClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.merged_pull_requests: list[tuple[str, int, str, str]] = []

    def merge_pull_request(
        self,
        *,
        repository: str,
        pull_request_number: int,
        head_sha: str,
        merge_method: str,
    ) -> str:
        self.merged_pull_requests.append((repository, pull_request_number, head_sha, merge_method))
        if self.error is not None:
            raise self.error
        return f"merge-{pull_request_number}"


class MergeTrainWorkerStepTests(unittest.TestCase):
    def test_worker_step_applies_block_label_once(self) -> None:
        label_client = _FakeLabelClient()
        clients = _clients(label_client=label_client)

        result = run_merge_train_worker_step(
            policy=build_test_merge_train_policy(),
            snapshot=_snapshot(_pull_request(11, required_checks_status="fail")),
            clients=clients,
        )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.intended_next_action, "block")
        self.assertEqual(result.selected_pr_number, 11)
        self.assertFalse(result.reread_required)
        self.assertFalse(result.poll_required)
        self.assertIsNotNone(result.block_result)
        self.assertEqual(
            label_client.applied_labels, [("cbusillo/sellyouroutboard", 11, "merge-blocked")]
        )

    def test_worker_step_does_not_reapply_existing_block_label(self) -> None:
        label_client = _FakeLabelClient()

        result = run_merge_train_worker_step(
            policy=build_test_merge_train_policy(),
            snapshot=_snapshot(
                _pull_request(
                    12,
                    labels=("ready-to-merge", "merge-blocked"),
                    mergeable="conflicting",
                )
            ),
            clients=_clients(label_client=label_client),
        )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(label_client.applied_labels, [])

    def test_worker_step_updates_branch_once_and_requires_reread(self) -> None:
        branch_client = _FakeBranchClient()

        result = run_merge_train_worker_step(
            policy=build_test_merge_train_policy(),
            snapshot=_snapshot(_pull_request(21, branch_update_required=True)),
            clients=_clients(branch_client=branch_client),
        )

        self.assertEqual(result.status, "branch_updated")
        self.assertEqual(result.intended_next_action, "update_branch")
        self.assertTrue(result.reread_required)
        self.assertFalse(result.poll_required)
        self.assertIsNotNone(result.branch_update_result)
        self.assertEqual(
            branch_client.updated_branches, [("cbusillo/sellyouroutboard", 21, "head-21")]
        )

    def test_worker_step_waits_without_mutating_github(self) -> None:
        label_client = _FakeLabelClient()
        branch_client = _FakeBranchClient()
        merge_client = _FakeMergeClient()

        result = run_merge_train_worker_step(
            policy=build_test_merge_train_policy(),
            snapshot=_snapshot(_pull_request(31, required_checks_status="pending")),
            clients=MergeTrainWorkerClients(
                label_client=label_client,
                branch_client=branch_client,
                merge_client=merge_client,
            ),
        )

        self.assertEqual(result.status, "waiting")
        self.assertEqual(result.intended_next_action, "wait_for_checks")
        self.assertFalse(result.reread_required)
        self.assertTrue(result.poll_required)
        self.assertIsNotNone(result.wait_result)
        self.assertEqual(label_client.applied_labels, [])
        self.assertEqual(branch_client.updated_branches, [])
        self.assertEqual(merge_client.merged_pull_requests, [])

    def test_worker_step_merges_once_and_requires_reread(self) -> None:
        merge_client = _FakeMergeClient()

        result = run_merge_train_worker_step(
            policy=build_test_merge_train_policy(),
            snapshot=_snapshot(_pull_request(41)),
            clients=_clients(merge_client=merge_client),
        )

        self.assertEqual(result.status, "merged")
        self.assertEqual(result.intended_next_action, "merge")
        self.assertTrue(result.reread_required)
        self.assertFalse(result.poll_required)
        self.assertIsNotNone(result.merge_result)
        self.assertEqual(
            merge_client.merged_pull_requests,
            [("cbusillo/sellyouroutboard", 41, "head-41", "merge")],
        )

    def test_worker_step_treats_guarded_merge_conflict_as_stale_head(self) -> None:
        merge_client = _FakeMergeClient(
            MergeTrainGitHubStaleHeadError("GitHub merge rejected stale head", status_code=409)
        )

        result = run_merge_train_worker_step(
            policy=build_test_merge_train_policy(),
            snapshot=_snapshot(_pull_request(42)),
            clients=_clients(merge_client=merge_client),
        )

        self.assertEqual(result.status, "stale_head")
        self.assertEqual(result.intended_next_action, "merge")
        self.assertTrue(result.reread_required)
        self.assertFalse(result.poll_required)
        self.assertIsNone(result.merge_result)
        self.assertEqual(
            merge_client.merged_pull_requests,
            [("cbusillo/sellyouroutboard", 42, "head-42", "merge")],
        )

    def test_worker_step_idles_without_mutating_github(self) -> None:
        label_client = _FakeLabelClient()
        branch_client = _FakeBranchClient()
        merge_client = _FakeMergeClient()

        result = run_merge_train_worker_step(
            policy=build_test_merge_train_policy(),
            snapshot=_snapshot(_pull_request(51, labels=())),
            clients=MergeTrainWorkerClients(
                label_client=label_client,
                branch_client=branch_client,
                merge_client=merge_client,
            ),
        )

        self.assertEqual(result.status, "idle")
        self.assertEqual(result.intended_next_action, "idle")
        self.assertIsNone(result.selected_pr_number)
        self.assertFalse(result.reread_required)
        self.assertFalse(result.poll_required)
        self.assertEqual(label_client.applied_labels, [])
        self.assertEqual(branch_client.updated_branches, [])
        self.assertEqual(merge_client.merged_pull_requests, [])

    def test_worker_step_fails_closed_when_policy_is_missing(self) -> None:
        with self.assertRaisesRegex(ValueError, "policy not found"):
            run_merge_train_worker_step(
                policy=build_test_merge_train_policy(),
                snapshot=MergeTrainDryRunSnapshot(
                    repository="cbusillo/other",
                    base_branch="main",
                    pull_requests=(_pull_request(61),),
                ),
                clients=_clients(),
            )


def _clients(
    *,
    label_client: _FakeLabelClient | None = None,
    branch_client: _FakeBranchClient | None = None,
    merge_client: _FakeMergeClient | None = None,
) -> MergeTrainWorkerClients:
    return MergeTrainWorkerClients(
        label_client=label_client or _FakeLabelClient(),
        branch_client=branch_client or _FakeBranchClient(),
        merge_client=merge_client or _FakeMergeClient(),
    )


def _snapshot(*pull_requests: MergeTrainPullRequestSnapshot) -> MergeTrainDryRunSnapshot:
    return MergeTrainDryRunSnapshot(
        repository="cbusillo/sellyouroutboard",
        base_branch="main",
        pull_requests=pull_requests,
    )


def _pull_request(
    number: int,
    *,
    labels: tuple[str, ...] = ("ready-to-merge",),
    mergeable: MergeTrainMergeableState = "mergeable",
    required_checks_status: MergeTrainCheckStatus = "pass",
    branch_update_required: bool = False,
) -> MergeTrainPullRequestSnapshot:
    return MergeTrainPullRequestSnapshot(
        number=number,
        url=f"https://github.com/cbusillo/sellyouroutboard/pull/{number}",
        title=f"Pull request {number}",
        created_at=f"2026-05-08T10:{number:02d}:00Z",
        labels=labels,
        actor_role="repo_owner",
        head_sha=f"head-{number}",
        base_sha="base-sha",
        mergeable=mergeable,
        required_checks_status=required_checks_status,
        branch_update_required=branch_update_required,
    )
