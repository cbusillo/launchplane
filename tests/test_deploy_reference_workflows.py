import unittest
from pathlib import Path

from tests.support.workflows import load_workflow, workflow_call_inputs


REPO_ROOT = Path(__file__).resolve().parents[1]


class DeployReferenceWorkflowContractTests(unittest.TestCase):
    def test_stable_deploy_workflows_accept_and_forward_deploy_reference(self) -> None:
        for workflow_name, request_step_name in (
            (
                "reusable-generic-web-stable-deploy.yml",
                "Request Launchplane generic-web stable deploy",
            ),
            (
                "reusable-product-driver-stable-deploy.yml",
                "Request Launchplane stable deploy",
            ),
        ):
            with self.subTest(workflow=workflow_name):
                workflow = load_workflow(REPO_ROOT / ".github/workflows" / workflow_name)
                self.assertIn("deploy_reference", workflow_call_inputs(workflow))
                request_step = workflow.step_named("stable-deploy", request_step_name)
                self.assertIsNotNone(request_step)
                assert request_step is not None
                payload_fields = request_step.with_values["payload-fields"]
                self.assertIsInstance(payload_fields, str)
                assert isinstance(payload_fields, str)
                self.assertIn(
                    "deploy.deploy_reference=${{ inputs.deploy_reference }}",
                    payload_fields,
                )

        generic_workflow = load_workflow(
            REPO_ROOT / ".github/workflows/reusable-generic-web-stable-deploy.yml"
        )
        resolve_step = generic_workflow.step_named(
            "stable-deploy", "Resolve Launchplane deploy request"
        )
        self.assertIsNotNone(resolve_step)
        assert resolve_step is not None
        self.assertIn('"$ARTIFACT_ID"', resolve_step.run)
        self.assertIn('"$DEPLOY_REFERENCE"', resolve_step.run)

    def test_generic_stable_deploy_reruns_reuse_initial_operation_identity(self) -> None:
        workflow = load_workflow(
            REPO_ROOT / ".github/workflows/reusable-generic-web-stable-deploy.yml"
        )
        resolve_step = workflow.step_named("stable-deploy", "Resolve Launchplane deploy request")
        self.assertIsNotNone(resolve_step)
        assert resolve_step is not None
        self.assertIn('run_scope="${GITHUB_RUN_ID}:1"', resolve_step.run)
        self.assertNotIn("GITHUB_RUN_ATTEMPT", resolve_step.run)

    def test_prod_promotion_workflows_accept_and_forward_deploy_reference(self) -> None:
        workflow = load_workflow(
            REPO_ROOT / ".github/workflows/reusable-product-driver-prod-promotion.yml"
        )
        self.assertIn("deploy_reference", workflow_call_inputs(workflow))
        request_step = workflow.step_named(
            "prod-promotion", "Request Launchplane VeriReel prod promotion"
        )
        self.assertIsNotNone(request_step)
        assert request_step is not None
        payload_fields = request_step.with_values["payload-fields"]
        self.assertIsInstance(payload_fields, str)
        assert isinstance(payload_fields, str)
        self.assertIn(
            "promotion.deploy_reference=${{ inputs.deploy_reference }}",
            payload_fields,
        )

        generic_workflow = load_workflow(
            REPO_ROOT / ".github/workflows/reusable-generic-web-prod-promotion.yml"
        )
        self.assertIn("deploy_reference", workflow_call_inputs(generic_workflow))
        payload_step = generic_workflow.step_named(
            "prod-promotion", "Write Launchplane generic-web promotion payload"
        )
        self.assertIsNotNone(payload_step)
        assert payload_step is not None
        self.assertIn("deploy_reference", payload_step.run)
        self.assertIn("DEPLOY_REFERENCE", payload_step.run)


if __name__ == "__main__":
    unittest.main()
