import unittest

import click
from pathlib import Path
from unittest.mock import patch

from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.preview_inventory_scan_record import PreviewInventoryScanRecord
from control_plane.workflows.preview_lifecycle import build_preview_lifecycle_plan
from control_plane.workflows.preview_desired_state import (
    discover_github_preview_desired_state,
    list_github_open_pull_requests,
)


class PreviewDesiredStateTests(unittest.TestCase):
    def test_discovers_open_pull_requests_as_desired_previews(self) -> None:
        with (
            patch(
                "control_plane.workflows.preview_desired_state.resolve_launchplane_github_token",
                return_value="token",
            ),
            patch(
                "control_plane.workflows.preview_desired_state.list_github_open_pull_requests",
                return_value=(
                    {
                        "number": 42,
                        "html_url": "https://github.com/every/verireel/pull/42",
                        "head_sha": "abc1234",
                    },
                ),
            ) as list_mock,
        ):
            record = discover_github_preview_desired_state(
                control_plane_root=Path("/tmp/launchplane"),
                product="verireel",
                context="verireel-testing",
                source="launchplane-preview-lifecycle",
                discovered_at="2026-04-29T21:30:00Z",
                repository="every/verireel",
                anchor_repo="verireel",
            )

        self.assertEqual(record.status, "pass")
        self.assertEqual(record.desired_count, 1)
        self.assertEqual(record.desired_previews[0].preview_slug, "pr-42")
        self.assertEqual(record.desired_previews[0].anchor_pr_number, 42)
        list_mock.assert_called_once_with(
            owner="every",
            repo="verireel",
            token="token",
            max_pages=10,
        )

    def test_records_failure_when_runtime_token_is_missing(self) -> None:
        with patch(
            "control_plane.workflows.preview_desired_state.resolve_launchplane_github_token",
            return_value="",
        ):
            record = discover_github_preview_desired_state(
                control_plane_root=Path("/tmp/launchplane"),
                product="verireel",
                context="verireel-testing",
                source="launchplane-preview-lifecycle",
                discovered_at="2026-04-29T21:30:00Z",
                repository="every/verireel",
                anchor_repo="verireel",
            )

        self.assertEqual(record.status, "fail")
        self.assertEqual(record.desired_count, 0)
        self.assertIn("Delivery App", record.error_message)

    def test_lists_every_open_pull_request_draft_or_not_whatever_its_labels(self) -> None:
        page = [
            {
                "number": 7,
                "draft": False,
                "labels": [],
                "html_url": "https://github.com/every/verireel/pull/7",
                "head": {"sha": "a" * 40},
            },
            {
                "number": 8,
                "draft": True,
                "labels": [{"name": "preview"}],
                "html_url": "https://github.com/every/verireel/pull/8",
                "head": {"sha": "b" * 40},
            },
        ]
        with patch(
            "control_plane.workflows.preview_desired_state.github_api_request",
            return_value=page,
        ) as request:
            pulls = list_github_open_pull_requests(owner="every", repo="verireel", token="token")

        self.assertEqual(
            pulls,
            (
                {
                    "number": 7,
                    "html_url": "https://github.com/every/verireel/pull/7",
                    "head_sha": "a" * 40,
                },
                {
                    "number": 8,
                    "html_url": "https://github.com/every/verireel/pull/8",
                    "head_sha": "b" * 40,
                },
            ),
        )
        self.assertIn("/pulls?state=open", request.call_args.kwargs["path"])

    def test_a_record_written_with_the_retired_label_still_reads(self) -> None:
        record = PreviewDesiredStateRecord.model_validate(
            {
                "desired_state_id": "preview-desired-state-verireel-testing-1",
                "product": "verireel",
                "context": "verireel-testing",
                "source": "launchplane-preview-lifecycle",
                "discovered_at": "2026-04-29T21:30:00Z",
                "repository": "every/verireel",
                "label": "preview",
                "anchor_repo": "verireel",
                "status": "pass",
                "desired_count": 0,
            }
        )

        self.assertNotIn("label", record.model_dump())

    def test_a_list_cut_off_at_the_page_limit_fails_instead_of_shrinking(self) -> None:
        full_page = [
            {
                "number": number,
                "draft": False,
                "html_url": f"https://github.com/every/verireel/pull/{number}",
                "head": {"sha": "a" * 40},
            }
            for number in range(1, 101)
        ]
        with (
            patch(
                "control_plane.workflows.preview_desired_state.github_api_request",
                return_value=full_page,
            ),
            self.assertRaisesRegex(click.ClickException, "list is incomplete"),
        ):
            list_github_open_pull_requests(
                owner="every", repo="verireel", token="token", max_pages=1
            )

    def test_unknown_desired_previews_block_cleanup_of_every_preview(self) -> None:
        scan = PreviewInventoryScanRecord(
            scan_id="scan-1",
            context="verireel-testing",
            scanned_at="2026-10-02T16:00:00Z",
            source="launchplane-preview-lifecycle",
            status="pass",
            preview_count=1,
            preview_slugs=("pr-7",),
        )

        plan = build_preview_lifecycle_plan(
            product="verireel",
            context="verireel-testing",
            planned_at="2026-10-02T16:00:00Z",
            source="launchplane-preview-lifecycle",
            desired_previews=(),
            latest_inventory_scan=scan,
            desired_state_error="GitHub pull request list timed out.",
        )

        self.assertEqual((plan.status, plan.orphaned_slugs), ("fail", ()))


if __name__ == "__main__":
    unittest.main()
