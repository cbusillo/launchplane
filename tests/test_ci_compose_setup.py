from __future__ import annotations

import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import unittest

from tests.support.workflows import load_workflow

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = b"""#!/bin/sh
if [ "$1" = docker-cli-plugin-metadata ]; then
  printf '%s\n' '{"SchemaVersion":"0.1.0","Vendor":"fixture","Version":"v0.0.0","ShortDescription":"Fixture Compose"}'
else
  printf '%s\n' 'Fixture Compose works'
fi
"""


class ComposeSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        if not shutil.which("docker"):
            if os.environ.get("CI"):
                self.fail("CI requires the Docker CLI for Compose installation proof")
            self.skipTest("Docker CLI is unavailable")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.scratch = Path(temporary.name)
        self.config = self.scratch / "job-config"
        self.tools = self.scratch / "runner-tools"
        architecture = "arm64" if platform.machine() in {"arm64", "aarch64"} else "x64"
        self.cached = (
            self.tools
            / "compose-dl-bin"
            / "0.0.0"
            / f"{platform.system().lower()}-{architecture}"
            / "docker-compose"
        )
        self.cached.parent.mkdir(parents=True)
        self.cached.write_bytes(PLUGIN)
        self.download = self.scratch / "download"
        self.download.write_bytes(PLUGIN)
        commands = self.scratch / "commands"
        commands.mkdir()
        curl = commands / "curl"
        curl.write_text(
            '#!/bin/sh\nset -eu\nprintf "%s\\n" "$*" >> "$DOWNLOAD_CALLS"\n'
            'if [ "$DOWNLOAD_STATUS" != 0 ]; then exit "$DOWNLOAD_STATUS"; fi\n'
            'while [ "$1" != "--output" ]; do shift; done\n'
            'cp "$DOWNLOAD_SOURCE" "$2"\n',
            encoding="utf-8",
        )
        curl.chmod(0o755)
        self.calls = self.scratch / "download-calls"
        self.environment = {
            **os.environ,
            "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}",
            "DOCKER_CONFIG": str(self.config),
            "DOCKER_COMPOSE_VERSION": "v0.0.0",
            "RUNNER_TEMP": str(self.scratch),
            "RUNNER_TOOL_CACHE": str(self.tools),
            "DOWNLOAD_CALLS": str(self.calls),
            "DOWNLOAD_SOURCE": str(self.download),
            "DOWNLOAD_STATUS": "0",
        }

    def run_setup(
        self, command: str = "bash scripts/setup-ci-compose.sh"
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-euo", "pipefail", "-c", command],
            cwd=ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_ci_paths_install_a_discoverable_plugin_from_the_runner_cache(self) -> None:
        workflow = load_workflow(ROOT / ".github/workflows/ci.yml")
        for job in ("container_scan", "container_scan_fork", "test_shards", "test_fork"):
            with self.subTest(job=job):
                step = workflow.step_named(job, "Set up Docker Compose")
                assert step is not None
                result = self.run_setup(step.run)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Fixture Compose works", result.stdout)
                self.assertEqual(self.cached.read_bytes(), PLUGIN)
                self.assertFalse(self.calls.exists())
                self.assertEqual(list((self.config / "cli-plugins").glob("compose-install.*")), [])

    def test_distinct_job_plugins_remain_usable_when_the_runner_cache_is_rewritten(self) -> None:
        for job in ("first", "second"):
            self.environment["DOCKER_CONFIG"] = str(self.scratch / job)
            result = self.run_setup()
            self.assertEqual(result.returncode, 0, result.stderr)
        self.cached.write_bytes(b"")
        for job in ("first", "second"):
            result = subprocess.run(
                ["docker", "compose", "version"],
                env={**self.environment, "DOCKER_CONFIG": str(self.scratch / job)},
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_cache_miss_downloads_the_requested_version(self) -> None:
        self.cached.unlink()
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            f"/releases/download/{self.environment['DOCKER_COMPOSE_VERSION']}/",
            self.calls.read_text(),
        )
        self.assertFalse(self.cached.exists())

    def test_corrupt_cached_binary_is_replaced_without_changing_the_runner_cache(self) -> None:
        self.cached.write_bytes(b"corrupt cached executable")
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Fixture Compose works", result.stdout)
        self.assertTrue(self.calls.exists())
        self.assertEqual(self.cached.read_bytes(), b"corrupt cached executable")

    def test_invalid_download_does_not_publish_a_plugin(self) -> None:
        self.cached.unlink()
        self.download.write_bytes(b"corrupt downloaded executable")
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.config / "cli-plugins/docker-compose").exists())
        self.assertEqual(list((self.config / "cli-plugins").glob("compose-install.*")), [])

    def test_failed_download_preserves_the_installed_plugin_and_cleans_staging(self) -> None:
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        plugin = self.config / "cli-plugins/docker-compose"
        previous = plugin.read_bytes()
        self.cached.unlink()
        self.environment["DOWNLOAD_STATUS"] = "22"
        result = self.run_setup()
        self.assertEqual(result.returncode, 22, result.stderr)
        self.assertEqual(plugin.read_bytes(), previous)
        self.assertEqual(list(plugin.parent.glob("compose-install.*")), [])
