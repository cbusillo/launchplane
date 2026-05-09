import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.merge_train_policy import (
    MergeTrainPolicy,
    build_sellyouroutboard_main_merge_train_policy,
)
from control_plane.merge_train import (
    MergeTrainDryRunSnapshot,
    MergeTrainPullRequestSnapshot,
    apply_merge_train_branch_update_intent,
    apply_merge_train_block_intent,
    apply_merge_train_merge_intent,
    build_merge_train_dry_run_result,
    build_merge_train_wait_result,
    reread_merge_train_after_branch_update,
)
from control_plane.merge_train_github import MergeTrainGitHubError


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
    def __init__(self) -> None:
        self.merged_pull_requests: list[tuple[str, int, str, str]] = []

    def merge_pull_request(
        self,
        *,
        repository: str,
        pull_request_number: int,
        head_sha: str,
        merge_method: str,
    ) -> str:
        self.merged_pull_requests.append(
            (repository, pull_request_number, head_sha, merge_method)
        )
        return f"merge-{pull_request_number}"


class _FakeSnapshotReader:
    def __init__(
        self,
        snapshot: MergeTrainDryRunSnapshot,
        error: Exception | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.error = error
        self.reads: list[tuple[str, str]] = []

    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainDryRunSnapshot:
        self.reads.append((repository, base_branch))
        if self.error is not None:
            raise self.error
        return self.snapshot


class MergeTrainDryRunTests(unittest.TestCase):
    def test_dry_run_orders_oldest_eligible_ready_pull_requests_first(self) -> None:
        result = build_merge_train_dry_run_result(
            policy=build_sellyouroutboard_main_merge_train_policy(),
            snapshot=MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(
                    _pull_request(12, created_at="2026-05-08T12:00:00Z"),
                    _pull_request(10, created_at="2026-05-08T10:00:00Z"),
                    _pull_request(11, created_at="2026-05-08T11:00:00Z", is_draft=True),
                ),
            ),
        )

        self.assertEqual(result.queue_order, (10, 12))
        self.assertEqual(result.selected_pr.number if result.selected_pr else None, 10)
        self.assertEqual(result.intended_next_action, "merge")
        self.assertEqual(result.merge_method, "merge")

    def test_dry_run_fails_closed_when_policy_is_missing(self) -> None:
        with self.assertRaisesRegex(ValueError, "policy not found"):
            build_merge_train_dry_run_result(
                policy=build_sellyouroutboard_main_merge_train_policy(),
                snapshot=MergeTrainDryRunSnapshot(
                    repository="cbusillo/other",
                    base_branch="main",
                    pull_requests=(_pull_request(1),),
                ),
            )

    def test_dry_run_excludes_missing_label_and_disallowed_actor_role(self) -> None:
        result = build_merge_train_dry_run_result(
            policy=build_sellyouroutboard_main_merge_train_policy(),
            snapshot=MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(
                    _pull_request(4, labels=()),
                    _pull_request(5, actor_role="external_contributor"),
                ),
            ),
        )

        self.assertEqual(result.queue_order, ())
        self.assertIsNone(result.selected_pr)
        self.assertEqual(result.intended_next_action, "idle")
        self.assertIn("missing ready-to-merge label", result.queue[0].ineligible_reasons)
        self.assertIn("actor role is not allowed", result.queue[1].ineligible_reasons[0])

    def test_dry_run_reports_block_and_update_actions(self) -> None:
        blocked_result = build_merge_train_dry_run_result(
            policy=build_sellyouroutboard_main_merge_train_policy(),
            snapshot=MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(_pull_request(6, mergeable="conflicting"),),
            ),
        )
        update_result = build_merge_train_dry_run_result(
            policy=build_sellyouroutboard_main_merge_train_policy(),
            snapshot=MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(_pull_request(7, branch_update_required=True),),
            ),
        )

        self.assertEqual(blocked_result.intended_next_action, "block")
        self.assertIn("merge-blocked", blocked_result.next_action_detail)
        self.assertEqual(update_result.intended_next_action, "update_branch")

    def test_cli_renders_merge_train_dry_run_snapshot(self) -> None:
        with TemporaryDirectory() as temp_dir:
            snapshot_file = Path(temp_dir) / "snapshot.json"
            snapshot_file.write_text(
                json.dumps(
                    {
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "pull_requests": [
                            _pull_request(22, created_at="2026-05-08T22:00:00Z")
                            .model_dump(mode="json")
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                main,
                [
                    "work-graph",
                    "merge-train-dry-run",
                    "--snapshot-file",
                    str(snapshot_file),
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["queue_order"], [22])
        self.assertEqual(payload["selected_pr"]["number"], 22)

    def test_cli_merge_train_run_once_reads_live_snapshot_without_mutation(self) -> None:
        snapshot_reader = _FakeSnapshotReader(
            MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(_pull_request(23),),
            )
        )

        with (
            patch(
                "control_plane.cli.UrllibMergeTrainGitHubTransport",
                return_value=object(),
            ) as transport_class,
            patch(
                "control_plane.cli.GitHubMergeTrainSnapshotReader",
                return_value=snapshot_reader,
            ) as reader_class,
            patch.dict("os.environ", {"GITHUB_TOKEN": "token"}, clear=True),
        ):
            result = CliRunner().invoke(
                main,
                ["work-graph", "merge-train-run-once"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["repository"], "cbusillo/sellyouroutboard")
        self.assertEqual(payload["base_branch"], "main")
        self.assertEqual(payload["dry_run_result"]["intended_next_action"], "merge")
        self.assertEqual(snapshot_reader.reads, [("cbusillo/sellyouroutboard", "main")])
        transport_class.assert_called_once_with(
            token="token", api_base_url="https://api.github.com"
        )
        reader_class.assert_called_once()

    def test_cli_merge_train_run_once_mutates_one_worker_step_when_requested(self) -> None:
        snapshot_reader = _FakeSnapshotReader(
            MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(_pull_request(24, required_checks_status="fail"),),
            )
        )
        label_client = _FakeLabelClient()

        class _FakeGitHubClient:
            def __init__(self, *, transport: object) -> None:
                self.transport = transport

            def add_pull_request_label(
                self, *, repository: str, pull_request_number: int, label: str
            ) -> None:
                label_client.add_pull_request_label(
                    repository=repository,
                    pull_request_number=pull_request_number,
                    label=label,
                )

            def update_pull_request_branch(
                self, *, repository: str, pull_request_number: int, expected_head_sha: str
            ) -> None:
                raise AssertionError("block path should not update branches")

            def merge_pull_request(
                self,
                *,
                repository: str,
                pull_request_number: int,
                head_sha: str,
                merge_method: str,
            ) -> str:
                raise AssertionError("block path should not merge")

        with (
            patch(
                "control_plane.cli.UrllibMergeTrainGitHubTransport",
                return_value=object(),
            ),
            patch(
                "control_plane.cli.GitHubMergeTrainSnapshotReader",
                return_value=snapshot_reader,
            ),
            patch("control_plane.cli.GitHubMergeTrainClient", _FakeGitHubClient),
            patch.dict("os.environ", {"GITHUB_TOKEN": "token"}, clear=True),
        ):
            result = CliRunner().invoke(
                main,
                ["work-graph", "merge-train-run-once", "--mutate"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["mode"], "mutate")
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["block_result"]["pull_request_number"], 24)
        self.assertEqual(
            label_client.applied_labels,
            [("cbusillo/sellyouroutboard", 24, "merge-blocked")],
        )

    def test_cli_merge_train_run_once_requires_github_token(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            result = CliRunner().invoke(
                main,
                ["work-graph", "merge-train-run-once"],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Missing GitHub token", result.output)

    def test_cli_merge_train_run_once_reports_github_transport_error(self) -> None:
        snapshot_reader = _FakeSnapshotReader(
            MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(),
            ),
            error=MergeTrainGitHubError("rate limited", status_code=403),
        )

        with (
            patch(
                "control_plane.cli.UrllibMergeTrainGitHubTransport",
                return_value=object(),
            ),
            patch(
                "control_plane.cli.GitHubMergeTrainSnapshotReader",
                return_value=snapshot_reader,
            ),
            patch.dict("os.environ", {"GITHUB_TOKEN": "token"}, clear=True),
        ):
            result = CliRunner().invoke(
                main,
                ["work-graph", "merge-train-run-once"],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("GitHub merge train request failed", result.output)
        self.assertIn("rate limited", result.output)
        self.assertIn("HTTP 403", result.output)


class MergeTrainBlockIntentTests(unittest.TestCase):
    def test_block_intent_applies_blocked_label_and_pauses_train(self) -> None:
        dry_run_result = build_merge_train_dry_run_result(
            policy=build_sellyouroutboard_main_merge_train_policy(),
            snapshot=MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(_pull_request(31, required_checks_status="fail"),),
            ),
        )
        label_client = _FakeLabelClient()

        result = apply_merge_train_block_intent(
            dry_run_result=dry_run_result, label_client=label_client
        )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.pull_request_number, 31)
        self.assertEqual(result.blocked_label, "merge-blocked")
        self.assertFalse(result.train_should_continue)
        self.assertEqual(
            label_client.applied_labels,
            [("cbusillo/sellyouroutboard", 31, "merge-blocked")],
        )

    def test_block_intent_skips_non_block_actions_without_mutation(self) -> None:
        dry_run_result = build_merge_train_dry_run_result(
            policy=build_sellyouroutboard_main_merge_train_policy(),
            snapshot=MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(_pull_request(32),),
            ),
        )
        label_client = _FakeLabelClient()

        result = apply_merge_train_block_intent(
            dry_run_result=dry_run_result, label_client=label_client
        )

        self.assertEqual(result.status, "skipped")
        self.assertTrue(result.train_should_continue)
        self.assertEqual(label_client.applied_labels, [])

    def test_block_intent_is_idempotent_when_blocked_label_exists(self) -> None:
        dry_run_result = build_merge_train_dry_run_result(
            policy=build_sellyouroutboard_main_merge_train_policy(),
            snapshot=MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(
                    _pull_request(
                        34,
                        labels=("ready-to-merge", "merge-blocked"),
                        required_checks_status="fail",
                    ),
                ),
            ),
        )
        label_client = _FakeLabelClient()

        result = apply_merge_train_block_intent(
            dry_run_result=dry_run_result, label_client=label_client
        )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.pull_request_number, 34)
        self.assertEqual(label_client.applied_labels, [])

    def test_block_intent_can_continue_train_when_policy_allows_it(self) -> None:
        base_policy = build_sellyouroutboard_main_merge_train_policy()
        repository_policy = base_policy.policies[0].model_copy(
            update={"failure_policy": "continue_after_blocking_pr"}
        )
        dry_run_result = build_merge_train_dry_run_result(
            policy=MergeTrainPolicy(policies=(repository_policy,)),
            snapshot=MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(_pull_request(33, mergeable="conflicting"),),
            ),
        )

        result = apply_merge_train_block_intent(
            dry_run_result=dry_run_result, label_client=_FakeLabelClient()
        )

        self.assertEqual(result.status, "blocked")
        self.assertTrue(result.train_should_continue)


class MergeTrainBranchUpdateIntentTests(unittest.TestCase):
    def test_branch_update_intent_updates_selected_pr_and_requires_reread(self) -> None:
        dry_run_result = build_merge_train_dry_run_result(
            policy=build_sellyouroutboard_main_merge_train_policy(),
            snapshot=MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(_pull_request(41, branch_update_required=True),),
            ),
        )
        branch_client = _FakeBranchClient()

        result = apply_merge_train_branch_update_intent(
            dry_run_result=dry_run_result, branch_client=branch_client
        )

        self.assertEqual(result.status, "updated")
        self.assertEqual(result.pull_request_number, 41)
        self.assertEqual(result.expected_head_sha, "head-41")
        self.assertTrue(result.reread_required)
        self.assertEqual(
            branch_client.updated_branches,
            [("cbusillo/sellyouroutboard", 41, "head-41")],
        )

    def test_branch_update_intent_skips_other_actions_without_mutation(self) -> None:
        dry_run_result = build_merge_train_dry_run_result(
            policy=build_sellyouroutboard_main_merge_train_policy(),
            snapshot=MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(_pull_request(42),),
            ),
        )
        branch_client = _FakeBranchClient()

        result = apply_merge_train_branch_update_intent(
            dry_run_result=dry_run_result, branch_client=branch_client
        )

        self.assertEqual(result.status, "skipped")
        self.assertFalse(result.reread_required)
        self.assertEqual(branch_client.updated_branches, [])


class MergeTrainRereadTests(unittest.TestCase):
    def test_reread_after_branch_update_re_evaluates_fresh_snapshot(self) -> None:
        policy = build_sellyouroutboard_main_merge_train_policy()
        stale_result = build_merge_train_dry_run_result(
            policy=policy,
            snapshot=MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(_pull_request(51, branch_update_required=True),),
            ),
        )
        branch_update_result = apply_merge_train_branch_update_intent(
            dry_run_result=stale_result, branch_client=_FakeBranchClient()
        )
        snapshot_reader = _FakeSnapshotReader(
            MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(
                    _pull_request(
                        51,
                        created_at="2026-05-08T10:00:00Z",
                        required_checks_status="pending",
                    ),
                ),
            )
        )

        result = reread_merge_train_after_branch_update(
            branch_update_result=branch_update_result,
            policy=policy,
            snapshot_reader=snapshot_reader,
        )

        self.assertEqual(result.status, "reread")
        self.assertEqual(snapshot_reader.reads, [("cbusillo/sellyouroutboard", "main")])
        self.assertIsNotNone(result.refreshed_result)
        refreshed_result = result.refreshed_result
        assert refreshed_result is not None
        self.assertEqual(refreshed_result.intended_next_action, "wait_for_checks")

    def test_reread_skips_when_branch_update_was_not_required(self) -> None:
        policy = build_sellyouroutboard_main_merge_train_policy()
        dry_run_result = build_merge_train_dry_run_result(
            policy=policy,
            snapshot=MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(_pull_request(52),),
            ),
        )
        branch_update_result = apply_merge_train_branch_update_intent(
            dry_run_result=dry_run_result, branch_client=_FakeBranchClient()
        )
        snapshot_reader = _FakeSnapshotReader(
            MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(),
            )
        )

        result = reread_merge_train_after_branch_update(
            branch_update_result=branch_update_result,
            policy=policy,
            snapshot_reader=snapshot_reader,
        )

        self.assertEqual(result.status, "skipped")
        self.assertIsNone(result.refreshed_result)
        self.assertEqual(snapshot_reader.reads, [])


class MergeTrainWaitTests(unittest.TestCase):
    def test_wait_result_records_selected_pr_and_head_sha_for_pending_checks(
        self,
    ) -> None:
        dry_run_result = build_merge_train_dry_run_result(
            policy=build_sellyouroutboard_main_merge_train_policy(),
            snapshot=MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(
                    _pull_request(61, required_checks_status="pending"),
                ),
            ),
        )

        result = build_merge_train_wait_result(dry_run_result=dry_run_result)

        self.assertEqual(result.status, "waiting")
        self.assertEqual(result.pull_request_number, 61)
        self.assertEqual(result.head_sha, "head-61")
        self.assertEqual(result.mergeable, "mergeable")
        self.assertEqual(result.required_checks_status, "pending")
        self.assertTrue(result.poll_required)
        self.assertIn("Wait for mergeability", result.detail)

    def test_wait_result_records_unknown_mergeability(self) -> None:
        dry_run_result = build_merge_train_dry_run_result(
            policy=build_sellyouroutboard_main_merge_train_policy(),
            snapshot=MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(
                    _pull_request(62, mergeable="unknown"),
                ),
            ),
        )

        result = build_merge_train_wait_result(dry_run_result=dry_run_result)

        self.assertEqual(result.status, "waiting")
        self.assertEqual(result.pull_request_number, 62)
        self.assertEqual(result.mergeable, "unknown")
        self.assertEqual(result.required_checks_status, "pass")
        self.assertTrue(result.poll_required)

    def test_wait_result_skips_non_wait_actions(self) -> None:
        dry_run_result = build_merge_train_dry_run_result(
            policy=build_sellyouroutboard_main_merge_train_policy(),
            snapshot=MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(_pull_request(63),),
            ),
        )

        result = build_merge_train_wait_result(dry_run_result=dry_run_result)

        self.assertEqual(result.status, "skipped")
        self.assertIsNone(result.pull_request_number)
        self.assertEqual(result.head_sha, "")
        self.assertIsNone(result.mergeable)
        self.assertIsNone(result.required_checks_status)
        self.assertFalse(result.poll_required)


class MergeTrainMergeIntentTests(unittest.TestCase):
    def test_merge_intent_uses_selected_head_sha_and_policy_method(self) -> None:
        dry_run_result = build_merge_train_dry_run_result(
            policy=build_sellyouroutboard_main_merge_train_policy(),
            snapshot=MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(_pull_request(71),),
            ),
        )
        merge_client = _FakeMergeClient()

        result = apply_merge_train_merge_intent(
            dry_run_result=dry_run_result, merge_client=merge_client
        )

        self.assertEqual(result.status, "merged")
        self.assertEqual(result.pull_request_number, 71)
        self.assertEqual(result.head_sha, "head-71")
        self.assertEqual(result.merge_method, "merge")
        self.assertEqual(result.merge_commit_sha, "merge-71")
        self.assertTrue(result.reread_required)
        self.assertEqual(
            merge_client.merged_pull_requests,
            [("cbusillo/sellyouroutboard", 71, "head-71", "merge")],
        )

    def test_merge_intent_skips_non_merge_actions_without_mutation(self) -> None:
        dry_run_result = build_merge_train_dry_run_result(
            policy=build_sellyouroutboard_main_merge_train_policy(),
            snapshot=MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(_pull_request(72, required_checks_status="pending"),),
            ),
        )
        merge_client = _FakeMergeClient()

        result = apply_merge_train_merge_intent(
            dry_run_result=dry_run_result, merge_client=merge_client
        )

        self.assertEqual(result.status, "skipped")
        self.assertIsNone(result.pull_request_number)
        self.assertEqual(result.head_sha, "")
        self.assertIsNone(result.merge_method)
        self.assertEqual(result.merge_commit_sha, "")
        self.assertFalse(result.reread_required)
        self.assertEqual(merge_client.merged_pull_requests, [])


def _pull_request(
    number: int,
    *,
    created_at: str = "2026-05-08T10:00:00Z",
    labels: tuple[str, ...] = ("ready-to-merge",),
    actor_role: str = "repo_admin",
    is_draft: bool = False,
    mergeable: str = "mergeable",
    required_checks_status: str = "pass",
    branch_update_required: bool = False,
) -> MergeTrainPullRequestSnapshot:
    return MergeTrainPullRequestSnapshot.model_validate(
        {
            "number": number,
            "url": f"https://github.com/cbusillo/sellyouroutboard/pull/{number}",
            "title": f"PR {number}",
            "created_at": created_at,
            "labels": labels,
            "actor_role": actor_role,
            "is_draft": is_draft,
            "head_sha": f"head-{number}",
            "base_sha": "base-main",
            "mergeable": mergeable,
            "required_checks_status": required_checks_status,
            "branch_update_required": branch_update_required,
        }
    )


if __name__ == "__main__":
    unittest.main()
