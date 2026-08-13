from __future__ import annotations

from pathlib import Path
import unittest

from tests.support.workflows import (
    SELF_HOSTED_RUNNER,
    launchplane_request_action_reference,
    load_workflow,
)


class ExternalRouteBindingOperatorWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load_workflow(".github/workflows/external-route-binding-reconcile.yml")
        self.worker = load_workflow(
            ".github/workflows/reusable-external-route-binding-reconcile.yml"
        )

    def test_reusable_worker_is_oidc_service_backed(self) -> None:
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
            launchplane_request_action_reference(),
        )
        self.assertEqual(current_step.with_values["method"], "GET")
        self.assertEqual(current_step.with_values["expected-status"], "200,404")
        self.assertEqual(current_step.with_values["log-response-body"], "false")

        reconcile_step = self.worker.step_named(
            "reconcile", "Request external route-binding reconcile"
        )
        self.assertIsNotNone(reconcile_step)
        assert reconcile_step is not None
        self.assertEqual(
            reconcile_step.uses,
            launchplane_request_action_reference(),
        )
        self.assertEqual(
            reconcile_step.with_values["route-path"],
            "/v1/route-bindings/external/reconcile",
        )
        self.assertEqual(reconcile_step.with_values["fail-result-paths"], "result.status")
        self.assertEqual(
            reconcile_step.with_values["fail-result-statuses"],
            "blocked,conflict",
        )

    def test_worker_discovers_current_record_and_requires_apply_guards(self) -> None:
        build_step = self.worker.step_named(
            "reconcile", "Build external route-binding reconcile request"
        )
        self.assertIsNotNone(build_step)
        assert build_step is not None
        self.assertIn(".record.record_sha256", build_step.run)
        self.assertIn('{state:"present"', build_step.run)
        self.assertIn('{"state":"absent"}', build_step.run)
        self.assertIn("expected_current: $expected_current", build_step.run)
        self.assertIn("desired_status: $desired_status", build_step.run)

        guard_step = self.worker.step_named(
            "reconcile", "Validate external route-binding apply guards"
        )
        self.assertIsNotNone(guard_step)
        assert guard_step is not None
        self.assertEqual(guard_step.data["if"], "${{ inputs.mode == 'apply' }}")
        self.assertIn("APPLY EXTERNAL ROUTE BINDING RECONCILE", guard_step.run)
        self.assertIn("idempotency_key is required for apply", guard_step.run)

    def test_worker_accepts_only_neutral_authority_inputs(self) -> None:
        workflow_call = self.worker.data["on"]
        assert isinstance(workflow_call, dict)
        workflow_call_contract = workflow_call["workflow_call"]
        assert isinstance(workflow_call_contract, dict)
        worker_inputs = workflow_call_contract["inputs"]
        assert isinstance(worker_inputs, dict)
        self.assertEqual(
            set(worker_inputs),
            {
                "mode",
                "product",
                "context",
                "instance",
                "desired_status",
                "source_label",
                "reason",
                "idempotency_key",
                "confirmation",
            },
        )
        workflow_text = Path(self.worker.path).read_text(encoding="utf-8")
        self.assertNotIn("provider_host_id", workflow_text)
        self.assertNotIn("provider_certificate_ref", workflow_text)
        self.assertNotIn("target_id", workflow_text)
        self.assertNotIn("domain_name", workflow_text)
        self.assertNotIn("upstream_host", workflow_text)

    def test_dispatch_wrapper_calls_immutable_worker(self) -> None:
        trigger = self.workflow.data["on"]
        assert isinstance(trigger, dict)
        self.assertEqual(set(trigger), {"workflow_dispatch"})
        self.assertEqual(self.workflow.permissions, {"contents": "read", "id-token": "write"})
        self.assertEqual(set(self.workflow.jobs), {"reconcile"})
        self.assertEqual(
            self.workflow.job_uses("reconcile"),
            "cbusillo/launchplane/.github/workflows/"
            "reusable-external-route-binding-reconcile.yml@"
            "33801fbf510a1ab2cf858b7be5742b90bacc22a3",
        )
        self.assertEqual(
            self.workflow.job_permissions("reconcile"),
            {"contents": "read", "id-token": "write"},
        )
        job = self.workflow.job("reconcile")
        forwarded_inputs = job["with"]
        assert isinstance(forwarded_inputs, dict)
        self.assertEqual(
            set(forwarded_inputs),
            {
                "mode",
                "product",
                "context",
                "instance",
                "desired_status",
                "source_label",
                "reason",
                "idempotency_key",
                "confirmation",
            },
        )

    def test_dispatch_wrapper_exposes_only_neutral_authority_inputs(self) -> None:
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
                "desired_status",
                "source_label",
                "reason",
                "idempotency_key",
                "confirmation",
            },
        )
        desired_status = inputs["desired_status"]
        assert isinstance(desired_status, dict)
        self.assertEqual(desired_status["options"], ["active", "disabled"])
        workflow_text = Path(self.workflow.path).read_text(encoding="utf-8")
        self.assertNotIn("provider_host_id", workflow_text)
        self.assertNotIn("provider_certificate_ref", workflow_text)
        self.assertNotIn("target_id", workflow_text)
        self.assertNotIn("domain_name", workflow_text)


if __name__ == "__main__":
    unittest.main()
