from __future__ import annotations

import unittest

from tests.support.workflows import load_workflow


class TrackedTargetLogsOperatorWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load_workflow(".github/workflows/tracked-target-logs.yml")

    def test_dispatch_contract_includes_exact_compose_service_selector(self) -> None:
        trigger = self.workflow.data["on"]
        assert isinstance(trigger, dict)
        workflow_dispatch = trigger["workflow_dispatch"]
        assert isinstance(workflow_dispatch, dict)
        inputs = workflow_dispatch["inputs"]
        assert isinstance(inputs, dict)
        self.assertEqual(
            set(inputs),
            {"context", "instance", "lines", "source", "since", "search", "service"},
        )
        service_input = inputs["service"]
        assert isinstance(service_input, dict)
        self.assertEqual(service_input["default"], "")
        self.assertEqual(self.workflow.permissions, {"contents": "read", "id-token": "write"})

    def test_service_selector_is_validated_and_forwarded(self) -> None:
        validation_step = self.workflow.step_named("read", "Validate inputs")
        route_step = self.workflow.step_named("read", "Build logs route")
        self.assertIsNotNone(validation_step)
        self.assertIsNotNone(route_step)
        assert validation_step is not None
        assert route_step is not None
        self.assertIn("deployment logs do not support compose service", validation_step.run)
        self.assertIn("exact lowercase Compose service name", validation_step.run)
        self.assertIn('--arg service "$SERVICE"', route_step.run)
        self.assertIn('"&service=\\(.service | @uri)"', route_step.run)


if __name__ == "__main__":
    unittest.main()
