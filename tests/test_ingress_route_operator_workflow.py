from pathlib import Path
import unittest

from tests.support.workflows import load_workflow


class IngressRouteOperatorWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dry_run = load_workflow(
            ".github/workflows/reusable-ingress-route-dry-run.yml"
        )
        self.apply = load_workflow(
            ".github/workflows/reusable-ingress-route-apply.yml"
        )

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
