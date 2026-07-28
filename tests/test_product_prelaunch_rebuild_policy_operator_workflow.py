from __future__ import annotations

from pathlib import Path
import unittest

from tests.support.workflows import load_workflow


class ProductPrelaunchRebuildPolicyOperatorWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.worker = load_workflow(
            ".github/workflows/reusable-product-prelaunch-rebuild-policy.yml"
        )

    def test_reusable_worker_is_oidc_service_backed(self) -> None:
        trigger = self.worker.data["on"]
        assert isinstance(trigger, dict)
        self.assertEqual(set(trigger), {"workflow_call"})
        self.assertEqual(self.worker.permissions, {"contents": "read", "id-token": "write"})
        self.assertEqual(set(self.worker.jobs), {"apply"})
        self.assertEqual(self.worker.job("apply")["runs-on"], "ubuntu-latest")

        request_step = self.worker.step_named(
            "apply", "Request product prelaunch rebuild policy operation"
        )
        self.assertIsNotNone(request_step)
        assert request_step is not None
        self.assertEqual(
            request_step.uses,
            "cbusillo/launchplane/.github/actions/launchplane-request@"
            "adcf937c6aef14e02478724040852d1d2a82a850",
        )
        self.assertEqual(
            request_step.with_values["route-path"],
            "/v1/product-profiles/prelaunch-rebuild/apply",
        )
        self.assertEqual(request_step.with_values["log-response-body"], "false")

    def test_worker_requires_reviewed_apply_guards(self) -> None:
        validation_step = self.worker.step_named(
            "apply", "Validate product prelaunch rebuild policy request"
        )
        self.assertIsNotNone(validation_step)
        assert validation_step is not None
        self.assertIn(
            "APPLY PRODUCT PRELAUNCH REBUILD POLICY",
            validation_step.run,
        )
        self.assertIn("reviewed dry-run plan SHA-256", validation_step.run)
        self.assertIn("idempotency_key is required for apply", validation_step.run)
        self.assertIn(
            "expected_domains_json must be a JSON array of strings",
            validation_step.run,
        )
        self.assertIn(
            "Disabled policy requests must clear all destructive proof fields",
            validation_step.run,
        )

    def test_worker_accepts_only_bounded_policy_inputs(self) -> None:
        trigger = self.worker.data["on"]
        assert isinstance(trigger, dict)
        workflow_call = trigger["workflow_call"]
        assert isinstance(workflow_call, dict)
        inputs = workflow_call["inputs"]
        assert isinstance(inputs, dict)
        self.assertEqual(
            set(inputs),
            {
                "mode",
                "product",
                "context",
                "instance",
                "enabled",
                "approval_issue_url",
                "data_source_mode",
                "policy_confirmation",
                "expected_target_name",
                "expected_domains_json",
                "reviewed_plan_sha256",
                "reason",
                "idempotency_key",
                "confirmation",
            },
        )
        workflow_text = Path(self.worker.path).read_text(encoding="utf-8")
        for forbidden in (
            "product_profile_json",
            "provider_target_id",
            "provider_host_id",
            "database_volume",
            "filestore_volume",
            "secret_plaintext",
            "monitoring_intent:",
            "health_check:",
        ):
            self.assertNotIn(forbidden, workflow_text)


if __name__ == "__main__":
    unittest.main()
