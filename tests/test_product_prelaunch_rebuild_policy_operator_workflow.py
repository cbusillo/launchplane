from __future__ import annotations

from pathlib import Path
import unittest

from tests.support.workflows import load_workflow


class ProductPrelaunchRebuildPolicyOperatorWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load_workflow(".github/workflows/product-prelaunch-rebuild-policy.yml")
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

    def test_dispatch_wrapper_calls_immutable_worker(self) -> None:
        trigger = self.workflow.data["on"]
        assert isinstance(trigger, dict)
        self.assertEqual(set(trigger), {"workflow_dispatch"})
        self.assertEqual(self.workflow.permissions, {"contents": "read", "id-token": "write"})
        self.assertEqual(set(self.workflow.jobs), {"apply"})
        self.assertEqual(
            self.workflow.job_uses("apply"),
            "cbusillo/launchplane/.github/workflows/"
            "reusable-product-prelaunch-rebuild-policy.yml@"
            "f63623c4228b227d2baa3325de3d0a576fc8af9f",
        )
        forwarded_inputs = self.workflow.job("apply")["with"]
        assert isinstance(forwarded_inputs, dict)
        self.assertEqual(
            set(forwarded_inputs),
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

    def test_dispatch_wrapper_has_no_real_target_defaults(self) -> None:
        trigger = self.workflow.data["on"]
        assert isinstance(trigger, dict)
        workflow_dispatch = trigger["workflow_dispatch"]
        assert isinstance(workflow_dispatch, dict)
        inputs = workflow_dispatch["inputs"]
        assert isinstance(inputs, dict)
        for name in (
            "product",
            "context",
            "instance",
            "approval_issue_url",
            "policy_confirmation",
            "expected_target_name",
        ):
            value = inputs[name]
            assert isinstance(value, dict)
            if name in {"product", "context", "instance"}:
                self.assertNotIn("default", value)
            else:
                self.assertEqual(value.get("default"), "")


if __name__ == "__main__":
    unittest.main()
