import re
import unittest

from tests.support.workflows import load_workflow


class ProductRetirementOperatorWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load_workflow(".github/workflows/product-retirement.yml")

    def test_workflow_is_protected_exact_and_redacted(self) -> None:
        trigger = self.workflow.data["on"]
        assert isinstance(trigger, dict)
        self.assertEqual(set(trigger), {"workflow_dispatch"})
        dispatch = trigger["workflow_dispatch"]
        assert isinstance(dispatch, dict)
        inputs = dispatch["inputs"]
        assert isinstance(inputs, dict)
        mode_input = inputs["mode"]
        assert isinstance(mode_input, dict)
        self.assertEqual(mode_input["options"], ["plan", "apply"])
        for required_input in (
            "product",
            "instance",
            "expected_target_sha256",
            "operator_idempotency_key",
            "reason",
            "related_issue",
        ):
            input_contract = inputs[required_input]
            assert isinstance(input_contract, dict)
            self.assertTrue(input_contract["required"])
        job = self.workflow.job("retire")
        self.assertEqual(job["environment"], "launchplane-authz-admin")
        self.assertEqual(
            self.workflow.job_permissions("retire"),
            {"contents": "read", "id-token": "write"},
        )
        request_step = self.workflow.step_named("retire", "Request audited product retirement")
        self.assertIsNotNone(request_step)
        assert request_step is not None
        action = request_step.data["uses"]
        assert isinstance(action, str)
        self.assertRegex(
            action,
            re.compile(r"^cbusillo/launchplane/\.github/actions/launchplane-request@[0-9a-f]{40}$"),
        )
        request_inputs = request_step.data["with"]
        assert isinstance(request_inputs, dict)
        self.assertEqual(request_inputs["route-path"], "/v1/product-retirement")
        validation = self.workflow.step_named("retire", "Validate exact retirement intent")
        self.assertIsNotNone(validation)
        assert validation is not None
        self.assertIn("exactly bind product, instance, and target digest", validation.run)
        evidence = self.workflow.step_named("retire", "Verify redacted retirement evidence")
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertIn("provider_operation_key", evidence.run)
        self.assertIn("target_id", evidence.run)
        upload = self.workflow.step_named("retire", "Upload redacted retirement evidence")
        self.assertIsNotNone(upload)
        assert upload is not None
        upload_inputs = upload.data["with"]
        assert isinstance(upload_inputs, dict)
        self.assertEqual(upload_inputs["path"], "product-retirement-response.json")


if __name__ == "__main__":
    unittest.main()
