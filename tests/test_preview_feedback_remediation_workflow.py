from __future__ import annotations

from pathlib import Path
import unittest

from tests.support.workflows import load_workflow


class PreviewFeedbackRemediationWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load_workflow(".github/workflows/preview-feedback-remediation.yml")

    def test_workflow_is_manual_oidc_and_apply_is_protected(self) -> None:
        trigger = self.workflow.data["on"]
        assert isinstance(trigger, dict)
        self.assertEqual(set(trigger), {"workflow_dispatch"})
        self.assertEqual(self.workflow.permissions, {"contents": "read", "id-token": "write"})
        self.assertEqual(set(self.workflow.jobs), {"plan", "apply"})
        self.assertEqual(
            self.workflow.job("apply")["environment"],
            "launchplane-authz-admin",
        )
        self.assertEqual(
            self.workflow.job("apply")["if"],
            "${{ inputs.mode == 'apply' }}",
        )

    def test_workflow_accepts_only_exact_bounded_inputs(self) -> None:
        trigger = self.workflow.data["on"]
        assert isinstance(trigger, dict)
        workflow_dispatch = trigger["workflow_dispatch"]
        assert isinstance(workflow_dispatch, dict)
        inputs = workflow_dispatch["inputs"]
        assert isinstance(inputs, dict)
        self.assertEqual(
            set(inputs),
            {
                "mode",
                "product",
                "repository",
                "pull_request_number",
                "desired_status",
                "reason",
                "issue_reference",
                "confirmation",
            },
        )
        desired_status = inputs["desired_status"]
        assert isinstance(desired_status, dict)
        self.assertEqual(desired_status["options"], ["cleared", "destroyed"])
        workflow_text = Path(self.workflow.path).read_text(encoding="utf-8")
        self.assertNotIn("marker:", workflow_text)
        self.assertNotIn("comment_id:", workflow_text)
        self.assertNotIn("comment_markdown:", workflow_text)

    def test_plan_and_apply_use_exact_remediation_route(self) -> None:
        plan_request = self.workflow.step_named("plan", "Plan preview feedback remediation")
        apply_request = self.workflow.step_named("apply", "Apply preview feedback remediation")
        self.assertIsNotNone(plan_request)
        self.assertIsNotNone(apply_request)
        assert plan_request is not None
        assert apply_request is not None
        for request_step in (plan_request, apply_request):
            self.assertEqual(
                request_step.uses,
                "cbusillo/launchplane/.github/actions/launchplane-request@"
                "7c7493f840fb6dc032d26f30fc546ae9553ebd37",
            )
            self.assertEqual(
                request_step.with_values["route-path"],
                "/v1/previews/pr-feedback/remediation",
            )
        self.assertEqual(
            apply_request.with_values["idempotency-key"],
            "${{ steps.request.outputs.idempotency_key }}",
        )
        self.assertEqual(
            apply_request.with_values["fail-result-paths"],
            "result.feedback.delivery_status",
        )

    def test_workflow_requires_reason_issue_plan_and_confirmation(self) -> None:
        validation = self.workflow.step_named(
            "plan", "Validate preview feedback remediation inputs"
        )
        self.assertIsNotNone(validation)
        assert validation is not None
        self.assertIn('required_line("REASON")', validation.run)
        self.assertIn("issue_reference must be an exact GitHub issue URL", validation.run)
        self.assertIn("APPLY PREVIEW FEEDBACK REMEDIATION", validation.run)
        apply_payload = self.workflow.step_named(
            "apply", "Render preview feedback remediation apply request"
        )
        self.assertIsNotNone(apply_payload)
        assert apply_payload is not None
        self.assertIn("expected_plan_sha256", apply_payload.run)
        self.assertIn("idempotency_key", apply_payload.run)


if __name__ == "__main__":
    unittest.main()
