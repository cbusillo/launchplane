from __future__ import annotations

import unittest
from collections.abc import Mapping

from tests.support.workflows import YamlValue, load_workflow


def _mapping(value: YamlValue) -> Mapping[str, YamlValue]:
    assert isinstance(value, dict)
    return value


class DokployRuntimeEvidenceWorkflowTests(unittest.TestCase):
    def test_manual_inspect_supports_bounded_runtime_proof(self) -> None:
        workflow = load_workflow(".github/workflows/dokploy-target-inspect.yml")
        dispatch = _mapping(_mapping(workflow.data["on"])["workflow_dispatch"])
        inputs = _mapping(dispatch["inputs"])

        self.assertIn("service", inputs)
        self.assertIn("event", inputs)
        self.assertIn("expected_image", inputs)
        self.assertEqual(workflow.permissions, {"contents": "read", "id-token": "write"})

        build_request = workflow.step_named("inspect", "Build inspect request")
        check_response = workflow.step_named("inspect", "Check inspect response")
        upload_evidence = workflow.step_named("inspect", "Upload inspect evidence")
        self.assertIsNotNone(build_request)
        self.assertIsNotNone(check_response)
        self.assertIsNotNone(upload_evidence)
        assert build_request is not None
        assert check_response is not None
        assert upload_evidence is not None
        self.assertIn('encpair("service"; $service)', build_request.run)
        self.assertIn('encpair("event"; $event)', build_request.run)
        self.assertIn('encpair("expected_image"; $expected_image)', build_request.run)
        self.assertIn("runtime_evidence.proof_ready", check_response.run)
        validate_inputs = workflow.step_named("inspect", "Validate inspect inputs")
        self.assertIsNotNone(validate_inputs)
        assert validate_inputs is not None
        self.assertIn("runtime proof requires the expected immutable image", validate_inputs.run)
        self.assertNotIn("requires the structured event name", validate_inputs.run)
        self.assertEqual(upload_evidence.data.get("if"), "always()")


if __name__ == "__main__":
    unittest.main()
