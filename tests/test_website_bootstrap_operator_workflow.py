import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.support.workflows import load_workflow


class WebsiteBootstrapOperatorWorkflowTests(unittest.TestCase):
    def test_existing_worker_applies_and_verifies_requested_bootstrap_presence(self) -> None:
        worker = load_workflow(".github/workflows/reusable-odoo-website-bootstrap-override.yml")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            env = {
                **os.environ,
                "PRODUCT": "example-product",
                "CONTEXT_NAME": "example",
                "INSTANCE": "testing",
                "GITHUB_OUTPUT": str(root / "outputs"),
                "GITHUB_RUN_ID": "1",
                "GITHUB_RUN_ATTEMPT": "1",
                "STATUS_CODE": "202",
            }
            for bootstrap in (None, {"name": "Example", "homepage_url": "/example"}):
                env["WEBSITE_BOOTSTRAP_PAYLOAD"] = json.dumps(bootstrap)
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
                self.assertEqual(request["override"]["website_bootstrap"], bootstrap)
                response_path = root / "odoo-website-bootstrap-override.json"
                result = {"website_bootstrap": bootstrap is not None}
                response_path.write_text(json.dumps({"status": "accepted", "result": result}))
                verify = worker.step_named("write", "Verify website bootstrap override response")
                assert verify is not None
                accepted = subprocess.run(
                    ["bash", "-c", verify.run], cwd=root, env=env, capture_output=True, text=True
                )
                self.assertEqual(accepted.returncode, 0, accepted.stderr)
                result["website_bootstrap"] = not result["website_bootstrap"]
                response_path.write_text(json.dumps({"status": "accepted", "result": result}))
                rejected = subprocess.run(
                    ["bash", "-c", verify.run], cwd=root, env=env, capture_output=True, text=True
                )
                self.assertNotEqual(rejected.returncode, 0)
