import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from tests.support.workflows import Workflow, load_workflow


ROOT = Path(__file__).resolve().parents[1]


def prepare_workflow_workspace(workflow: Workflow, root: Path, product: str) -> dict[str, str]:
    initial = next(
        step for step in workflow.steps("prod-promotion") if step.data.get("id") == "request"
    )
    env = {
        "PATH": os.environ["PATH"],
        "PRODUCT": product,
        "CONTEXT": product,
        "DRIVER": "odoo",
        "FROM_INSTANCE": "testing",
        "TO_INSTANCE": "prod",
        "ARTIFACT_ID": "",
        "BACKUP_RECORD_ID": "",
        "DEPLOY_REFERENCE": "",
        "PROMOTION_RECORD_ID": "",
        "SOURCE_GIT_REF": "",
        "GITHUB_RUN_ID": "12345",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_OUTPUT": str(root / "outputs"),
    }
    subprocess.run(["bash", "-c", initial.run], cwd=root, env=env, check=True, capture_output=True)
    return env


class ProductionBackupPromotionWorkflowTests(unittest.TestCase):
    def test_promotion_steps_wait_for_durable_capture_and_fail_on_cancellation(self) -> None:
        for filename, promotion_step_id in (
            ("reusable-product-driver-prod-promotion.yml", "lp_odoo"),
            ("reusable-generic-web-prod-promotion.yml", "lp"),
        ):
            with self.subTest(workflow=filename):
                workflow = load_workflow(ROOT / ".github/workflows" / filename)
                steps = {
                    str(step.data.get("id")): step for step in workflow.steps("prod-promotion")
                }
                capture = steps["infrastructure_backup"]
                self.assertLess(steps["release_approval"].index, capture.index)
                self.assertEqual(steps["release_approval"].data["if"], capture.data["if"])
                self.assertLess(capture.index, steps[promotion_step_id].index)
                self.assertEqual(capture.with_values["route-path"], "/v1/production-backup-gates")
                self.assertEqual(capture.with_values["poll-result-path"], "operation_status")
                self.assertEqual(capture.with_values["poll-result-statuses"], "pending,running")
                self.assertEqual(capture.with_values["poll-retry-on-request-error"], "true")
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

    def test_unapproved_or_missing_release_stops_before_capture(self) -> None:
        for filename in (
            "reusable-product-driver-prod-promotion.yml",
            "reusable-generic-web-prod-promotion.yml",
        ):
            workflow = load_workflow(ROOT / ".github/workflows" / filename)
            step = workflow.step_named("prod-promotion", "Require accepted release before backup")
            assert step is not None
            with TemporaryDirectory() as directory:
                root = Path(directory)
                env = prepare_workflow_workspace(workflow, root, "example")
                for review, expected_success in (
                    ({}, False),
                    ({"approved": False}, False),
                    ({"approved": True}, True),
                    ({"required": False, "approved": False}, True),
                    ({"required": True, "approved": False}, False),
                    ({"required": "false", "approved": False}, False),
                ):
                    with self.subTest(workflow=filename, review=review):
                        (root / ".launchplane/promotion-release-review.json").write_text(
                            json.dumps({"product": "example", "review": review})
                        )
                        result = subprocess.run(
                            ["bash", "-c", step.run],
                            cwd=root,
                            env=env,
                            capture_output=True,
                        )
                        self.assertEqual(result.returncode == 0, expected_success)

    def test_generic_web_resolves_stored_context_and_passes_capture_identity(self) -> None:
        workflow = load_workflow(ROOT / ".github/workflows/reusable-generic-web-prod-promotion.yml")
        resolve = workflow.step_named("prod-promotion", "Resolve production backup capture request")
        payload = workflow.step_named(
            "prod-promotion", "Write Launchplane generic-web promotion payload"
        )
        assert resolve is not None and payload is not None
        with TemporaryDirectory() as directory:
            root = Path(directory)
            env = prepare_workflow_workspace(workflow, root, "example-product")
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
