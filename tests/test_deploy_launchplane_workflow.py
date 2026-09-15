from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from typing import cast
import unittest

from tests.support.workflows import load_workflow


class DeployLaunchplaneWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load_workflow(".github/workflows/deploy-launchplane.yml")

    def _render(self, name: str, env: dict[str, str]) -> dict[str, object]:
        step = self.workflow.step_named("deploy", name)
        assert step is not None
        with TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            output = directory / "output"
            runtime = directory / "runtime.json"
            runtime.write_text("{}\n", encoding="utf-8")
            result = subprocess.run(
                ["bash", "-c", step.run],
                cwd=Path.cwd(),
                env={
                    "PATH": os.environ["PATH"],
                    "HOME": str(directory),
                    "LANG": "C.UTF-8",
                    "GITHUB_OUTPUT": str(output),
                    "RUNNER_TEMP": str(directory),
                    "PREVIOUS_RUNTIME_RESPONSE_FILE": str(runtime),
                    "LAUNCHPLANE_DOKPLOY_TARGET_TYPE": "compose",
                    "LAUNCHPLANE_DOKPLOY_TARGET_ID": "compose-test",
                    "OMIT_EVERY_CODE_ENV": "false",
                    "OMIT_TERMINAL_AGENT_ENV": "false",
                    "OMIT_OWNER_AGENT_ENV": "false",
                    "OMIT_NPMPLUS_ENV": "false",
                    **env,
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            values = dict(
                line.split("=", 1) for line in output.read_text().splitlines() if "=" in line
            )
            return cast(dict[str, object], json.loads(Path(values["payload_file"]).read_text()))

    def test_rendered_worker_changes_and_same_image_rollback_are_exact(self) -> None:
        base = {
            "BOOTSTRAP_SECRET_OPERATION": "preserve",
            "DEPLOYMENT_MARKER": "deploy-marker",
            "DEPLOY_IMAGE_REFERENCE": "ghcr.io/cbusillo/launchplane@sha256:abc",
            "IMAGE_REPOSITORY": "ghcr.io/cbusillo/launchplane",
        }
        cases = (
            ("preserve", "absent", None),
            ("enable", "absent", {"expected": "absent", "desired": "1"}),
            ("enable", "0", {"expected": "0", "desired": "1"}),
            ("disable", "1", {"expected": "1", "desired": "0"}),
        )
        for operation, expected, wanted in cases:
            with self.subTest(operation=operation, expected=expected):
                payload = self._render(
                    "Render Launchplane self deploy request",
                    {
                        **base,
                        "ORDINARY_AGENT_WORKERS": operation,
                        "ORDINARY_AGENT_WORKERS_EXPECTED_STATE": expected,
                    },
                )
                deploy = cast(dict[str, object], payload["deploy"])
                self.assertEqual(deploy.get("ordinary_agent_worker_replicas"), wanted)
        rollback = self._render(
            "Render Launchplane rollback request",
            {
                "BOOTSTRAP_SECRET_OPERATION": "preserve",
                "ORDINARY_AGENT_WORKERS": "enable",
                "ORDINARY_AGENT_WORKERS_EXPECTED_STATE": "absent",
                "DEPLOYED_IMAGE_REFERENCE": "ghcr.io/cbusillo/launchplane@sha256:same",
                "PREVIOUS_IMAGE_REFERENCE": "ghcr.io/cbusillo/launchplane@sha256:same",
                "FORWARD_DEPLOYMENT_MARKER": "forward",
                "ROLLBACK_DEPLOYMENT_MARKER": "rollback",
                "SELF_DEPLOY_IDEMPOTENCY_KEY": "self-deploy-key",
            },
        )
        self.assertEqual(
            cast(dict[str, object], rollback["deploy"])["ordinary_agent_worker_replicas"],
            {"expected": "1", "desired": "absent"},
        )

    def test_automatic_input_resolution_preserves_worker_replicas(self) -> None:
        step = self.workflow.step_named("deploy", "Resolve deploy inputs")
        assert step is not None
        with TemporaryDirectory() as directory_name:
            output = Path(directory_name) / "output"
            result = subprocess.run(
                ["bash", "-c", step.run],
                env={
                    "PATH": os.environ["PATH"],
                    "HOME": directory_name,
                    "LANG": "C.UTF-8",
                    "GITHUB_OUTPUT": str(output),
                    "EVENT_NAME": "workflow_run",
                    "WORKFLOW_RUN_HEAD_SHA": "a" * 40,
                    "WORKFLOW_SHA": "b" * 40,
                    "GITHUB_REPOSITORY": "cbusillo/launchplane",
                    "LAUNCHPLANE_IMAGE_REPOSITORY": "",
                    "GITHUB_RUN_ID": "123",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "DISPATCH_BOOTSTRAP_SECRET_OPERATION": "",
                    "DISPATCH_ORDINARY_AGENT_WORKERS": "",
                    "DISPATCH_ORDINARY_AGENT_WORKERS_EXPECTED_STATE": "",
                    "DISPATCH_IMAGE_REFERENCE": "",
                    "DISPATCH_SELF_DEPLOY_IDEMPOTENCY_KEY": "",
                    "OMIT_EVERY_CODE_ENV": "false",
                    "OMIT_TERMINAL_AGENT_ENV": "false",
                    "OMIT_OWNER_AGENT_ENV": "false",
                    "OMIT_NPMPLUS_ENV": "false",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ordinary_agent_workers=preserve", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
