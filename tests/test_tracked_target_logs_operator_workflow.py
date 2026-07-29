from __future__ import annotations

import unittest

from tests.support.workflows import SELF_HOSTED_RUNNER, load_workflow


class TrackedTargetLogsOperatorWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load_workflow(".github/workflows/tracked-target-logs.yml")
        self.worker = load_workflow(".github/workflows/reusable-tracked-target-logs.yml")

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
        job = self.workflow.job("read")
        self.assertEqual(
            job["uses"],
            "cbusillo/launchplane/.github/workflows/reusable-tracked-target-logs.yml@b62941a9219f69a2575c1bce1a8d7fcb8c605f3c",
        )
        self.assertEqual(
            self.workflow.job_permissions("read"),
            {"contents": "read", "id-token": "write"},
        )
        self.assertEqual(
            job["with"],
            {
                "context": "${{ inputs.context }}",
                "instance": "${{ inputs.instance }}",
                "lines": "${{ inputs.lines }}",
                "source": "${{ inputs.source }}",
                "since": "${{ inputs.since }}",
                "search": "${{ inputs.search }}",
                "service": "${{ inputs.service }}",
            },
        )

    def test_service_selector_is_validated_and_forwarded(self) -> None:
        validation_step = self.worker.step_named("read", "Validate inputs")
        route_step = self.worker.step_named("read", "Build logs route")
        self.assertIsNotNone(validation_step)
        self.assertIsNotNone(route_step)
        assert validation_step is not None
        assert route_step is not None
        self.assertIn("deployment logs do not support compose service", validation_step.run)
        self.assertIn("exact lowercase Compose service name", validation_step.run)
        self.assertIn('--arg service "$SERVICE"', route_step.run)
        self.assertIn('"&service=\\(.service | @uri)"', route_step.run)

    def test_reusable_worker_preserves_redacted_log_read_contract(self) -> None:
        trigger = self.worker.data["on"]
        assert isinstance(trigger, dict)
        workflow_call = trigger["workflow_call"]
        assert isinstance(workflow_call, dict)
        inputs = workflow_call["inputs"]
        assert isinstance(inputs, dict)
        self.assertEqual(
            set(inputs),
            {
                "context",
                "instance",
                "lines",
                "source",
                "since",
                "search",
                "service",
            },
        )
        runs_on = self.worker.job("read")["runs-on"]
        assert isinstance(runs_on, list)
        self.assertEqual(tuple(runs_on), SELF_HOSTED_RUNNER)
        self.assertEqual(
            self.worker.job_permissions("read"),
            {"contents": "read", "id-token": "write"},
        )
        request_step = self.worker.step_named("read", "Read tracked target logs")
        summary_step = self.worker.step_named("read", "Show redacted log summary")
        upload_step = self.worker.step_named("read", "Upload tracked target logs")
        artifact_step = self.worker.step_named("read", "Build artifact name")
        self.assertIsNotNone(request_step)
        self.assertIsNotNone(summary_step)
        self.assertIsNotNone(upload_step)
        self.assertIsNotNone(artifact_step)
        assert request_step is not None
        assert summary_step is not None
        assert upload_step is not None
        assert artifact_step is not None
        self.assertEqual(
            request_step.uses,
            "cbusillo/launchplane/.github/actions/launchplane-request@adcf937c6aef14e02478724040852d1d2a82a850",
        )
        self.assertEqual(request_step.with_values["method"], "GET")
        self.assertEqual(request_step.with_values["log-response-body"], False)
        self.assertEqual(
            request_step.with_values["launchplane-url"],
            "${{ env.LAUNCHPLANE_SERVICE_URL }}",
        )
        self.assertEqual(
            request_step.with_values["audience"],
            "${{ env.LAUNCHPLANE_SERVICE_AUDIENCE }}",
        )
        self.assertFalse(
            any(step.uses.startswith("actions/checkout@") for step in self.worker.steps("read"))
        )
        self.assertIn("line_count", summary_step.run)
        self.assertIn("redacted", summary_step.run)
        upload_path = str(upload_step.with_values["path"])
        self.assertIn("tracked-target-logs-request.json", upload_path)
        self.assertIn("tracked-target-logs.json", upload_path)
        self.assertIn("sed -E", artifact_step.run)
        artifact_name = str(upload_step.with_values["name"])
        self.assertIn("steps.artifact.outputs.name", artifact_name)
        self.assertIn("github.run_id", artifact_name)
        self.assertIn("github.run_attempt", artifact_name)
        self.assertNotIn("inputs.context", str(upload_step.with_values["name"]))
        self.assertEqual(upload_step.with_values["retention-days"], 30)

    def test_reusable_worker_preserves_failure_evidence_before_failing(self) -> None:
        request_step = self.worker.step_named("read", "Read tracked target logs")
        upload_step = self.worker.step_named("read", "Upload tracked target logs")
        fail_step = self.worker.step_named("read", "Fail when tracked target log read failed")
        self.assertIsNotNone(request_step)
        self.assertIsNotNone(upload_step)
        self.assertIsNotNone(fail_step)
        assert request_step is not None
        assert upload_step is not None
        assert fail_step is not None
        self.assertEqual(request_step.data["continue-on-error"], True)
        self.assertEqual(
            upload_step.data["if"],
            "${{ always() && steps.route.outcome == 'success' }}",
        )
        self.assertEqual(upload_step.with_values["if-no-files-found"], "error")
        self.assertEqual(fail_step.data["if"], "${{ steps.request.outcome == 'failure' }}")


if __name__ == "__main__":
    unittest.main()
