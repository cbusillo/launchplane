import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from tests.support.workflows import load_workflow


ROOT = Path(__file__).resolve().parents[1]


class ProductionBackupPromotionWorkflowTests(unittest.TestCase):
    def test_promotion_steps_wait_for_durable_capture_and_fail_on_cancellation(self) -> None:
        for filename, promotion_step_id in (
            ("reusable-product-driver-prod-promotion.yml", "lp_odoo"),
            ("reusable-generic-web-prod-promotion.yml", "lp"),
        ):
            with self.subTest(workflow=filename):
                workflow = load_workflow(ROOT / ".github/workflows" / filename)
                steps = {step.data.get("id"): step for step in workflow.steps("prod-promotion")}
                capture = steps["infrastructure_backup"]
                self.assertLess(capture.index, steps[promotion_step_id].index)
                self.assertEqual(capture.with_values["route-path"], "/v1/production-backup-gates")
                self.assertEqual(capture.with_values["poll-result-path"], "operation_status")
                self.assertEqual(capture.with_values["poll-result-statuses"], "pending,running")
                self.assertEqual(capture.with_values["fail-result-paths"], "operation_status")
                self.assertEqual(capture.with_values["fail-result-statuses"], "fail,cancelled")
                self.assertIn("idempotency-key", capture.with_values)
        odoo = load_workflow(ROOT / ".github/workflows/reusable-product-driver-prod-promotion.yml")
        step = odoo.step_named("prod-promotion", "Request Launchplane Odoo prod promotion")
        assert step is not None
        self.assertIn(
            "run.infrastructure_backup_record_id=${{ steps.infrastructure_backup.outputs.backup_record_id }}",
            str(step.with_values["payload-fields"]),
        )

    def test_generic_web_resolves_stored_context_and_passes_capture_identity(self) -> None:
        workflow = load_workflow(ROOT / ".github/workflows/reusable-generic-web-prod-promotion.yml")
        resolve = workflow.step_named("prod-promotion", "Resolve production backup capture request")
        payload = workflow.step_named(
            "prod-promotion", "Write Launchplane generic-web promotion payload"
        )
        assert resolve is not None and payload is not None
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".launchplane").mkdir()
            profile = {
                "product": "example-product",
                "lanes": [
                    {"instance": "testing", "context": "different-testing-context"},
                    {"instance": "prod", "context": "stored-production-context"},
                ],
            }
            (root / ".launchplane/production-backup-profile.json").write_text(
                json.dumps({"profile": profile})
            )
            env = {
                **os.environ,
                "PRODUCT": "example-product",
                "TO_INSTANCE": "prod",
                "GITHUB_RUN_ID": "12345",
                "GITHUB_RUN_ATTEMPT": "1",
            }
            subprocess.run(
                ["bash", "-c", resolve.run], cwd=root, env=env, check=True, capture_output=True
            )
            capture = json.loads((root / ".launchplane/production-backup-request.json").read_text())
            self.assertEqual(capture["context"], "stored-production-context")
            self.assertEqual(capture["instance"], "prod")
            env.update(
                {
                    "BACKUP_RECORD_ID": capture["backup_record_id"],
                    "DRY_RUN": "false",
                    "FROM_INSTANCE": "testing",
                    "VERIFY_HEALTH": "true",
                    "NO_CACHE": "false",
                    "HEALTH_TIMEOUT_SECONDS": "120",
                    "TIMEOUT_SECONDS": "300",
                }
            )
            subprocess.run(
                ["bash", "-c", payload.run], cwd=root, env=env, check=True, capture_output=True
            )
            promotion = json.loads(
                (root / ".launchplane/generic-web-prod-promotion-payload.json").read_text()
            )["promotion"]
            self.assertEqual(promotion["backup_record_id"], capture["backup_record_id"])
            self.assertFalse(promotion["dry_run"])
