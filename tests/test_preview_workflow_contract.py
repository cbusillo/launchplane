import unittest

from pydantic import ValidationError

from control_plane.contracts.preview_workflow_contract import (
    PreviewWorkflowEvent,
    decide_preview_workflow_operation,
    preview_workflow_idempotency_key,
)


def _event(**overrides: object) -> PreviewWorkflowEvent:
    values: dict[str, object] = {
        "event_name": "pull_request",
        "action": "synchronize",
        "repository": "cbusillo/sellyouroutboard",
        "anchor_repo": "cbusillo/sellyouroutboard",
        "anchor_pr_number": 105,
        "actor": "cbusillo",
        "base_repository": "cbusillo/sellyouroutboard",
        "head_repository": "cbusillo/sellyouroutboard",
        "head_sha": "abc123",
        "label_names": ("preview",),
        "preview_label": "preview",
    }
    values.update(overrides)
    return PreviewWorkflowEvent.model_validate(values)


class PreviewWorkflowContractTests(unittest.TestCase):
    def test_same_repo_labeled_pr_refreshes_preview(self) -> None:
        decision = decide_preview_workflow_operation(
            _event(action="labeled", action_label="preview")
        )

        self.assertEqual(decision.operation, "refresh")
        self.assertEqual(decision.reason, "preview_label_added")
        self.assertEqual(decision.execution_trust, "same_repo")
        self.assertEqual(decision.launchplane_route_path, "/v1/drivers/generic-web/preview-refresh")
        self.assertEqual(decision.feedback_status, "pending")
        self.assertTrue(decision.product_build_required)
        self.assertTrue(decision.launchplane_feedback_required)

    def test_same_repo_preview_label_removal_destroys_preview(self) -> None:
        decision = decide_preview_workflow_operation(
            _event(action="unlabeled", action_label="preview", label_names=("bug",))
        )

        self.assertEqual(decision.operation, "destroy")
        self.assertEqual(decision.reason, "preview_label_removed")
        self.assertEqual(decision.feedback_status, "destroyed")
        self.assertTrue(decision.launchplane_feedback_required)

    def test_same_repo_unlabeled_pr_is_ignored(self) -> None:
        decision = decide_preview_workflow_operation(_event(label_names=("bug",)))

        self.assertEqual(decision.operation, "ignore")
        self.assertEqual(decision.reason, "preview_label_not_enabled")

    def test_pull_request_target_fork_preview_writes_unsupported_notice_only(self) -> None:
        decision = decide_preview_workflow_operation(
            _event(
                event_name="pull_request_target",
                action="labeled",
                action_label="preview",
                head_repository="somebody/sellyouroutboard",
            )
        )

        self.assertEqual(decision.operation, "unsupported_notice")
        self.assertEqual(decision.execution_trust, "fork")
        self.assertEqual(decision.launchplane_route_path, "/v1/previews/pr-feedback")
        self.assertEqual(decision.feedback_status, "unsupported")
        self.assertFalse(decision.checkout_untrusted_head)
        self.assertFalse(decision.product_build_required)

    def test_pull_request_target_same_repo_is_ignored(self) -> None:
        decision = decide_preview_workflow_operation(_event(event_name="pull_request_target"))

        self.assertEqual(decision.operation, "ignore")
        self.assertEqual(decision.reason, "pull_request_target_is_only_for_unsupported_notices")

    def test_dependabot_pull_request_event_fails_closed(self) -> None:
        with self.assertRaises(ValidationError):
            _event(actor="dependabot[bot]")

    def test_dependabot_pull_request_target_writes_unsupported_notice_only(self) -> None:
        decision = decide_preview_workflow_operation(
            _event(
                event_name="pull_request_target",
                actor="dependabot[bot]",
                action="synchronize",
            )
        )

        self.assertEqual(decision.operation, "unsupported_notice")
        self.assertEqual(decision.execution_trust, "dependabot")
        self.assertEqual(decision.feedback_status, "unsupported")

    def test_workflow_dispatch_destroy_uses_destroy_route(self) -> None:
        decision = decide_preview_workflow_operation(
            _event(event_name="workflow_dispatch", action="", operation="destroy")
        )

        self.assertEqual(decision.operation, "destroy")
        self.assertEqual(decision.reason, "manual_destroy_requested")
        self.assertEqual(decision.launchplane_route_path, "/v1/drivers/generic-web/preview-destroy")

    def test_preview_workflow_idempotency_key_is_run_scoped(self) -> None:
        self.assertEqual(
            preview_workflow_idempotency_key(
                product="sell-your-outboard",
                context="sellyouroutboard-testing",
                operation="refresh",
                anchor_pr_number=105,
                run_id="123456",
                run_attempt="2",
            ),
            "preview-workflow:sell-your-outboard:sellyouroutboard-testing:refresh:pr-105:123456:2",
        )

    def test_ignored_preview_workflow_does_not_have_idempotency_key(self) -> None:
        with self.assertRaises(ValueError):
            preview_workflow_idempotency_key(
                product="sell-your-outboard",
                context="sellyouroutboard-testing",
                operation="ignore",
                anchor_pr_number=105,
                run_id="123456",
                run_attempt="2",
            )


if __name__ == "__main__":
    unittest.main()
