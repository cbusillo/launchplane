import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.support.workflows import load_workflow


class WebsiteBootstrapOperatorWorkflowTests(unittest.TestCase):
    def test_existing_worker_plans_and_applies_explicit_withdrawal(self) -> None:
        worker = load_workflow(".github/workflows/reusable-odoo-website-bootstrap-override.yml")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            env = {
                **os.environ,
                "PRODUCT": "example-product",
                "CONTEXT_NAME": "example",
                "INSTANCE": "testing",
                "WEBSITE_BOOTSTRAP_PAYLOAD": "null",
                "GITHUB_OUTPUT": str(root / "outputs"),
                "GITHUB_RUN_ID": "1",
                "GITHUB_RUN_ATTEMPT": "1",
                "STATUS_CODE": "202",
            }
            for mode in ("dry-run", "apply"):
                env["MODE"] = mode
                for name in (
                    "Validate runtime inputs",
                    "Write website bootstrap override payload",
                    "Write redacted request evidence",
                ):
                    step = worker.step_named("write", name)
                    assert step is not None
                    completed = subprocess.run(
                        ["bash", "-c", step.run], cwd=root, env=env, capture_output=True, text=True
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                request = json.loads(
                    (root / ".launchplane/odoo-website-bootstrap-override-payload.json").read_text()
                )
                self.assertIsNone(request["override"]["website_bootstrap"])
                self.assertEqual(request["override"]["mode"], mode)
                response_path = root / "odoo-website-bootstrap-override.json"
                result = {
                    "website_bootstrap": False,
                    "applied": False,
                    "planned_website_bootstrap": None,
                }
                response_path.write_text(json.dumps({"status": "accepted", "result": result}))
                verify = worker.step_named("write", "Verify website bootstrap override response")
                assert verify is not None
                accepted = subprocess.run(
                    ["bash", "-c", verify.run], cwd=root, env=env, capture_output=True, text=True
                )
                self.assertEqual(accepted.returncode, 0, accepted.stderr)
                result["website_bootstrap"] = True
                response_path.write_text(json.dumps({"status": "accepted", "result": result}))
                rejected = subprocess.run(
                    ["bash", "-c", verify.run], cwd=root, env=env, capture_output=True, text=True
                )
                self.assertNotEqual(rejected.returncode, 0)
