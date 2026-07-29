from __future__ import annotations

import unittest

from tests.support.workflows import load_workflow


class OdooWebsiteBootstrapOverrideWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.worker = load_workflow(
            ".github/workflows/reusable-odoo-website-bootstrap-override.yml"
        )

    def test_reusable_worker_preserves_typed_override_contract(self) -> None:
        trigger = self.worker.data["on"]
        assert isinstance(trigger, dict)
        workflow_call = trigger["workflow_call"]
        assert isinstance(workflow_call, dict)
        inputs = workflow_call["inputs"]
        assert isinstance(inputs, dict)
        self.assertEqual(
            set(inputs),
            {
                "product",
                "context",
                "instance",
                "website_bootstrap_payload",
                "launchplane_url",
                "launchplane_audience",
            },
        )
        self.assertEqual(self.worker.job("write")["runs-on"], "ubuntu-latest")
        self.assertEqual(
            self.worker.job_permissions("write"),
            {"contents": "read", "id-token": "write"},
        )
        concurrency = self.worker.data["concurrency"]
        assert isinstance(concurrency, dict)
        self.assertIn("odoo-website-bootstrap-override", str(concurrency["group"]))
        self.assertEqual(concurrency["cancel-in-progress"], False)
        validation_step = self.worker.step_named("write", "Validate runtime inputs")
        payload_step = self.worker.step_named("write", "Write website bootstrap override payload")
        request_step = self.worker.step_named("write", "Write website bootstrap override")
        upload_step = self.worker.step_named("write", "Upload override result")
        self.assertIsNotNone(validation_step)
        self.assertIsNotNone(payload_step)
        self.assertIsNotNone(request_step)
        self.assertIsNotNone(upload_step)
        assert validation_step is not None
        assert payload_step is not None
        assert request_step is not None
        assert upload_step is not None
        self.assertIn("website_bootstrap_payload must be a JSON object", validation_step.run)
        self.assertIn("instance must be 'testing' or 'prod'", validation_step.run)
        self.assertIn("homepage_url must be empty or a local Odoo route path", validation_step.run)
        self.assertIn(
            "routes[].url values must be empty or local Odoo route paths", validation_step.run
        )
        self.assertIn("website_bootstrap: $website_bootstrap", payload_step.run)
        self.assertIn("source_label", payload_step.run)
        self.assertEqual(
            request_step.uses,
            "cbusillo/launchplane/.github/actions/launchplane-request@adcf937c6aef14e02478724040852d1d2a82a850",
        )
        self.assertEqual(
            request_step.with_values["route-path"],
            "/v1/drivers/odoo/website-bootstrap-override",
        )
        self.assertEqual(request_step.with_values["log-response-body"], False)
        verify_step = self.worker.step_named("write", "Verify website bootstrap override response")
        self.assertIsNotNone(verify_step)
        assert verify_step is not None
        self.assertIn('if [ "$STATUS_CODE" != "202" ]', verify_step.run)
        self.assertNotIn('steps.launchplane.outputs.status-code }}" !=', verify_step.run)
        self.assertIn("website_bootstrap: (.result.website_bootstrap // false)", verify_step.run)
        self.assertIn("GITHUB_RUN_ID", payload_step.run)
        self.assertIn("GITHUB_RUN_ATTEMPT", payload_step.run)
        self.assertEqual(upload_step.data["if"], "always()")
        self.assertEqual(upload_step.with_values["if-no-files-found"], "error")
        self.assertIn("odoo-website-bootstrap-override.json", str(upload_step.with_values["path"]))


if __name__ == "__main__":
    unittest.main()
