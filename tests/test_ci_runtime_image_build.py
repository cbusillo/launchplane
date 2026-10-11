from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.support.workflows import load_workflow


ROOT = Path(__file__).resolve().parents[1]
MISSING_LAYER = (
    "ERROR: failed to solve: failed to compute cache key: failed to copy: "
    "httpReadSeeker: failed open: could not fetch content descriptor sha256:abc123 "
    "(application/vnd.oci.image.layer.v1.tar+gzip)"
)


class RuntimeImageBuildTests(unittest.TestCase):
    def run_build(self, first_error: str, retry_status: int = 0) -> tuple[int, list[list[str]]]:
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            docker = scratch / "docker"
            docker.write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                'printf "%s\\n" "$*" >> "$BUILD_CALLS"\n'
                'if [ "$(wc -l < "$BUILD_CALLS")" -eq 1 ]; then\n'
                '  if [ -n "$FIRST_ERROR" ]; then\n'
                '    echo "$FIRST_ERROR" >&2\n'
                "    exit 17\n"
                "  fi\n"
                "else\n"
                '  exit "$RETRY_STATUS"\n'
                "fi\n",
                encoding="utf-8",
            )
            docker.chmod(0o755)
            calls = scratch / "calls"
            environment = {
                **os.environ,
                "PATH": f"{scratch}{os.pathsep}{os.environ['PATH']}",
                "RUNNER_TEMP": str(scratch),
                "LAUNCHPLANE_CI_BUILDER": "isolated-job-builder",
                "LAUNCHPLANE_CI_IMAGE_TAG": "fixture-image:current-run",
                "BUILD_CALLS": str(calls),
                "FIRST_ERROR": first_error,
                "RETRY_STATUS": str(retry_status),
            }
            workflow = load_workflow(ROOT / ".github/workflows/ci.yml")
            for job in ("container_scan", "container_scan_fork"):
                with self.subTest(job=job):
                    calls.unlink(missing_ok=True)
                    step = workflow.step_named(job, "Build runtime image")
                    assert step is not None
                    result = subprocess.run(
                        ["bash", "-euo", "pipefail", "-c", step.run],
                        cwd=ROOT,
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    commands = [line.split() for line in calls.read_text().splitlines()]
                    self.assertEqual(list(scratch.glob("launchplane-image-build.*")), [])
                    self.assertEqual(
                        result.returncode,
                        retry_status
                        if first_error == MISSING_LAYER
                        else (17 if first_error else 0),
                    )
            return result.returncode, commands

    def test_success_builds_and_loads_once_in_job_builder(self) -> None:
        status, commands = self.run_build("")
        self.assertEqual(status, 0)
        self.assertEqual(len(commands), 1)
        command = commands[0]
        self.assertIn("--load", command)
        self.assertIn("--pull", command)
        self.assertEqual(command[command.index("--builder") + 1], "isolated-job-builder")
        self.assertEqual(command[command.index("--tag") + 1], "fixture-image:current-run")
        self.assertNotIn("--cache-from", command)

    def test_missing_layer_retries_once_without_cache(self) -> None:
        status, commands = self.run_build(MISSING_LAYER)
        self.assertEqual(status, 0)
        self.assertEqual(len(commands), 2)
        self.assertIn("--no-cache", commands[1])
        self.assertNotIn("--no-cache-filter", commands[1])
        for option in ("--builder", "--tag"):
            self.assertEqual(
                commands[0][commands[0].index(option) + 1],
                commands[1][commands[1].index(option) + 1],
            )
        self.assertIn("--load", commands[1])
        self.assertIn("--pull", commands[1])

    def test_failed_retry_still_fails_without_third_attempt(self) -> None:
        status, commands = self.run_build(MISSING_LAYER, retry_status=23)
        self.assertEqual(status, 23)
        self.assertEqual(len(commands), 2)

    def test_unrelated_errors_fail_without_retry(self) -> None:
        for error in (
            "ERROR: process did not complete successfully: exit code: 1",
            "ERROR: failed to compute cache key: file not found",
            "ERROR: httpReadSeeker: failed open: unauthorized",
            "ERROR: failed to compute cache key: could not fetch content descriptor sha256:abc123",
        ):
            with self.subTest(error=error):
                status, commands = self.run_build(error)
                self.assertEqual(status, 17)
                self.assertEqual(len(commands), 1)
