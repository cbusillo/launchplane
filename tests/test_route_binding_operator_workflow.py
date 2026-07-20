from __future__ import annotations

from pathlib import Path
import unittest

from tests.support.workflows import SELF_HOSTED_RUNNER, load_workflow


class RouteBindingOperatorWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load_workflow(".github/workflows/route-binding-reconcile.yml")
        self.worker = load_workflow(".github/workflows/reusable-route-binding-reconcile.yml")

    def test_reusable_worker_is_manual_oidc_and_service_backed(self) -> None:
        trigger = self.worker.data["on"]
        assert isinstance(trigger, dict)
        self.assertEqual(set(trigger), {"workflow_call"})
        self.assertEqual(self.worker.permissions, {"contents": "read", "id-token": "write"})
        self.assertEqual(set(self.worker.jobs), {"reconcile"})
        job = self.worker.job("reconcile")
        runs_on = job["runs-on"]
        assert isinstance(runs_on, list)
        self.assertEqual(tuple(runs_on), SELF_HOSTED_RUNNER)

        current_step = self.worker.step_named("reconcile", "Read current route binding")
        self.assertIsNotNone(current_step)
        assert current_step is not None
        self.assertEqual(
            current_step.uses,
            "cbusillo/launchplane/.github/actions/launchplane-request@"
            "adcf937c6aef14e02478724040852d1d2a82a850",
        )
        self.assertEqual(current_step.with_values["method"], "GET")
        self.assertEqual(current_step.with_values["expected-status"], "200,404")
        self.assertEqual(current_step.with_values["log-response-body"], "false")

        reconcile_step = self.worker.step_named("reconcile", "Request route-binding reconcile")
        self.assertIsNotNone(reconcile_step)
        assert reconcile_step is not None
        self.assertEqual(
            reconcile_step.uses,
            "cbusillo/launchplane/.github/actions/launchplane-request@"
            "adcf937c6aef14e02478724040852d1d2a82a850",
        )
        self.assertEqual(
            reconcile_step.with_values["route-path"],
            "/v1/route-bindings/reconcile",
        )
        self.assertEqual(reconcile_step.with_values["fail-result-paths"], "result.status")
        self.assertEqual(
            reconcile_step.with_values["fail-result-statuses"],
            "blocked,conflict",
        )

    def test_workflow_is_manual_oidc_and_service_backed(self) -> None:
        trigger = self.workflow.data["on"]
        assert isinstance(trigger, dict)
        self.assertEqual(set(trigger), {"workflow_dispatch"})
        self.assertEqual(self.workflow.permissions, {"contents": "read", "id-token": "write"})
        self.assertEqual(set(self.workflow.jobs), {"reconcile"})
        job = self.workflow.job("reconcile")
        runs_on = job["runs-on"]
        assert isinstance(runs_on, list)
        self.assertEqual(tuple(runs_on), SELF_HOSTED_RUNNER)

        current_step = self.workflow.step_named("reconcile", "Read current route binding")
        self.assertIsNotNone(current_step)
        assert current_step is not None
        self.assertEqual(current_step.uses, "./.github/actions/launchplane-request")
        self.assertEqual(current_step.with_values["method"], "GET")
        self.assertEqual(current_step.with_values["expected-status"], "200,404")
        self.assertEqual(current_step.with_values["log-response-body"], "false")

        reconcile_step = self.workflow.step_named("reconcile", "Request route-binding reconcile")
        self.assertIsNotNone(reconcile_step)
        assert reconcile_step is not None
        self.assertEqual(reconcile_step.uses, "./.github/actions/launchplane-request")
        self.assertEqual(
            reconcile_step.with_values["route-path"],
            "/v1/route-bindings/reconcile",
        )
        self.assertEqual(reconcile_step.with_values["fail-result-paths"], "result.status")
        self.assertEqual(
            reconcile_step.with_values["fail-result-statuses"],
            "blocked,conflict",
        )

    def test_workflow_discovers_expected_current_and_requires_apply_guards(self) -> None:
        build_step = self.workflow.step_named("reconcile", "Build route-binding reconcile request")
        self.assertIsNotNone(build_step)
        assert build_step is not None
        self.assertIn(".record.record_sha256", build_step.run)
        self.assertIn('{state:"present"', build_step.run)
        self.assertIn('{"state":"absent"}', build_step.run)
        self.assertIn("expected_current: $expected_current", build_step.run)

        guard_step = self.workflow.step_named("reconcile", "Validate route-binding apply guards")
        self.assertIsNotNone(guard_step)
        assert guard_step is not None
        self.assertEqual(guard_step.data["if"], "${{ inputs.mode == 'apply' }}")
        self.assertIn("APPLY LAUNCHPLANE ROUTE BINDING RECONCILE", guard_step.run)
        self.assertIn("idempotency_key is required for apply", guard_step.run)

    def test_workflow_accepts_only_neutral_identity_inputs(self) -> None:
        workflow_text = Path(self.workflow.path).read_text(encoding="utf-8")
        dispatch = self.workflow.data["on"]
        assert isinstance(dispatch, dict)
        workflow_dispatch = dispatch["workflow_dispatch"]
        assert isinstance(workflow_dispatch, dict)
        inputs = workflow_dispatch["inputs"]
        assert isinstance(inputs, dict)
        self.assertEqual(
            set(inputs),
            {
                "mode",
                "product",
                "context",
                "instance",
                "source_label",
                "reason",
                "idempotency_key",
                "confirmation",
            },
        )
        self.assertNotIn("provider_host_id", workflow_text)
        self.assertNotIn("provider_certificate_ref", workflow_text)
        self.assertNotIn("target_id", workflow_text)
        self.assertNotIn("domain_name", workflow_text)


if __name__ == "__main__":
    unittest.main()
