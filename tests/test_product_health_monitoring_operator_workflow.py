from __future__ import annotations

from pathlib import Path
import unittest

from tests.support.workflows import load_workflow


class ProductHealthMonitoringOperatorWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load_workflow(".github/workflows/product-health-monitoring.yml")
        self.worker = load_workflow(".github/workflows/reusable-product-health-monitoring.yml")

    def test_reusable_worker_is_oidc_service_backed(self) -> None:
        trigger = self.worker.data["on"]
        assert isinstance(trigger, dict)
        self.assertEqual(set(trigger), {"workflow_call"})
        self.assertEqual(self.worker.permissions, {"contents": "read", "id-token": "write"})
        self.assertEqual(set(self.worker.jobs), {"apply"})
        self.assertEqual(self.worker.job("apply")["runs-on"], "ubuntu-latest")

        request_step = self.worker.step_named(
            "apply", "Request product health monitoring operation"
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
            "/v1/product-profiles/health-monitoring/apply",
        )
        self.assertEqual(request_step.with_values["log-response-body"], "false")

    def test_worker_requires_reviewed_apply_guards(self) -> None:
        validation_step = self.worker.step_named(
            "apply", "Validate product health monitoring request"
        )
        self.assertIsNotNone(validation_step)
        assert validation_step is not None
        self.assertIn("APPLY PRODUCT HEALTH MONITORING", validation_step.run)
        self.assertIn("reviewed dry-run plan SHA-256", validation_step.run)
        self.assertIn("idempotency_key is required for apply", validation_step.run)
        self.assertIn("A disabled check cannot require runtime identity", validation_step.run)
        self.assertIn(
            "enabled private_http checks require private_endpoint_key", validation_step.run
        )
        self.assertIn(
            "monitoring_intent must be public, private, or prelaunch",
            validation_step.run,
        )

    def test_worker_accepts_no_topology_or_full_profile_inputs(self) -> None:
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
                "check_name",
                "check_kind",
                "monitoring_intent",
                "enabled",
                "require_runtime_identity",
                "private_endpoint_key",
                "reviewed_plan_sha256",
                "reason",
                "idempotency_key",
                "confirmation",
            },
        )
        workflow_text = Path(self.worker.path).read_text(encoding="utf-8")
        for forbidden in (
            "domain_name",
            "target_id",
            "provider_host_id",
            "certificate_ref",
            "health_url",
            "base_url",
            "product_profile_json",
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
            "reusable-product-health-monitoring.yml@"
            "88584ae2800bceabc9d448eba7defddc5da75ec1",
        )
        job = self.workflow.job("apply")
        forwarded_inputs = job["with"]
        assert isinstance(forwarded_inputs, dict)
        self.assertEqual(
            set(forwarded_inputs),
            {
                "mode",
                "product",
                "context",
                "instance",
                "check_name",
                "check_kind",
                "monitoring_intent",
                "enabled",
                "require_runtime_identity",
                "private_endpoint_key",
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
        for name in ("product", "context", "instance"):
            value = inputs[name]
            assert isinstance(value, dict)
            self.assertNotIn("default", value)


if __name__ == "__main__":
    unittest.main()
