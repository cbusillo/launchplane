import unittest
from pathlib import Path
from unittest.mock import patch

from control_plane.workflows.preview_pr_feedback import build_preview_pr_feedback_record


class PreviewPrFeedbackWorkflowTests(unittest.TestCase):
    def test_pending_feedback_renders_neutral_waiting_comment(self) -> None:
        with (
            patch(
                "control_plane.workflows.preview_pr_feedback.resolve_launchplane_github_token",
                return_value="github-token",
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.find_github_issue_comment_by_marker",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.create_github_issue_comment",
                return_value={
                    "id": 123,
                    "html_url": "https://github.com/every/verireel/pull/43#issuecomment-123",
                },
            ) as create_comment,
            patch(
                "control_plane.workflows.preview_pr_feedback.update_github_issue_comment"
            ) as update_comment,
        ):
            record = build_preview_pr_feedback_record(
                control_plane_root=Path("."),
                product="verireel",
                context="verireel-testing",
                source="preview-fork-notice",
                requested_at="2026-04-30T00:00:00Z",
                repository="every/verireel",
                anchor_repo="verireel",
                anchor_pr_number=43,
                anchor_pr_url="https://github.com/every/verireel/pull/43",
                status="pending",
                run_url="https://github.com/every/verireel/actions/runs/123",
            )

        self.assertEqual(record.status, "pending")
        self.assertEqual(record.delivery_status, "delivered")
        self.assertEqual(record.delivery_action, "created_comment")
        self.assertIn(
            "Launchplane preview is waiting for PR #43.",
            record.comment_markdown,
        )
        self.assertIn(
            "Preview prerequisites are still in flight.",
            record.comment_markdown,
        )
        self.assertNotIn("failed", record.comment_markdown.lower())
        create_comment.assert_called_once()
        update_comment.assert_not_called()

    def test_cleared_feedback_deletes_existing_comment(self) -> None:
        with (
            patch(
                "control_plane.workflows.preview_pr_feedback.resolve_launchplane_github_token",
                return_value="github-token",
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.find_github_issue_comment_by_marker",
                return_value={"id": 123, "body": "<!-- verireel-preview-unsupported -->\nold"},
            ) as find_comment,
            patch(
                "control_plane.workflows.preview_pr_feedback.delete_github_issue_comment"
            ) as delete_comment,
            patch(
                "control_plane.workflows.preview_pr_feedback.create_github_issue_comment"
            ) as create_comment,
            patch(
                "control_plane.workflows.preview_pr_feedback.update_github_issue_comment"
            ) as update_comment,
        ):
            record = build_preview_pr_feedback_record(
                control_plane_root=Path("."),
                product="verireel",
                context="verireel-testing",
                source="preview-fork-notice",
                requested_at="2026-04-30T00:00:00Z",
                repository="every/verireel",
                anchor_repo="verireel",
                anchor_pr_number=43,
                anchor_pr_url="https://github.com/every/verireel/pull/43",
                status="cleared",
                marker="<!-- verireel-preview-unsupported -->",
            )

        self.assertEqual(record.status, "cleared")
        self.assertEqual(record.delivery_status, "delivered")
        self.assertEqual(record.delivery_action, "deleted_comment")
        self.assertEqual(record.comment_id, 123)
        find_comment.assert_called_once_with(
            owner="every",
            repo="verireel",
            issue_number=43,
            token="github-token",
            marker="<!-- verireel-preview-unsupported -->",
        )
        delete_comment.assert_called_once_with(
            owner="every",
            repo="verireel",
            comment_id=123,
            token="github-token",
        )
        create_comment.assert_not_called()
        update_comment.assert_not_called()

    def test_cleared_feedback_skips_when_comment_is_missing(self) -> None:
        with (
            patch(
                "control_plane.workflows.preview_pr_feedback.resolve_launchplane_github_token",
                return_value="github-token",
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.find_github_issue_comment_by_marker",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.delete_github_issue_comment"
            ) as delete_comment,
            patch(
                "control_plane.workflows.preview_pr_feedback.create_github_issue_comment"
            ) as create_comment,
        ):
            record = build_preview_pr_feedback_record(
                control_plane_root=Path("."),
                product="verireel",
                context="verireel-testing",
                source="preview-fork-notice",
                requested_at="2026-04-30T00:00:00Z",
                repository="every/verireel",
                anchor_repo="verireel",
                anchor_pr_number=43,
                anchor_pr_url="https://github.com/every/verireel/pull/43",
                status="cleared",
                marker="<!-- verireel-preview-unsupported -->",
            )

        self.assertEqual(record.delivery_status, "skipped")
        self.assertEqual(record.delivery_action, "no_existing_comment")
        delete_comment.assert_not_called()
        create_comment.assert_not_called()


if __name__ == "__main__":
    unittest.main()
