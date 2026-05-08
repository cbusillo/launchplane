import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.merge_train_policy import (
    build_sellyouroutboard_main_merge_train_policy,
)
from control_plane.merge_train import (
    MergeTrainDryRunSnapshot,
    MergeTrainPullRequestSnapshot,
    build_merge_train_dry_run_result,
)


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
