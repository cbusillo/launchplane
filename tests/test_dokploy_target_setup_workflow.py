import unittest
from typing import cast

from tests.support.workflows import YamlMapping, load_workflow


class DokployTargetSetupWorkflowTests(unittest.TestCase):
    def test_repair_domain_authority_is_explicit_and_fenced(self) -> None:
        workflow = load_workflow(".github/workflows/dokploy-target-setup.yml")
        workflow_on = cast(YamlMapping, workflow.data["on"])
        dispatch = cast(YamlMapping, workflow_on["workflow_dispatch"])
        inputs = cast(YamlMapping, dispatch["inputs"])
        operation = cast(YamlMapping, inputs["operation"])
        operation_options = cast(list[object], operation["options"])
        target_id = cast(YamlMapping, inputs["target_id"])
        expected_target = cast(YamlMapping, inputs["expected_current_provider_target_json"])

        self.assertIn("repair-domain-authority", operation_options)
        self.assertIn("repair-domain-authority", str(target_id["description"]))
        self.assertIn("repair-domain-authority", str(expected_target["description"]))

        validation = workflow.step_named("setup", "Validate domain authority repair inputs")
        self.assertIsNotNone(validation)
        assert validation is not None
        self.assertIn("EXPECTED_CURRENT_PROVIDER_TARGET_JSON", validation.run)
        self.assertIn("jq -e .", validation.run)

        request = workflow.step_named("setup", "Request Launchplane target setup")
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request.with_values["route-path"], "/v1/dokploy-targets/setup")


if __name__ == "__main__":
    unittest.main()
