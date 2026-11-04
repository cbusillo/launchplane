import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from tests.support.workflows import load_workflow


class SetupAllocationWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "outputs"

    def run_step(
        self, workflow: str, job: str, name: str, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        step = load_workflow(Path(".github/workflows") / workflow).step_named(job, name)
        assert step is not None
        return subprocess.run(
            ["bash", "-c", step.run],
            cwd=self.root,
            env={**os.environ, "GITHUB_OUTPUT": str(self.output), **env},
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def setup_request(
        self, control: object, *, mode: str = "apply", attempt: str = "1"
    ) -> subprocess.CompletedProcess[str]:
        env = dict.fromkeys(
            (
                "TARGET_ID",
                "TARGET_NAME",
                "PROJECT_ID",
                "PROJECT_NAME",
                "ENVIRONMENT_ID",
                "ENVIRONMENT_NAME",
                "SERVER_ID",
                "APP_NAME",
                "DESCRIPTION",
                "SOURCE_GIT_REF",
                "SOURCE_TYPE",
                "COMPOSE_PATH",
                "DOMAIN",
                "RUNTIME_PORT",
                "HEALTHCHECK_PATH",
                "DEPLOY_TIMEOUT_SECONDS",
                "EXPECTED_CURRENT_PROVIDER_TARGET_JSON",
                "CONFIRMATION",
                "REASON",
            ),
            "",
        )
        env.update(
            MODE=mode,
            OPERATION="create-application",
            CONTEXT="fixture-testing",
            INSTANCE="testing",
            TARGET_TYPE="application",
            GITHUB_RUN_ID="101",
            GITHUB_RUN_ATTEMPT=attempt,
            ALLOCATION_CONTROL=json.dumps(control),
        )
        return self.run_step(
            "dokploy-target-setup.yml", "setup", "Prepare target setup request", env
        )

    def test_setup_reuses_explicit_key_and_renders_readback_and_retention(self) -> None:
        control = {"idempotency_key": "original key-é"}
        first = self.setup_request(control)
        self.assertEqual(first.returncode, 0, first.stderr)
        first_output = self.output.read_text()
        self.output.unlink()
        rerun = self.setup_request(control, attempt="2")
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertEqual(self.output.read_text(), first_output)
        readback = self.setup_request(control, mode="readback")
        self.assertEqual(readback.returncode, 0, readback.stderr)
        self.assertEqual(
            json.loads((self.root / "dokploy-target-setup-payload.json").read_text())["mode"],
            "readback",
        )
        retained = self.setup_request(
            {**control, "retain_allocation": True, "expected_allocation_custody_sha256": "a" * 64}
        )
        self.assertEqual(retained.returncode, 0, retained.stderr)
        self.assertTrue(
            json.loads((self.root / "dokploy-target-setup-payload.json").read_text())[
                "retain_allocation"
            ]
        )

    def test_setup_refuses_missing_key_or_open_control_before_request(self) -> None:
        for control in (
            {},
            {"idempotency_key": "key", "context": "other"},
            {"idempotency_key": "key\ninjected=value"},
            {"idempotency_key": "key\n"},
            {"idempotency_key": "key", "retain_allocation": "true"},
            {"idempotency_key": "key", "retain_allocation": True},
        ):
            with self.subTest(control=control):
                self.assertNotEqual(self.setup_request(control).returncode, 0)
                self.assertFalse((self.root / "dokploy-target-setup-payload.json").exists())
