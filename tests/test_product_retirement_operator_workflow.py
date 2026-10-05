import subprocess
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.support.workflows import load_workflow


class ProductRetirementOperatorWorkflowTests(unittest.TestCase):
    def test_no_target_worker_validates_and_builds_the_exact_service_intent(self) -> None:
        worker = load_workflow(".github/workflows/reusable-product-retirement.yml")
        validate = worker.step_named("retire", "Validate exact retirement intent")
        build = worker.step_named("retire", "Build retirement request")
        assert validate is not None and build is not None
        env = {
            **os.environ,
            "LAUNCHPLANE_URL": "https://service.invalid",
            "PRODUCT": "example-site",
            "INSTANCE": "testing",
            "MODE": "apply",
            "NO_TARGET": "true",
            "EXPECTED_TARGET_SHA256": "",
            "OPERATOR_IDEMPOTENCY_KEY": "retire-example",
            "REASON": "Retire unused records.",
            "RELATED_ISSUE": "example/repo#1",
            "REVIEWED_PLAN_RECORD_ID": "reviewed-plan",
            "REVIEWED_PLAN_SHA256": "a" * 64,
            "CONFIRMATION": "retire product example-site instance testing with no target",
        }
        with TemporaryDirectory() as directory:
            checked = subprocess.run(
                ["bash", "-c", validate.run], cwd=directory, env=env, capture_output=True, text=True
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
            built = subprocess.run(
                ["bash", "-c", build.run], cwd=directory, env=env, capture_output=True, text=True
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            payload = json.loads((Path(directory) / "product-retirement-request.json").read_text())
            from control_plane.contracts.product_retirement import ProductRetirementRequest

            intent = ProductRetirementRequest.model_validate(payload)
            self.assertTrue(intent.no_target)
            self.assertEqual(intent.confirmation, intent.expected_confirmation)
            for changes in (
                {"CONFIRMATION": "retire an application"},
                {"EXPECTED_TARGET_SHA256": "b" * 64},
                {"NO_TARGET": "false"},
            ):
                rejected = subprocess.run(
                    ["bash", "-c", validate.run],
                    cwd=directory,
                    env={**env, **changes},
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(rejected.returncode, 0)
