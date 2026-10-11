import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import yaml


class LaunchplaneDeployWaitTests(unittest.TestCase):
    def test_completed_release_drain_excludes_only_release_workers_from_observation(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts/deploy/resolve-wait-timeout.py"
        with TemporaryDirectory() as directory:
            compose = Path(directory) / "compose.yml"
            compose.write_text(
                yaml.safe_dump(
                    {
                        "services": {
                            "release": {
                                "stop_grace_period": "2h",
                                "labels": {"launchplane.release-worker": "true"},
                            },
                            "other": {"stop_grace_period": "5m"},
                        }
                    }
                )
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--compose-file",
                    str(compose),
                    "--release-drain-complete",
                ],
                env={
                    **os.environ,
                    "LAUNCHPLANE_DOKPLOY_DEPLOY_TIMEOUT_SECONDS": "7",
                    "LAUNCHPLANE_DEPLOY_HEALTH_TIMEOUT_SECONDS": "11",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(int(result.stdout), 5 * 60 + 7 + 11)

    def test_wait_includes_longest_parallel_drain_and_deploy_health_budgets(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts/deploy/resolve-wait-timeout.py"
        with TemporaryDirectory() as directory:
            compose = Path(directory) / "compose.yml"
            compose.write_text(
                yaml.safe_dump(
                    {
                        "services": {
                            "backup": {"stop_grace_period": "2m13s"},
                            "other": {"stop_grace_period": "5s"},
                            "api": {},
                        }
                    }
                )
            )
            for deploy, health in ((7, 11), (1000, 50)):
                with self.subTest(deploy=deploy, health=health):
                    result = subprocess.run(
                        [sys.executable, str(script), "--compose-file", str(compose)],
                        env={
                            **os.environ,
                            "LAUNCHPLANE_DOKPLOY_DEPLOY_TIMEOUT_SECONDS": str(deploy),
                            "LAUNCHPLANE_DEPLOY_HEALTH_TIMEOUT_SECONDS": str(health),
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(int(result.stdout), 2 * 60 + 13 + deploy + health)

    def test_unreadable_or_invalid_grace_never_returns_a_shorter_budget(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts/deploy/resolve-wait-timeout.py"
        with TemporaryDirectory() as directory:
            compose = Path(directory) / "compose.yml"
            for grace in (None, "unresolved", "-1s"):
                with self.subTest(grace=grace):
                    if grace is not None:
                        compose.write_text(
                            yaml.safe_dump(
                                {
                                    "services": {
                                        "backup": {"stop_grace_period": grace},
                                    }
                                }
                            )
                        )
                    result = subprocess.run(
                        [sys.executable, str(script), "--compose-file", str(compose)],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
