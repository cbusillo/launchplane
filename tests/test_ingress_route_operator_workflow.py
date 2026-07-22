from pathlib import Path
import unittest

from tests.support.workflows import load_workflow


class IngressRouteOperatorWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dry_run_wrapper = load_workflow(".github/workflows/ingress-route-dry-run.yml")
        self.apply_wrapper = load_workflow(".github/workflows/ingress-route-apply.yml")
        self.dry_run = load_workflow(".github/workflows/reusable-ingress-route-dry-run.yml")
        self.apply = load_workflow(".github/workflows/reusable-ingress-route-apply.yml")

    def test_dispatch_wrappers_pin_and_forward_to_workers(self) -> None:
        expected = {
            "dry-run": (
                self.dry_run_wrapper,
                "cbusillo/launchplane/.github/workflows/"
                "reusable-ingress-route-dry-run.yml@"
                "b649f41982c478189aabb7c9e5a5e8649279b01b",
            ),
            "apply": (
                self.apply_wrapper,
                "cbusillo/launchplane/.github/workflows/"
                "reusable-ingress-route-apply.yml@"
                "b649f41982c478189aabb7c9e5a5e8649279b01b",
            ),
        }
        for job_name, (workflow, worker_ref) in expected.items():
            with self.subTest(job=job_name):
                self.assertEqual(workflow.job_uses(job_name), worker_ref)
                job = workflow.job(job_name)
                forwarded = job["with"]
                assert isinstance(forwarded, dict)
                self.assertEqual(forwarded["instance"], "${{ inputs.instance }}")

    def test_reusable_workers_accept_optional_exact_instance(self) -> None:
        for workflow in (self.dry_run, self.apply):
            with self.subTest(workflow=workflow.path):
                trigger = workflow.data["on"]
                assert isinstance(trigger, dict)
                self.assertEqual(set(trigger), {"workflow_call"})
                workflow_call = trigger["workflow_call"]
                assert isinstance(workflow_call, dict)
                inputs = workflow_call["inputs"]
                assert isinstance(inputs, dict)
                instance = inputs["instance"]
                assert isinstance(instance, dict)
                self.assertFalse(instance["required"])
                self.assertEqual(instance["default"], "")
                self.assertEqual(instance["type"], "string")

    def test_dry_run_wrapper_normalizes_port_for_number_input(self) -> None:
        trigger = self.dry_run_wrapper.data["on"]
        assert isinstance(trigger, dict)
        dispatch = trigger["workflow_dispatch"]
        assert isinstance(dispatch, dict)
        dispatch_inputs = dispatch["inputs"]
        assert isinstance(dispatch_inputs, dict)
        dispatch_port = dispatch_inputs["forward_port"]
        assert isinstance(dispatch_port, dict)
        self.assertEqual(dispatch_port["type"], "string")

        worker_trigger = self.dry_run.data["on"]
        assert isinstance(worker_trigger, dict)
        workflow_call = worker_trigger["workflow_call"]
        assert isinstance(workflow_call, dict)
        worker_inputs = workflow_call["inputs"]
        assert isinstance(worker_inputs, dict)
        worker_port = worker_inputs["forward_port"]
        assert isinstance(worker_port, dict)
        self.assertEqual(worker_port["type"], "number")

        wrapper_job = self.dry_run_wrapper.job("dry-run")
        forwarded = wrapper_job["with"]
        assert isinstance(forwarded, dict)
        self.assertEqual(forwarded["forward_port"], "${{ fromJSON(inputs.forward_port) }}")

    def test_reusable_workers_send_instance_to_service(self) -> None:
        dry_run_text = Path(self.dry_run.path).read_text(encoding="utf-8")
        apply_text = Path(self.apply.path).read_text(encoding="utf-8")

        for workflow_text in (dry_run_text, apply_text):
            self.assertIn("INSTANCE: ${{ inputs.instance }}", workflow_text)
            self.assertIn('--arg instance "$INSTANCE"', workflow_text)
            self.assertIn("instance: $instance", workflow_text)
            self.assertIn('echo "- Instance: ${INSTANCE:-context-scoped}"', workflow_text)

    def test_apply_worker_preserves_operator_guards(self) -> None:
        workflow_text = Path(self.apply.path).read_text(encoding="utf-8")

        self.assertIn("APPLY LAUNCHPLANE INGRESS ROUTE", workflow_text)
        self.assertIn("idempotency-key: ${{ inputs.idempotency_key }}", workflow_text)
        self.assertIn('option("allow_create"; false)', workflow_text)
        self.assertIn('option("allow_enable_disable"; false)', workflow_text)
