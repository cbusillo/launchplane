import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from unittest.mock import patch

from click import Command
from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.merge_train_policy import MergeTrainPolicy
from control_plane.merge_train import (
    MergeTrainDryRunSnapshot,
    MergeTrainPullRequestSnapshot,
    apply_merge_train_branch_update_intent,
    apply_merge_train_block_intent,
    apply_merge_train_merge_intent,
    build_merge_train_dry_run_result,
    build_merge_train_wait_result,
    discover_merge_train_stack,
    reread_merge_train_after_branch_update,
)
from control_plane.merge_train_github import MergeTrainGitHubError
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_with_codex_skills


CLI_MAIN = cast(Command, main)


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
        self.merged_pull_requests.append((repository, pull_request_number, head_sha, merge_method))
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
    def test_cli_merge_train_dry_run_requires_policy_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            snapshot_file = Path(temp_dir) / "snapshot.json"
            snapshot_file.write_text(
                json.dumps(
                    {
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "pull_requests": [],
                    }
                ),
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "merge-train-dry-run",
                    "--snapshot-file",
                    str(snapshot_file),
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Missing option '--policy-file'", result.output)

    def test_dry_run_orders_oldest_eligible_ready_pull_requests_first(self) -> None:
        result = build_merge_train_dry_run_result(
            policy=build_test_merge_train_policy(),
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
                policy=build_test_merge_train_policy(),
                snapshot=MergeTrainDryRunSnapshot(
                    repository="cbusillo/other",
                    base_branch="main",
                    pull_requests=(_pull_request(1),),
                ),
            )

    def test_dry_run_excludes_missing_label_and_disallowed_actor_role(self) -> None:
        result = build_merge_train_dry_run_result(
            policy=build_test_merge_train_policy(),
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

    def test_dry_run_ignores_stack_children_that_do_not_target_base_branch(self) -> None:
        result = build_merge_train_dry_run_result(
            policy=build_test_merge_train_policy(),
            snapshot=MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(
                    _pull_request(8, head_ref="feature/root", base_ref="main"),
                    _pull_request(9, head_ref="feature/child", base_ref="feature/root"),
                ),
            ),
        )

        self.assertEqual(result.queue_order, (8,))
        self.assertEqual([entry.number for entry in result.queue], [8])

    def test_dry_run_preserves_legacy_snapshot_files_without_base_refs(self) -> None:
        result = build_merge_train_dry_run_result(
            policy=build_test_merge_train_policy(),
            snapshot=MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(
                    _pull_request(13, head_ref="", base_ref=""),
                    _pull_request(14, head_ref="", base_ref=""),
                ),
            ),
        )

        self.assertEqual(result.queue_order, (13, 14))

    def test_dry_run_reports_block_and_update_actions(self) -> None:
        blocked_result = build_merge_train_dry_run_result(
            policy=build_test_merge_train_policy(),
            snapshot=MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(_pull_request(6, mergeable="conflicting"),),
            ),
        )
        update_result = build_merge_train_dry_run_result(
            policy=build_test_merge_train_policy(),
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
            policy_file = _write_policy_file(Path(temp_dir))
            snapshot_file.write_text(
                json.dumps(
                    {
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "pull_requests": [
                            _pull_request(22, created_at="2026-05-08T22:00:00Z").model_dump(
                                mode="json"
                            )
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "merge-train-dry-run",
                    "--snapshot-file",
                    str(snapshot_file),
                    "--policy-file",
                    str(policy_file),
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
            TemporaryDirectory() as temp_dir,
            patch(
                "control_plane.cli.UrllibMergeTrainGitHubTransport",
                return_value=object(),
            ) as transport_class,
            patch(
                "control_plane.cli.GitHubMergeTrainSnapshotReader",
                return_value=snapshot_reader,
            ) as reader_class,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            policy_file = _write_policy_file(Path(temp_dir))
            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "merge-train-run-once",
                    "--policy-file",
                    str(policy_file),
                    "--repository",
                    "cbusillo/sellyouroutboard",
                ],
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
            TemporaryDirectory() as temp_dir,
            patch(
                "control_plane.cli.UrllibMergeTrainGitHubTransport",
                return_value=object(),
            ),
            patch(
                "control_plane.cli.GitHubMergeTrainSnapshotReader",
                return_value=snapshot_reader,
            ),
            patch("control_plane.cli.GitHubMergeTrainClient", _FakeGitHubClient),
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            policy_file = _write_policy_file(Path(temp_dir))
            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "merge-train-run-once",
                    "--policy-file",
                    str(policy_file),
                    "--repository",
                    "cbusillo/sellyouroutboard",
                    "--mutate",
                ],
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
        with TemporaryDirectory() as temp_dir, patch.dict("os.environ", {}, clear=True):
            policy_file = _write_policy_file(Path(temp_dir))
            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "merge-train-run-once",
                    "--policy-file",
                    str(policy_file),
                    "--repository",
                    "cbusillo/sellyouroutboard",
                ],
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
            TemporaryDirectory() as temp_dir,
            patch(
                "control_plane.cli.UrllibMergeTrainGitHubTransport",
                return_value=object(),
            ),
            patch(
                "control_plane.cli.GitHubMergeTrainSnapshotReader",
                return_value=snapshot_reader,
            ),
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            policy_file = _write_policy_file(Path(temp_dir))
            result = CliRunner().invoke(
                CLI_MAIN,
                ["work-graph", "merge-train-run-once", "--policy-file", str(policy_file)],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("GitHub merge train request failed", result.output)
        self.assertIn("rate limited", result.output)
        self.assertIn("HTTP 403", result.output)


class MergeTrainStackDiscoveryTests(unittest.TestCase):
    def test_discovers_same_repo_linear_stack_from_root_to_leaf(self) -> None:
        snapshot = MergeTrainDryRunSnapshot(
            repository="example/merge-train-repo",
            base_branch="main",
            pull_requests=(
                _pull_request(
                    10,
                    head_ref="feature/root",
                    base_ref="main",
                    repository="example/merge-train-repo",
                ),
                _pull_request(
                    11,
                    head_ref="feature/middle",
                    base_ref="feature/root",
                    repository="example/merge-train-repo",
                ),
                _pull_request(
                    12,
                    head_ref="feature/leaf",
                    base_ref="feature/middle",
                    repository="example/merge-train-repo",
                ),
            ),
        )

        result = discover_merge_train_stack(snapshot=snapshot, root_pull_request_number=10)

        self.assertEqual(result.status, "ready_for_collapse")
        self.assertEqual(result.stack_order, (10, 11, 12))
        self.assertEqual(
            [entry.head_ref for entry in result.entries],
            ["feature/root", "feature/middle", "feature/leaf"],
        )

    def test_reports_not_stacked_for_single_root_pr(self) -> None:
        snapshot = MergeTrainDryRunSnapshot(
            repository="example/merge-train-repo",
            base_branch="main",
            pull_requests=(
                _pull_request(
                    20,
                    head_ref="feature/root",
                    base_ref="main",
                    repository="example/merge-train-repo",
                ),
            ),
        )

        result = discover_merge_train_stack(snapshot=snapshot, root_pull_request_number=20)

        self.assertEqual(result.status, "not_stacked")
        self.assertEqual(result.stack_order, (20,))
        self.assertEqual(result.unsupported_reasons, ())

    def test_rejects_ambiguous_sibling_stack_children(self) -> None:
        snapshot = MergeTrainDryRunSnapshot(
            repository="example/merge-train-repo",
            base_branch="main",
            pull_requests=(
                _pull_request(
                    30,
                    head_ref="feature/root",
                    base_ref="main",
                    repository="example/merge-train-repo",
                ),
                _pull_request(
                    31,
                    head_ref="feature/child-a",
                    base_ref="feature/root",
                    repository="example/merge-train-repo",
                ),
                _pull_request(
                    32,
                    head_ref="feature/child-b",
                    base_ref="feature/root",
                    repository="example/merge-train-repo",
                ),
            ),
        )

        result = discover_merge_train_stack(snapshot=snapshot, root_pull_request_number=30)

        self.assertEqual(result.status, "unsupported")
        self.assertIn("multiple stacked child", result.unsupported_reasons[0])

    def test_rejects_forked_stack_members(self) -> None:
        snapshot = MergeTrainDryRunSnapshot(
            repository="example/merge-train-repo",
            base_branch="main",
            pull_requests=(
                _pull_request(
                    40,
                    head_ref="feature/root",
                    base_ref="main",
                    repository="example/merge-train-repo",
                ),
                _pull_request(
                    41,
                    head_ref="feature/child",
                    base_ref="feature/root",
                    repository="example/merge-train-repo",
                    head_repository="fork/merge-train-repo",
                ),
            ),
        )

        result = discover_merge_train_stack(snapshot=snapshot, root_pull_request_number=40)

        self.assertEqual(result.status, "unsupported")
        self.assertIn("not from the train repository", result.unsupported_reasons[0])


class MergeTrainBlockIntentTests(unittest.TestCase):
    def test_block_intent_applies_blocked_label_and_pauses_train(self) -> None:
        dry_run_result = build_merge_train_dry_run_result(
            policy=build_test_merge_train_policy(),
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
            policy=build_test_merge_train_policy(),
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
            policy=build_test_merge_train_policy(),
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
        base_policy = build_test_merge_train_policy()
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
            policy=build_test_merge_train_policy(),
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
            policy=build_test_merge_train_policy(),
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
        policy = build_test_merge_train_policy()
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
        policy = build_test_merge_train_policy()
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
            policy=build_test_merge_train_policy(),
            snapshot=MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(_pull_request(61, required_checks_status="pending"),),
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
            policy=build_test_merge_train_policy(),
            snapshot=MergeTrainDryRunSnapshot(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pull_requests=(_pull_request(62, mergeable="unknown"),),
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
            policy=build_test_merge_train_policy(),
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
            policy=build_test_merge_train_policy(),
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
            policy=build_test_merge_train_policy(),
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
    head_ref: str = "",
    base_ref: str = "main",
    repository: str = "",
    head_repository: str = "",
    base_repository: str = "",
) -> MergeTrainPullRequestSnapshot:
    normalized_head_repository = head_repository or repository
    normalized_base_repository = base_repository or repository
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
            "head_ref": head_ref,
            "head_repository": normalized_head_repository,
            "base_sha": "base-main",
            "base_ref": base_ref,
            "base_repository": normalized_base_repository,
            "mergeable": mergeable,
            "required_checks_status": required_checks_status,
            "branch_update_required": branch_update_required,
        }
    )


def _write_policy_file(directory: Path) -> Path:
    policy_file = directory / "merge-train-policy.toml"
    policy = build_test_merge_train_policy_with_codex_skills()
    tables = []
    for repository_policy in policy.policies:
        tables.append(
            f"""[[policies]]
repository = "{repository_policy.repository}"
base_branch = "{repository_policy.base_branch}"
enqueue_label = "{repository_policy.enqueue_label}"
blocked_label = "{repository_policy.blocked_label}"
merge_method = "{repository_policy.merge_method}"
failure_policy = "{repository_policy.failure_policy}"
[policies.enqueue]
label_required = true
allowed_actor_roles = ["repo_owner", "repo_admin"]
[policies.merge_identity]
kind = "{repository_policy.merge_identity.kind}"
name = "{repository_policy.merge_identity.name}"
[policies.service_authz]
action = "{repository_policy.service_authz.action}"
product = "{repository_policy.service_authz.product}"
context = "{repository_policy.service_authz.context}"
[policies.github_token]
env_var = "{repository_policy.github_token.env_var}"
"""
        )
    policy_file.write_text("\n\n".join(("schema_version = 1", *tables)), encoding="utf-8")
    return policy_file


if __name__ == "__main__":
    unittest.main()
