import unittest
from pathlib import Path
from unittest.mock import patch

from control_plane.workflows.preview_desired_state import discover_github_preview_desired_state


class PreviewDesiredStateTests(unittest.TestCase):
    def test_discovers_labeled_pull_requests_as_desired_previews(self) -> None:
        with (
            patch(
                "control_plane.workflows.preview_desired_state.resolve_launchplane_github_token",
                return_value="token",
            ),
            patch(
                "control_plane.workflows.preview_desired_state.list_github_open_pull_requests_with_label",
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
                label="preview",
                anchor_repo="verireel",
            )

        self.assertEqual(record.status, "pass")
        self.assertEqual(record.desired_count, 1)
        self.assertEqual(record.desired_previews[0].preview_slug, "pr-42")
        self.assertEqual(record.desired_previews[0].anchor_pr_number, 42)
        list_mock.assert_called_once_with(
            owner="every",
            repo="verireel",
            label="preview",
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
                label="preview",
                anchor_repo="verireel",
            )

        self.assertEqual(record.status, "fail")
        self.assertEqual(record.desired_count, 0)
        self.assertIn("GITHUB_TOKEN", record.error_message)


if __name__ == "__main__":
    unittest.main()
