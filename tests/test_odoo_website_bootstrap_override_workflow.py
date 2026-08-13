from __future__ import annotations

import unittest

from tests.support.workflows import launchplane_request_action_reference, load_workflow


class OdooWebsiteBootstrapOverrideWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load_workflow(".github/workflows/odoo-website-bootstrap-override.yml")
        self.worker = load_workflow(
            ".github/workflows/reusable-odoo-website-bootstrap-override.yml"
        )

    def test_dispatcher_pins_reviewed_reusable_worker(self) -> None:
        trigger = self.workflow.data["on"]
        assert isinstance(trigger, dict)
        workflow_dispatch = trigger["workflow_dispatch"]
        assert isinstance(workflow_dispatch, dict)
        inputs = workflow_dispatch["inputs"]
        assert isinstance(inputs, dict)
        self.assertEqual(
            set(inputs),
            {"product", "context", "instance", "website_bootstrap_payload"},
        )
        instance_input = inputs["instance"]
        assert isinstance(instance_input, dict)
        self.assertEqual(instance_input["options"], ["testing", "prod"])
        self.assertEqual(self.workflow.permissions, {"contents": "read", "id-token": "write"})
        self.assertNotIn("concurrency", self.workflow.data)
        job = self.workflow.job("write")
        self.assertEqual(
            job["uses"],
            "cbusillo/launchplane/.github/workflows/reusable-odoo-website-bootstrap-override.yml@b62941a9219f69a2575c1bce1a8d7fcb8c605f3c",
        )
        self.assertEqual(
            self.workflow.job_permissions("write"),
            {"contents": "read", "id-token": "write"},
        )
        self.assertEqual(
            job["with"],
            {
                "product": "${{ inputs.product }}",
                "context": "${{ inputs.context }}",
                "instance": "${{ inputs.instance }}",
                "website_bootstrap_payload": "${{ inputs.website_bootstrap_payload }}",
            },
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
        evidence_step = self.worker.step_named("write", "Write redacted request evidence")
        request_step = self.worker.step_named("write", "Write website bootstrap override")
        upload_step = self.worker.step_named("write", "Upload override result")
        fail_step = self.worker.step_named(
            "write", "Fail when website bootstrap override request failed"
        )
        artifact_step = self.worker.step_named("write", "Build artifact name")
        self.assertIsNotNone(validation_step)
        self.assertIsNotNone(payload_step)
        self.assertIsNotNone(evidence_step)
        self.assertIsNotNone(request_step)
        self.assertIsNotNone(upload_step)
        self.assertIsNotNone(fail_step)
        self.assertIsNotNone(artifact_step)
        assert validation_step is not None
        assert payload_step is not None
        assert evidence_step is not None
        assert request_step is not None
        assert upload_step is not None
        assert fail_step is not None
        assert artifact_step is not None
        self.assertIn("website_bootstrap_payload must be a JSON object", validation_step.run)
        self.assertIn("must be a canonical lowercase identifier", validation_step.run)
        self.assertIn("instance must be 'testing' or 'prod'", validation_step.run)
        self.assertIn("homepage_url must be empty or a local Odoo route path", validation_step.run)
        self.assertIn(
            "routes[].url values must be empty or local Odoo route paths", validation_step.run
        )
        self.assertIn("website_bootstrap: $website_bootstrap", payload_step.run)
        self.assertIn("source_label", payload_step.run)
        self.assertEqual(
            request_step.uses,
            launchplane_request_action_reference(),
        )
        self.assertEqual(
            request_step.with_values["route-path"],
            "/v1/drivers/odoo/website-bootstrap-override",
        )
        self.assertEqual(request_step.with_values["log-response-body"], False)
        self.assertEqual(
            request_step.with_values["launchplane-url"],
            "${{ env.LAUNCHPLANE_SERVICE_URL }}",
        )
        self.assertEqual(
            request_step.with_values["audience"],
            "${{ env.LAUNCHPLANE_SERVICE_AUDIENCE }}",
        )
        self.assertEqual(request_step.data["continue-on-error"], True)
        self.assertEqual(
            request_step.with_values["idempotency-key"],
            "${{ steps.payload.outputs.idempotency_key }}",
        )
        verify_step = self.worker.step_named("write", "Verify website bootstrap override response")
        self.assertIsNotNone(verify_step)
        assert verify_step is not None
        self.assertIn('if [ "$STATUS_CODE" != "202" ]', verify_step.run)
        self.assertNotIn('steps.launchplane.outputs.status-code }}" !=', verify_step.run)
        self.assertIn("website_bootstrap: (.result.website_bootstrap // false)", verify_step.run)
        self.assertIn("did not persist website-bootstrap intent", verify_step.run)
        self.assertIn("GITHUB_RUN_ID", payload_step.run)
        self.assertIn("GITHUB_RUN_ATTEMPT", payload_step.run)
        self.assertIn("product_configured", evidence_step.run)
        self.assertIn("route_count", evidence_step.run)
        self.assertNotIn("canonical_url", evidence_step.run)
        self.assertEqual(
            upload_step.data["if"],
            "${{ always() && steps.evidence.outcome == 'success' }}",
        )
        self.assertEqual(upload_step.with_values["if-no-files-found"], "error")
        artifact_name = str(upload_step.with_values["name"])
        self.assertIn("steps.artifact.outputs.name", artifact_name)
        self.assertIn("github.run_id", artifact_name)
        self.assertIn("github.run_attempt", artifact_name)
        self.assertNotIn("inputs.product", str(upload_step.with_values["name"]))
        self.assertEqual(upload_step.with_values["retention-days"], 30)
        self.assertIn("sed -E", artifact_step.run)
        upload_path = str(upload_step.with_values["path"])
        self.assertIn("odoo-website-bootstrap-override-evidence.json", upload_path)
        self.assertIn("odoo-website-bootstrap-override.json", str(upload_step.with_values["path"]))
        self.assertNotIn("odoo-website-bootstrap-override-payload.json", upload_path)
        self.assertEqual(
            fail_step.data["if"],
            "${{ steps.launchplane.outcome == 'failure' }}",
        )


if __name__ == "__main__":
    unittest.main()
