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
                "878e6a317cfbd028c89d49cfa4ce34553aac0123",
            ),
            "apply": (
                self.apply_wrapper,
                "cbusillo/launchplane/.github/workflows/"
                "reusable-ingress-route-apply.yml@"
                "878e6a317cfbd028c89d49cfa4ce34553aac0123",
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
