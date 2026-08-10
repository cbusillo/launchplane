import unittest
from collections.abc import Callable
from types import SimpleNamespace
from typing import Literal
from unittest.mock import patch

import click

from control_plane.contracts.preview_pr_feedback_remediation import (
    PreviewPrFeedbackObservation,
    PreviewPrFeedbackRemediationRequest,
    PreviewPrFeedbackTerminalStatus,
)
from control_plane.preview_pr_feedback_remediation import (
    BoundPreviewPrFeedbackTarget,
    PreviewPrFeedbackRemediationApplyError,
    apply_remediation,
    bind_preview_pr_feedback_target,
    observe_managed_preview_pr_feedback,
)
from control_plane.workflows.preview_pr_feedback import (
    DEFAULT_PREVIEW_FEEDBACK_MARKER,
    render_preview_pr_feedback_markdown,
)


def _request(
    *,
    mode: Literal["dry-run", "apply"] = "dry-run",
    terminal_status: PreviewPrFeedbackTerminalStatus = "cleared",
) -> PreviewPrFeedbackRemediationRequest:
    confirmation = ""
    pull_request_url = "https://github.com/cbusillo/verireel/pull/311"
    if mode == "apply":
        confirmation = f"remediate preview feedback {pull_request_url} to {terminal_status}"
    return PreviewPrFeedbackRemediationRequest(
        mode=mode,
        product="verireel",
        context="verireel-preview",
        repository="cbusillo/verireel",
        pull_request_url=pull_request_url,
        terminal_status=terminal_status,
        reason="Clear a stale cleanup warning from a retired workflow identity.",
        related_issue="cbusillo/launchplane#2076",
        confirmation=confirmation,
    )


def _target() -> BoundPreviewPrFeedbackTarget:
    store = SimpleNamespace(
        read_product_profile_record=lambda _product: SimpleNamespace(
            repository="cbusillo/verireel",
            preview=SimpleNamespace(enabled=True, context="verireel-preview"),
        )
    )
    return bind_preview_pr_feedback_target(record_store=store, request=_request())


def _api_with_comments(
    comments: list[dict[str, object]],
) -> Callable[..., object]:
    def api_request(*, path: str, token: str) -> object:
        self_token = token
        if path == "/user":
            return {"id": 101, "login": "launchplane-bot"}
        if "comments?per_page=100&page=1" in path:
            return comments
        raise AssertionError((path, self_token))

    return api_request


class PreviewPrFeedbackRemediationTests(unittest.TestCase):
    def _present_observation(self, *, body: str = "old") -> PreviewPrFeedbackObservation:
        return observe_managed_preview_pr_feedback(
            target=_target(),
            token="token",
            api_request=_api_with_comments(
                [
                    {
                        "id": 55,
                        "body": f"{DEFAULT_PREVIEW_FEEDBACK_MARKER}\n{body}",
                        "html_url": "https://github.com/cbusillo/verireel/pull/311#issuecomment-55",
                        "user": {"id": 101, "login": "launchplane-bot"},
                    }
                ]
            ),
        )

    def test_absent_comment_plans_record_only_and_applies_truthful_reconciliation(
        self,
    ) -> None:
        target = _target()
        observation = observe_managed_preview_pr_feedback(
            target=target,
            token="token",
            api_request=_api_with_comments([]),
        )

        self.assertEqual(observation.state, "absent")
        with (
            patch(
                "control_plane.preview_pr_feedback_remediation.delete_github_issue_comment"
            ) as delete_comment,
            patch(
                "control_plane.preview_pr_feedback_remediation.update_github_issue_comment"
            ) as update_comment,
        ):
            outcome, evidence, feedback = apply_remediation(
                request=_request(mode="apply"),
                target=target,
                token="token",
                observation=observation,
                requested_at="2026-08-10T21:00:00Z",
                api_request=_api_with_comments([]),
            )

        self.assertEqual(outcome, "already_absent")
        self.assertFalse(evidence.attempted)
        self.assertTrue(evidence.verified_absent)
        self.assertEqual(feedback.status, "cleared")
        self.assertEqual(feedback.delivery_action, "no_existing_comment")
        delete_comment.assert_not_called()
        update_comment.assert_not_called()

    def test_owned_marker_comment_is_observed(self) -> None:
        observation = observe_managed_preview_pr_feedback(
            target=_target(),
            token="token",
            api_request=_api_with_comments(
                [
                    {
                        "id": 55,
                        "body": f"{DEFAULT_PREVIEW_FEEDBACK_MARKER}\nPreview failed.",
                        "html_url": "https://github.com/cbusillo/verireel/pull/311#issuecomment-55",
                        "user": {"id": 101, "login": "launchplane-bot"},
                    }
                ]
            ),
        )

        self.assertEqual(observation.state, "present")
        self.assertEqual(observation.comment_id, 55)
        self.assertEqual(observation.comment_author_id, observation.token_actor_id)

    def test_delete_mutation_is_verified_and_recorded(self) -> None:
        observation = self._present_observation()
        with patch(
            "control_plane.preview_pr_feedback_remediation.delete_github_issue_comment"
        ) as delete_comment:
            outcome, evidence, feedback = apply_remediation(
                request=_request(mode="apply"),
                target=_target(),
                token="token",
                observation=observation,
                requested_at="2026-08-10T21:00:01Z",
                api_request=_api_with_comments([]),
            )

        self.assertEqual(outcome, "deleted")
        self.assertTrue(evidence.attempted)
        self.assertTrue(evidence.mutated)
        self.assertTrue(evidence.verified_absent)
        self.assertEqual(feedback.delivery_action, "deleted_comment")
        delete_comment.assert_called_once()

    def test_replace_mutation_verifies_rendered_body(self) -> None:
        observation = self._present_observation()
        rendered = render_preview_pr_feedback_markdown(
            marker=DEFAULT_PREVIEW_FEEDBACK_MARKER,
            status="unsupported",
            anchor_pr_number=311,
        )
        with patch(
            "control_plane.preview_pr_feedback_remediation.update_github_issue_comment"
        ) as update_comment:
            outcome, evidence, feedback = apply_remediation(
                request=_request(mode="apply", terminal_status="unsupported"),
                target=_target(),
                token="token",
                observation=observation,
                requested_at="2026-08-10T21:00:02Z",
                api_request=_api_with_comments(
                    [
                        {
                            "id": 55,
                            "body": rendered,
                            "html_url": "https://github.com/cbusillo/verireel/pull/311#issuecomment-55",
                            "user": {"id": 101, "login": "launchplane-bot"},
                        }
                    ]
                ),
            )

        self.assertEqual(outcome, "replaced")
        self.assertTrue(evidence.mutated)
        self.assertEqual(feedback.delivery_action, "updated_comment")
        self.assertEqual(update_comment.call_args.kwargs["body"], rendered)

    def test_post_delete_verification_failure_preserves_mutation_evidence(self) -> None:
        observation = self._present_observation()

        def failed_api_request(*, path: str, token: str) -> object:
            raise click.ClickException(f"verification unavailable for {path}")

        with (
            patch("control_plane.preview_pr_feedback_remediation.delete_github_issue_comment"),
            self.assertRaises(PreviewPrFeedbackRemediationApplyError) as caught,
        ):
            apply_remediation(
                request=_request(mode="apply"),
                target=_target(),
                token="token",
                observation=observation,
                requested_at="2026-08-10T21:00:03Z",
                api_request=failed_api_request,
            )

        self.assertTrue(caught.exception.mutation_evidence.attempted)
        self.assertTrue(caught.exception.mutation_evidence.mutated)
        self.assertEqual(caught.exception.mutation_evidence.method, "DELETE")

    def test_foreign_marker_comment_is_refused(self) -> None:
        with self.assertRaisesRegex(click.ClickException, "different GitHub actor"):
            observe_managed_preview_pr_feedback(
                target=_target(),
                token="token",
                api_request=_api_with_comments(
                    [
                        {
                            "id": 56,
                            "body": DEFAULT_PREVIEW_FEEDBACK_MARKER,
                            "user": {"id": 202, "login": "contributor"},
                        }
                    ]
                ),
            )

    def test_multiple_owned_marker_comments_are_refused(self) -> None:
        comments: list[dict[str, object]] = [
            {
                "id": comment_id,
                "body": DEFAULT_PREVIEW_FEEDBACK_MARKER,
                "user": {"id": 101, "login": "launchplane-bot"},
            }
            for comment_id in (57, 58)
        ]
        with self.assertRaisesRegex(click.ClickException, "Multiple owned"):
            observe_managed_preview_pr_feedback(
                target=_target(),
                token="token",
                api_request=_api_with_comments(comments),
            )

    def test_comment_scan_page_limit_fails_closed(self) -> None:
        calls = 0

        def api_request(*, path: str, token: str) -> object:
            nonlocal calls
            if path == "/user":
                return {"id": 101, "login": "launchplane-bot"}
            calls += 1
            return [{"id": page_item, "body": "unmanaged"} for page_item in range(100)]

        with self.assertRaisesRegex(click.ClickException, "safe page limit"):
            observe_managed_preview_pr_feedback(
                target=_target(),
                token="token",
                api_request=api_request,
            )
        self.assertEqual(calls, 10)

    def test_request_rejects_non_terminal_status(self) -> None:
        with self.assertRaises(ValueError):
            PreviewPrFeedbackRemediationRequest.model_validate(
                {
                    **_request().model_dump(mode="json"),
                    "terminal_status": "ready",
                }
            )


if __name__ == "__main__":
    unittest.main()
