from __future__ import annotations

from pathlib import Path
import unittest

from tests.support.workflows import SELF_HOSTED_RUNNER, load_workflow


class RouteBindingRefreshWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load_workflow(".github/workflows/odoo-testing-route-binding-refresh.yml")
        self.worker = load_workflow(
            ".github/workflows/reusable-odoo-testing-route-binding-refresh.yml"
        )

    def test_wrapper_schedules_apply_and_defaults_manual_dispatch_to_dry_run(self) -> None:
        trigger = self.workflow.data["on"]
        assert isinstance(trigger, dict)
        self.assertEqual(set(trigger), {"schedule", "workflow_dispatch"})
        schedule = trigger["schedule"]
        assert isinstance(schedule, list)
        self.assertEqual(schedule, [{"cron": "37 */6 * * *"}])
        dispatch = trigger["workflow_dispatch"]
        assert isinstance(dispatch, dict)
        inputs = dispatch["inputs"]
        assert isinstance(inputs, dict)
        self.assertEqual(set(inputs), {"mode", "reason"})
        mode_input = inputs["mode"]
        assert isinstance(mode_input, dict)
        self.assertEqual(mode_input["default"], "dry-run")
        self.assertEqual(mode_input["options"], ["dry-run", "apply"])

    def test_wrapper_calls_immutable_worker_without_target_inputs(self) -> None:
        self.assertEqual(
            self.workflow.permissions,
            {"contents": "read", "id-token": "write"},
        )
        self.assertEqual(set(self.workflow.jobs), {"refresh"})
        self.assertEqual(
            self.workflow.job_uses("refresh"),
            "cbusillo/launchplane/.github/workflows/"
            "reusable-odoo-testing-route-binding-refresh.yml@"
            "f73e4616a6cbca2329ec6adaa0c76e4b3b3daf4b",
        )
        self.assertEqual(
            self.workflow.job_permissions("refresh"),
            {"contents": "read", "id-token": "write"},
        )
        forwarded_inputs = self.workflow.job("refresh")["with"]
        assert isinstance(forwarded_inputs, dict)
        self.assertEqual(set(forwarded_inputs), {"mode", "reason", "idempotency_key"})
        forwarded_mode = forwarded_inputs["mode"]
        assert isinstance(forwarded_mode, str)
        self.assertIn("github.event_name == 'schedule'", forwarded_mode)
        self.assertIn("'apply'", forwarded_mode)

        trigger = self.workflow.data["on"]
        assert isinstance(trigger, dict)
        dispatch = trigger["workflow_dispatch"]
        assert isinstance(dispatch, dict)
        inputs = dispatch["inputs"]
        assert isinstance(inputs, dict)
        self.assertNotIn("product", inputs)
        self.assertNotIn("context", inputs)
        self.assertNotIn("instance", inputs)

    def test_worker_is_testing_only_service_controller_transport(self) -> None:
        trigger = self.worker.data["on"]
        assert isinstance(trigger, dict)
        self.assertEqual(set(trigger), {"workflow_call"})
        workflow_call = trigger["workflow_call"]
        assert isinstance(workflow_call, dict)
        inputs = workflow_call["inputs"]
        assert isinstance(inputs, dict)
        self.assertEqual(set(inputs), {"mode", "reason", "idempotency_key"})
        self.assertEqual(self.worker.permissions, {"contents": "read", "id-token": "write"})
        self.assertEqual(set(self.worker.jobs), {"refresh"})
        job = self.worker.job("refresh")
        runs_on = job["runs-on"]
        assert isinstance(runs_on, list)
        self.assertEqual(tuple(runs_on), SELF_HOSTED_RUNNER)
        self.assertEqual(
            job["concurrency"],
            {
                "group": "launchplane-odoo-testing-route-binding-refresh-controller",
                "cancel-in-progress": False,
            },
        )

    def test_worker_calls_bounded_controller_without_target_inputs(self) -> None:
        request_step = self.worker.step_named(
            "refresh",
            "Run Odoo testing route-binding refresh",
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
            "/v1/route-bindings/odoo-testing/controller/run-once",
        )
        self.assertNotIn("fail-result-paths", request_step.with_values)
        self.assertNotIn("fail-result-statuses", request_step.with_values)

        enforce_step = self.worker.step_named("refresh", "Enforce refresh outcome")
        self.assertIsNotNone(enforce_step)
        assert enforce_step is not None
        self.assertIn("attention", enforce_step.run)

        upload_step = self.worker.step_named("refresh", "Upload refresh evidence")
        self.assertIsNotNone(upload_step)
        assert upload_step is not None
        self.assertEqual(upload_step.data["if"], "always()")

        workflow_text = Path(self.worker.path).read_text(encoding="utf-8")
        self.assertNotIn("product:", workflow_text)
        self.assertNotIn("context:", workflow_text)
        self.assertNotIn("instance:", workflow_text)

    def test_worker_builds_exact_apply_confirmation_internally(self) -> None:
        build_step = self.worker.step_named("refresh", "Build refresh request")
        self.assertIsNotNone(build_step)
        assert build_step is not None
        self.assertIn("APPLY ODOO TESTING ROUTE BINDING REFRESH", build_step.run)
        self.assertIn("schema_version: 1", build_step.run)


if __name__ == "__main__":
    unittest.main()
