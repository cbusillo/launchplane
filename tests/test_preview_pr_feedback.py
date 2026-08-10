import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.every_code_worker import every_code_worktree_branch
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.preview_pr_feedback import (
    DEFAULT_PREVIEW_FEEDBACK_MARKER,
    _render_preview_pr_feedback_markdown,
    build_preview_pr_feedback_record,
)


def _every_code_request(*, result_pr_url: str = "") -> EveryCodeWorkRequestRecord:
    return EveryCodeWorkRequestRecord(
        request_id="every-code-cbusillo-sellyouroutboard-82-test",
        source="github_issue_label",
        state="running",
        repository="cbusillo/sellyouroutboard",
        issue_number=82,
        issue_url="https://github.com/cbusillo/sellyouroutboard/issues/82",
        issue_title="Improve image previews",
        trigger_label="every-code",
        trigger_actor="Mbanks89",
        github_delivery_id="delivery-82",
        queued_at="2026-05-07T12:00:00Z",
        updated_at="2026-05-07T12:10:00Z",
        claimed_at="2026-05-07T12:01:00Z",
        claimed_by_host="Chris-Studio",
        started_at="2026-05-07T12:02:00Z",
        result_pr_url=result_pr_url,
    )


class PreviewPrFeedbackWorkflowTests(unittest.TestCase):
    def test_cleanup_failure_without_resource_evidence_does_not_claim_preview_exists(
        self,
    ) -> None:
        markdown = _render_preview_pr_feedback_markdown(
            marker=DEFAULT_PREVIEW_FEEDBACK_MARKER,
            status="cleanup_failed",
            anchor_pr_number=42,
            preview_url="",
            immutable_image_reference="",
            refresh_image_reference="",
            revision="",
            run_url="https://github.com/every/verireel/actions/runs/1",
            failure_summary="Cleanup request was not authorized.",
        )

        self.assertNotIn("may still exist", markdown)
        self.assertIn("could not confirm cleanup", markdown)

    def test_cleanup_failure_with_resource_evidence_warns_preview_may_exist(self) -> None:
        markdown = _render_preview_pr_feedback_markdown(
            marker=DEFAULT_PREVIEW_FEEDBACK_MARKER,
            status="cleanup_failed",
            anchor_pr_number=42,
            preview_url="https://pr-42.preview.example.test",
            immutable_image_reference="",
            refresh_image_reference="",
            revision="",
            run_url="",
            failure_summary="Provider teardown failed.",
        )

        self.assertIn("may still exist", markdown)

    def test_cleanup_failure_uses_active_preview_record_as_resource_evidence(self) -> None:
        preview_store = MagicMock()
        preview_store.list_preview_records.return_value = (
            PreviewRecord(
                preview_id="preview-verireel-testing-verireel-pr-42",
                context="verireel-testing",
                anchor_repo="verireel",
                anchor_pr_number=42,
                anchor_pr_url="https://github.com/every/verireel/pull/42",
                preview_label="preview",
                canonical_url="https://pr-42.preview.example.test",
                state="active",
                created_at="2026-08-10T12:00:00Z",
                updated_at="2026-08-10T12:00:00Z",
                eligible_at="2026-08-10T12:00:00Z",
            ),
        )

        with patch(
            "control_plane.workflows.preview_pr_feedback.resolve_launchplane_github_token",
            return_value="",
        ):
            record = build_preview_pr_feedback_record(
                control_plane_root=Path("."),
                product="verireel",
                context="verireel-testing",
                source="preview-fork-notice",
                requested_at="2026-08-10T12:05:00Z",
                repository="every/verireel",
                anchor_repo="verireel",
                anchor_pr_number=42,
                anchor_pr_url="https://github.com/every/verireel/pull/42",
                status="cleanup_failed",
                failure_summary="Provider teardown failed.",
                preview_record_store=preview_store,
            )

        self.assertIn("https://pr-42.preview.example.test", record.comment_markdown)
        self.assertIn("may still exist", record.comment_markdown)

    def test_ready_feedback_notifies_issue_author_on_source_issue(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            request = _every_code_request(
                result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/88"
            )
            store.write_every_code_work_request_record(request)

            with (
                patch(
                    "control_plane.workflows.preview_pr_feedback.resolve_launchplane_github_token",
                    return_value="github-token",
                ),
                patch(
                    "control_plane.workflows.preview_pr_feedback.find_github_issue_comment_by_marker",
                    side_effect=[None, None, None],
                ) as find_comment,
                patch(
                    "control_plane.workflows.preview_pr_feedback.create_github_issue_comment",
                    side_effect=[
                        {
                            "id": 123,
                            "html_url": "https://github.com/cbusillo/sellyouroutboard/pull/88#issuecomment-123",
                        },
                        {
                            "id": 456,
                            "html_url": "https://github.com/cbusillo/sellyouroutboard/issues/82#issuecomment-456",
                        },
                    ],
                ) as create_comment,
                patch(
                    "control_plane.workflows.preview_pr_feedback.github_api_request",
                    side_effect=[
                        {"user": {"login": "code-agent"}, "head": {"ref": "feature/preview"}},
                        {"user": {"login": "Mbanks89"}},
                        {"owner": {"login": "cbusillo", "type": "User"}},
                        [{"name": "preview-ready"}],
                        {},
                        {},
                    ],
                ) as github_request,
            ):
                record = build_preview_pr_feedback_record(
                    control_plane_root=Path("."),
                    product="sellyouroutboard",
                    context="sellyouroutboard-preview",
                    source="preview-control-plane",
                    requested_at="2026-05-07T12:20:00Z",
                    repository="cbusillo/sellyouroutboard",
                    anchor_repo="sellyouroutboard",
                    anchor_pr_number=88,
                    anchor_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/88",
                    status="ready",
                    preview_url="https://pr-88.sellyouroutboard.dev",
                    every_code_record_store=store,
                )

        self.assertEqual(record.delivery_status, "delivered")
        self.assertEqual(record.delivery_action, "created_comment")
        self.assertEqual(find_comment.call_count, 3)
        self.assertEqual(
            find_comment.call_args_list[0].kwargs["marker"], DEFAULT_PREVIEW_FEEDBACK_MARKER
        )
        self.assertEqual(
            find_comment.call_args_list[1].kwargs["marker"],
            "<!-- verireel-preview-control -->",
        )
        create_comment.assert_any_call(
            owner="cbusillo",
            repo="sellyouroutboard",
            issue_number=88,
            token="github-token",
            body=record.comment_markdown,
        )
        source_issue_call = create_comment.call_args_list[1]
        self.assertEqual(source_issue_call.kwargs["issue_number"], 82)
        self.assertIn("@Mbanks89", source_issue_call.kwargs["body"])
        self.assertIn("https://pr-88.sellyouroutboard.dev", source_issue_call.kwargs["body"])
        self.assertIn(
            "Confirm the preview resolves: Improve image previews", source_issue_call.kwargs["body"]
        )
        self.assertIn("/preview ok", source_issue_call.kwargs["body"])
        self.assertNotIn("/preview approve", source_issue_call.kwargs["body"])
        self.assertIn("author%3AMbanks89", source_issue_call.kwargs["body"])
        self.assertIn("assignee%3Acbusillo", source_issue_call.kwargs["body"])
        self.assertEqual(github_request.call_args_list[3].kwargs["method"], "POST")
        self.assertEqual(
            github_request.call_args_list[3].kwargs["body"], {"labels": ["preview-ready"]}
        )

    def test_ready_feedback_updates_issue_comment_without_review_request(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            request = _every_code_request()
            store.write_every_code_work_request_record(request)

            with (
                patch(
                    "control_plane.workflows.preview_pr_feedback.resolve_launchplane_github_token",
                    return_value="github-token",
                ),
                patch(
                    "control_plane.workflows.preview_pr_feedback.find_github_issue_comment_by_marker",
                    side_effect=[
                        None,
                        {"id": 123, "body": "<!-- verireel-preview-control -->\nold"},
                        {
                            "id": 456,
                            "body": "<!-- launchplane-every-code-preview-ready:cbusillo/sellyouroutboard#88 -->\nold",
                        },
                    ],
                ) as find_comment,
                patch(
                    "control_plane.workflows.preview_pr_feedback.update_github_issue_comment",
                    return_value={
                        "id": 123,
                        "html_url": "https://github.com/cbusillo/sellyouroutboard/pull/88#issuecomment-123",
                    },
                ) as update_comment,
                patch(
                    "control_plane.workflows.preview_pr_feedback.create_github_issue_comment"
                ) as create_comment,
                patch(
                    "control_plane.workflows.preview_pr_feedback.github_api_request",
                    side_effect=[
                        {
                            "user": {"login": "Mbanks89"},
                            "head": {"ref": every_code_worktree_branch(request)},
                        },
                        {"user": {"login": "Mbanks89"}},
                        {"owner": {"login": "cbusillo", "type": "User"}},
                        [{"name": "preview-ready"}],
                        {},
                        {},
                    ],
                ) as github_request,
            ):
                record = build_preview_pr_feedback_record(
                    control_plane_root=Path("."),
                    product="sellyouroutboard",
                    context="sellyouroutboard-preview",
                    source="preview-control-plane",
                    requested_at="2026-05-07T12:20:00Z",
                    repository="cbusillo/sellyouroutboard",
                    anchor_repo="sellyouroutboard",
                    anchor_pr_number=88,
                    anchor_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/88",
                    status="ready",
                    preview_url="https://pr-88.sellyouroutboard.dev",
                    every_code_record_store=store,
                )

        self.assertEqual(record.delivery_status, "delivered")
        self.assertEqual(record.marker, DEFAULT_PREVIEW_FEEDBACK_MARKER)
        self.assertIn(DEFAULT_PREVIEW_FEEDBACK_MARKER, record.comment_markdown)
        create_comment.assert_not_called()
        self.assertEqual(update_comment.call_count, 2)
        self.assertEqual(
            find_comment.call_args_list[0].kwargs["marker"], DEFAULT_PREVIEW_FEEDBACK_MARKER
        )
        self.assertEqual(
            find_comment.call_args_list[1].kwargs["marker"],
            "<!-- verireel-preview-control -->",
        )
        self.assertEqual(update_comment.call_args_list[0].kwargs["comment_id"], 123)
        self.assertEqual(update_comment.call_args_list[1].kwargs["comment_id"], 456)
        self.assertIn("comment `/preview ok`", update_comment.call_args_list[1].kwargs["body"])
        self.assertNotIn("reviewer", update_comment.call_args_list[1].kwargs["body"].lower())
        self.assertEqual(github_request.call_count, 6)

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
        self.assertEqual(record.marker, DEFAULT_PREVIEW_FEEDBACK_MARKER)
        self.assertIn(DEFAULT_PREVIEW_FEEDBACK_MARKER, record.comment_markdown)
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

    def test_custom_marker_does_not_fall_back_to_legacy_default_marker(self) -> None:
        with (
            patch(
                "control_plane.workflows.preview_pr_feedback.resolve_launchplane_github_token",
                return_value="github-token",
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.find_github_issue_comment_by_marker",
                return_value=None,
            ) as find_comment,
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
        find_comment.assert_called_once_with(
            owner="every",
            repo="verireel",
            issue_number=43,
            token="github-token",
            marker="<!-- verireel-preview-unsupported -->",
        )
        delete_comment.assert_not_called()
        create_comment.assert_not_called()


if __name__ == "__main__":
    unittest.main()
