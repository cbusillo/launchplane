from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
from tempfile import TemporaryDirectory
from typing import cast
import unittest

from tests.support.workflows import load_workflow


class DeployLaunchplaneWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load_workflow(".github/workflows/deploy-launchplane.yml")

    def _run_step(
        self, job: str, name: str, env: dict[str, str], directory: Path
    ) -> subprocess.CompletedProcess[str]:
        step = self.workflow.step_named(job, name)
        assert step is not None
        runtime = directory / "runtime.json"
        if not runtime.exists():
            runtime.write_text("{}\n", encoding="utf-8")
        return subprocess.run(
            ["bash", "-c", step.run],
            cwd=Path.cwd(),
            env={
                "PATH": os.environ["PATH"],
                "HOME": str(directory),
                "LANG": "C.UTF-8",
                "GITHUB_OUTPUT": str(directory / "output"),
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

    def _render(self, name: str, env: dict[str, str]) -> dict[str, object]:
        with TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            result = self._run_step("deploy", name, env, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = directory / "output"
            values = dict(
                line.split("=", 1) for line in output.read_text().splitlines() if "=" in line
            )
            return cast(dict[str, object], json.loads(Path(values["payload_file"]).read_text()))

    def test_rollback_reuses_prepared_budget_without_resolving_dependencies(self) -> None:
        with TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            budget = 77
            result = self._run_step(
                "deploy",
                "Resolve Launchplane rollback wait timeout",
                {"WAIT_TIMEOUT_SECONDS": str(budget)},
                directory,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            values = dict(
                line.split("=", 1) for line in (directory / "output").read_text().splitlines()
            )
            self.assertEqual(int(values["timeout_seconds"]), budget)
            self.assertEqual(int(values["timeout_ms"]), budget * 1000)

    def test_later_waits_consume_remaining_deadline_without_resetting_budget(self) -> None:
        for step in (
            "Resolve deploy_runtime_wait remaining wait",
            "Resolve deploy_marker_wait remaining wait",
            "Resolve rollback_runtime_wait remaining wait",
        ):
            for remaining in (60, -60):
                with (
                    self.subTest(step=step, remaining=remaining),
                    TemporaryDirectory() as directory_name,
                ):
                    directory = Path(directory_name)
                    result = self._run_step(
                        "deploy",
                        step,
                        {"WAIT_DEADLINE_EPOCH": str(int(time.time()) + remaining)},
                        directory,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    values = dict(
                        line.split("=", 1)
                        for line in (directory / "output").read_text().splitlines()
                    )
                    actual = int(values["timeout_ms"])
                    if remaining > 0:
                        self.assertLessEqual(actual, remaining * 1000)
                        self.assertGreater(actual, (remaining - 10) * 1000)
                    else:
                        self.assertEqual(actual, 1)

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

    def test_self_deploy_removes_unset_public_ingress_token_and_omitted_npmplus_env(
        self,
    ) -> None:
        base = {
            "BOOTSTRAP_SECRET_OPERATION": "preserve",
            "ORDINARY_AGENT_WORKERS": "preserve",
            "ORDINARY_AGENT_WORKERS_EXPECTED_STATE": "absent",
            "DEPLOYMENT_MARKER": "deploy-marker",
            "DEPLOY_IMAGE_REFERENCE": "ghcr.io/cbusillo/launchplane@sha256:" + ("b" * 64),
            "IMAGE_REPOSITORY": "ghcr.io/cbusillo/launchplane",
        }
        cases = (
            ("", "false", ["LAUNCHPLANE_PUBLIC_INGRESS_GITHUB_TOKEN"]),
            ("public-ingress-token", "false", None),
            (
                "",
                "true",
                [
                    "LAUNCHPLANE_NPMPLUS_BASE_URL",
                    "LAUNCHPLANE_NPMPLUS_IDENTITY",
                    "LAUNCHPLANE_NPMPLUS_SECRET",
                    "LAUNCHPLANE_PUBLIC_INGRESS_GITHUB_TOKEN",
                ],
            ),
        )
        for token, omit_npmplus, removals in cases:
            with self.subTest(token=bool(token), omit_npmplus=omit_npmplus):
                payload = self._render(
                    "Render Launchplane self deploy request",
                    {
                        **base,
                        "LAUNCHPLANE_PUBLIC_INGRESS_GITHUB_TOKEN": token,
                        "OMIT_NPMPLUS_ENV": omit_npmplus,
                    },
                )
                deploy = cast(dict[str, object], payload["deploy"])
                self.assertEqual(deploy.get("oauth_env_removals"), removals)

    def test_self_deploy_rejects_multiline_previous_image_reference(self) -> None:
        with TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            (directory / "runtime.json").write_text(
                json.dumps(
                    {
                        "runtime": {
                            "docker_image_reference": (
                                "ghcr.io/cbusillo/launchplane@sha256:"
                                + ("a" * 64)
                                + "\nprevious_image_reference=attacker-controlled"
                            )
                        }
                    }
                ),
                encoding="utf-8",
            )
            result = self._run_step(
                "deploy",
                "Render Launchplane self deploy request",
                {
                    "BOOTSTRAP_SECRET_OPERATION": "preserve",
                    "ORDINARY_AGENT_WORKERS": "preserve",
                    "ORDINARY_AGENT_WORKERS_EXPECTED_STATE": "absent",
                    "DEPLOYMENT_MARKER": "deploy-marker",
                    "DEPLOY_IMAGE_REFERENCE": "ghcr.io/cbusillo/launchplane@sha256:" + ("b" * 64),
                    "IMAGE_REPOSITORY": "ghcr.io/cbusillo/launchplane",
                },
                directory,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must not contain control characters", result.stderr)
            self.assertFalse((directory / "output").exists())

    def test_break_glass_validation_never_executes_the_dispatch_reason(self) -> None:
        image_reference = "ghcr.io/cbusillo/launchplane@sha256:" + ("a" * 64)
        with TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            command_marker = directory / "command-substitution-ran"
            result = self._run_step(
                "emergency-dokploy-rollback",
                "Validate manual break-glass request",
                {
                    "AUTHZ_GRANTS_MODE": "none",
                    "AUTHZ_MANAGED_MODE": "none",
                    "BREAK_GLASS_IMAGE_REFERENCE": image_reference,
                    "BREAK_GLASS_REASON": f'Restore after "review" $(touch {command_marker})',
                    "GITHUB_REPOSITORY": "cbusillo/launchplane",
                    "LAUNCHPLANE_IMAGE_REPOSITORY": "ghcr.io/cbusillo/launchplane",
                },
                directory,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(command_marker.exists())

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
