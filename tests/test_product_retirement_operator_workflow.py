import re
import unittest

from tests.support.workflows import load_workflow


class ProductRetirementOperatorWorkflowTests(unittest.TestCase):
    WORKER_REFERENCE = (
        "cbusillo/launchplane/.github/workflows/reusable-product-retirement.yml"
        "@c922d5f1a0bf3ab17a829196042a96ba89d7b693"
    )
    INPUT_NAMES = (
        "mode",
        "product",
        "instance",
        "expected_target_sha256",
        "operator_idempotency_key",
        "reason",
        "related_issue",
        "reviewed_plan_record_id",
        "reviewed_plan_sha256",
        "confirmation",
    )

    def setUp(self) -> None:
        self.workflow = load_workflow(".github/workflows/product-retirement.yml")
        self.reusable_workflow = load_workflow(
            ".github/workflows/reusable-product-retirement.yml"
        )

    def test_wrapper_is_an_immutable_thin_dispatch_contract(self) -> None:
        trigger = self.workflow.data["on"]
        assert isinstance(trigger, dict)
        self.assertEqual(set(trigger), {"workflow_dispatch"})
        dispatch = trigger["workflow_dispatch"]
        assert isinstance(dispatch, dict)
        inputs = dispatch["inputs"]
        assert isinstance(inputs, dict)
        self.assertEqual(tuple(inputs), self.INPUT_NAMES)
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
        self.assertEqual(self.workflow.permissions, {})
        self.assertNotIn("concurrency", self.workflow.data)

        job = self.workflow.job("retire")
        self.assertEqual(job["uses"], self.WORKER_REFERENCE)
        self.assertEqual(
            set(job),
            {"uses", "permissions", "with"},
        )
        self.assertEqual(
            self.workflow.job_permissions("retire"),
            {"contents": "read", "id-token": "write"},
        )
        for forbidden_key in ("runs-on", "environment", "steps", "concurrency"):
            self.assertNotIn(forbidden_key, job)

        wrapper_inputs = job["with"]
        assert isinstance(wrapper_inputs, dict)
        self.assertEqual(
            wrapper_inputs,
            {name: f"${{{{ inputs.{name} }}}}" for name in self.INPUT_NAMES},
        )

    def test_reusable_worker_matches_protected_dispatch_contract(self) -> None:
        trigger = self.reusable_workflow.data["on"]
        assert isinstance(trigger, dict)
        self.assertEqual(set(trigger), {"workflow_call"})
        reusable_call = trigger["workflow_call"]
        assert isinstance(reusable_call, dict)
        reusable_inputs = reusable_call["inputs"]
        assert isinstance(reusable_inputs, dict)

        wrapper_trigger = self.workflow.data["on"]
        assert isinstance(wrapper_trigger, dict)
        dispatch = wrapper_trigger["workflow_dispatch"]
        assert isinstance(dispatch, dict)
        dispatch_inputs = dispatch["inputs"]
        assert isinstance(dispatch_inputs, dict)
        self.assertEqual(tuple(reusable_inputs), self.INPUT_NAMES)
        self.assertEqual(tuple(dispatch_inputs), self.INPUT_NAMES)
        for name, dispatch_input in dispatch_inputs.items():
            assert isinstance(dispatch_input, dict)
            reusable_input = reusable_inputs[name]
            assert isinstance(reusable_input, dict)
            self.assertEqual(reusable_input["required"], dispatch_input["required"])
            if name != "mode" and "default" in dispatch_input:
                self.assertEqual(reusable_input["default"], dispatch_input["default"])
            self.assertEqual(reusable_input["type"], "string")

        self.assertEqual(
            self.reusable_workflow.permissions,
            {"contents": "read", "id-token": "write"},
        )
        self.assertEqual(
            self.reusable_workflow.data["concurrency"],
            {
                "group": "launchplane-product-retirement-${{ inputs.product }}-${{ inputs.instance }}",
                "cancel-in-progress": False,
            },
        )
        job = self.reusable_workflow.job("retire")
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertEqual(job["environment"], "launchplane-authz-admin")
        self.assertEqual(
            self.reusable_workflow.job_permissions("retire"),
            {"contents": "read", "id-token": "write"},
        )
        self.assertEqual(
            job["env"],
            {
                "LAUNCHPLANE_URL": "${{ vars.LAUNCHPLANE_PUBLIC_URL }}",
                "MODE": "${{ inputs.mode }}",
                "PRODUCT": "${{ inputs.product }}",
                "INSTANCE": "${{ inputs.instance }}",
                "EXPECTED_TARGET_SHA256": "${{ inputs.expected_target_sha256 }}",
                "OPERATOR_IDEMPOTENCY_KEY": "${{ inputs.operator_idempotency_key }}",
                "REASON": "${{ inputs.reason }}",
                "RELATED_ISSUE": "${{ inputs.related_issue }}",
                "REVIEWED_PLAN_RECORD_ID": "${{ inputs.reviewed_plan_record_id }}",
                "REVIEWED_PLAN_SHA256": "${{ inputs.reviewed_plan_sha256 }}",
                "CONFIRMATION": "${{ inputs.confirmation }}",
            },
        )
        self.assertEqual(
            tuple(step.name for step in self.reusable_workflow.steps("retire")),
            (
                "Validate exact retirement intent",
                "Build retirement request",
                "Request audited product retirement",
                "Verify redacted retirement evidence",
                "Upload redacted retirement evidence",
                "Summarize review evidence",
            ),
        )
        request_step = self.reusable_workflow.step_named(
            "retire", "Request audited product retirement"
        )
        self.assertIsNotNone(request_step)
        assert request_step is not None
        self.assertRegex(
            request_step.uses,
            re.compile(
                r"^cbusillo/launchplane/\.github/actions/launchplane-request@[0-9a-f]{40}$"
            ),
        )
        self.assertEqual(request_step.with_values["route-path"], "/v1/product-retirement")

        validation = self.reusable_workflow.step_named(
            "retire", "Validate exact retirement intent"
        )
        self.assertIsNotNone(validation)
        assert validation is not None
        self.assertIn('case "$MODE" in', validation.run)
        self.assertIn('plan|apply)', validation.run)
        self.assertIn("exactly bind product, instance, and target digest", validation.run)

        evidence = self.reusable_workflow.step_named(
            "retire", "Verify redacted retirement evidence"
        )
        self.assertIsNotNone(evidence)
        assert evidence is not None
        for identifier in ("target_id", "domain_ids", "provider_operation_key"):
            self.assertIn(identifier, evidence.run)

        upload = self.reusable_workflow.step_named(
            "retire", "Upload redacted retirement evidence"
        )
        self.assertIsNotNone(upload)
        assert upload is not None
        self.assertEqual(
            upload.uses,
            "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
        )
        self.assertEqual(upload.with_values["path"], "product-retirement-response.json")


if __name__ == "__main__":
    unittest.main()
