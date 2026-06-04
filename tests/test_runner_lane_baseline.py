import json
from pathlib import Path
from tempfile import TemporaryDirectory
from collections.abc import Sequence
from typing import cast
import unittest
from unittest.mock import patch

from click import Command
from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.runner_lane_baseline import RunnerLaneBaselineObservation
from control_plane.contracts.runner_lane_baseline import RunnerLaneBaselinePolicy
from control_plane.contracts.runner_lane_baseline import RunnerLaneDockerToolchainObservation
from control_plane.contracts.runner_lane_baseline import evaluate_runner_lane_baseline
from control_plane.workflows.runner_host_hygiene_executor import RemoteCommandResult


CLI_MAIN = cast(Command, main)


class RunnerLaneBaselineTests(unittest.TestCase):
    def test_readiness_passes_for_isolated_launchplane_runner(self) -> None:
        readiness = evaluate_runner_lane_baseline(
            policy=RunnerLaneBaselinePolicy(),
            observations=(
                RunnerLaneBaselineObservation(
                    runner_name="launchplane-runner-1",
                    labels=("self-hosted", "Launchplane", "Linux"),
                    docker_config_isolated=True,
                    observed_at="2026-05-18T12:00:00Z",
                ),
            ),
        )

        self.assertTrue(readiness.ready)
        self.assertEqual(readiness.observed_lanes, 1)
        self.assertEqual(readiness.compliant_lanes, 1)
        self.assertEqual(readiness.violations, ())
        self.assertEqual(readiness.summary, "runner lane baseline satisfied")

    def test_readiness_fails_closed_without_observations(self) -> None:
        readiness = evaluate_runner_lane_baseline(
            policy=RunnerLaneBaselinePolicy(), observations=()
        )

        self.assertFalse(readiness.ready)
        self.assertEqual(readiness.observed_lanes, 0)
        self.assertEqual(readiness.compliant_lanes, 0)
        self.assertEqual(readiness.summary, "no runner lane baseline observations supplied")

    def test_readiness_requires_docker_credential_isolation_evidence(self) -> None:
        readiness = evaluate_runner_lane_baseline(
            policy=RunnerLaneBaselinePolicy(),
            observations=(
                RunnerLaneBaselineObservation(
                    runner_name="launchplane-runner-1",
                    labels=("self-hosted", "launchplane"),
                    docker_config_isolated=None,
                    observed_at="2026-05-18T12:00:00Z",
                ),
            ),
        )

        self.assertFalse(readiness.ready)
        self.assertEqual(readiness.compliant_lanes, 0)
        self.assertEqual(
            [violation.code for violation in readiness.violations],
            ["docker_config_isolation_missing"],
        )

    def test_readiness_reports_missing_required_labels(self) -> None:
        readiness = evaluate_runner_lane_baseline(
            policy=RunnerLaneBaselinePolicy(required_labels=("self-hosted", "deploy")),
            observations=(
                RunnerLaneBaselineObservation(
                    runner_name="launchplane-runner-1",
                    labels=("self-hosted",),
                    docker_config_isolated=True,
                    observed_at="2026-05-18T12:00:00Z",
                ),
            ),
        )

        self.assertFalse(readiness.ready)
        self.assertEqual(
            [violation.code for violation in readiness.violations],
            ["required_label_missing"],
        )
        self.assertIn("deploy", readiness.violations[0].message)

    def test_readiness_can_enforce_service_user_and_home_root(self) -> None:
        readiness = evaluate_runner_lane_baseline(
            policy=RunnerLaneBaselinePolicy(
                allowed_service_users=("actions",),
                allowed_home_roots=("/var/lib/actions-runners",),
            ),
            observations=(
                RunnerLaneBaselineObservation(
                    runner_name="launchplane-runner-1",
                    labels=("self-hosted", "launchplane"),
                    docker_config_isolated=True,
                    service_user="root",
                    home_directory="/home/actions",
                    observed_at="2026-05-18T12:00:00Z",
                ),
            ),
        )

        self.assertFalse(readiness.ready)
        self.assertEqual(
            [violation.code for violation in readiness.violations],
            ["home_directory_outside_allowed_roots", "service_user_not_allowed"],
        )

    def test_readiness_rejects_stale_docker_buildx_toolchain(self) -> None:
        readiness = evaluate_runner_lane_baseline(
            policy=RunnerLaneBaselinePolicy(minimum_docker_buildx_version="0.23.0"),
            observations=(
                RunnerLaneBaselineObservation(
                    runner_name="chris-testing-runtime-smoke",
                    labels=("self-hosted", "launchplane"),
                    docker_config_isolated=True,
                    docker_toolchain=RunnerLaneDockerToolchainObservation(
                        docker_engine_version="26.1.5+dfsg1",
                        docker_cli_version="26.1.5+dfsg1",
                        docker_buildx_version="0.13.1+ds1",
                        docker_buildx_plugin_path="/usr/libexec/docker/cli-plugins/docker-buildx",
                        docker_buildx_package="docker-buildx 0.13.1+ds1-3",
                        docker_buildx_source="Debian trixie",
                        buildkit_version="0.30.0",
                    ),
                    observed_at="2026-06-04T12:00:00Z",
                ),
            ),
        )

        self.assertFalse(readiness.ready)
        self.assertEqual(readiness.compliant_lanes, 0)
        self.assertEqual(
            [violation.code for violation in readiness.violations],
            ["docker_buildx_version_below_minimum"],
        )
        self.assertIn("0.13.1+ds1 < 0.23.0", readiness.violations[0].message)

    def test_readiness_accepts_current_docker_buildx_toolchain(self) -> None:
        readiness = evaluate_runner_lane_baseline(
            policy=RunnerLaneBaselinePolicy(
                require_docker_toolchain_observation=True,
                minimum_docker_buildx_version="0.23.0",
            ),
            observations=(
                RunnerLaneBaselineObservation(
                    runner_name="launchplane-runner-1",
                    labels=("self-hosted", "launchplane"),
                    docker_config_isolated=True,
                    docker_toolchain=RunnerLaneDockerToolchainObservation(
                        docker_engine_version="27.5.1",
                        docker_cli_version="27.5.1",
                        docker_buildx_version="0.23.0",
                        docker_buildx_plugin_path="/usr/local/lib/docker/cli-plugins/docker-buildx",
                        docker_buildx_source="managed plugin",
                        buildkit_version="0.23.2",
                    ),
                    observed_at="2026-06-04T12:00:00Z",
                ),
            ),
        )

        self.assertTrue(readiness.ready)
        self.assertEqual(readiness.violations, ())

    def test_readiness_requires_docker_toolchain_when_policy_requests_it(self) -> None:
        readiness = evaluate_runner_lane_baseline(
            policy=RunnerLaneBaselinePolicy(require_docker_toolchain_observation=True),
            observations=(
                RunnerLaneBaselineObservation(
                    runner_name="launchplane-runner-1",
                    labels=("self-hosted", "launchplane"),
                    docker_config_isolated=True,
                    observed_at="2026-06-04T12:00:00Z",
                ),
            ),
        )

        self.assertFalse(readiness.ready)
        self.assertEqual(
            [violation.code for violation in readiness.violations],
            ["docker_toolchain_missing"],
        )

    def test_readiness_requires_buildx_version_when_toolchain_is_required(self) -> None:
        readiness = evaluate_runner_lane_baseline(
            policy=RunnerLaneBaselinePolicy(require_docker_toolchain_observation=True),
            observations=(
                RunnerLaneBaselineObservation(
                    runner_name="launchplane-runner-1",
                    labels=("self-hosted", "launchplane"),
                    docker_config_isolated=True,
                    docker_toolchain=RunnerLaneDockerToolchainObservation(
                        docker_engine_version="27.5.1",
                    ),
                    observed_at="2026-06-04T12:00:00Z",
                ),
            ),
        )

        self.assertFalse(readiness.ready)
        self.assertEqual(
            [violation.code for violation in readiness.violations],
            ["docker_buildx_version_invalid"],
        )

    def test_readiness_rejects_invalid_buildx_version_for_minimum_policy(self) -> None:
        readiness = evaluate_runner_lane_baseline(
            policy=RunnerLaneBaselinePolicy(minimum_docker_buildx_version="0.23.0"),
            observations=(
                RunnerLaneBaselineObservation(
                    runner_name="launchplane-runner-1",
                    labels=("self-hosted", "launchplane"),
                    docker_config_isolated=True,
                    docker_toolchain=RunnerLaneDockerToolchainObservation(
                        docker_buildx_version="unknown",
                        docker_buildx_plugin_path="/usr/local/lib/docker/cli-plugins/docker-buildx",
                    ),
                    observed_at="2026-06-04T12:00:00Z",
                ),
            ),
        )

        self.assertFalse(readiness.ready)
        self.assertEqual(
            [violation.code for violation in readiness.violations],
            ["docker_buildx_version_invalid"],
        )

    def test_readiness_rejects_home_directory_traversal_outside_allowed_roots(
        self,
    ) -> None:
        readiness = evaluate_runner_lane_baseline(
            policy=RunnerLaneBaselinePolicy(
                allowed_home_roots=("/var/lib/actions-runners",),
            ),
            observations=(
                RunnerLaneBaselineObservation(
                    runner_name="launchplane-runner-1",
                    labels=("self-hosted", "launchplane"),
                    docker_config_isolated=True,
                    home_directory="/var/lib/actions-runners/../tmp",
                    observed_at="2026-05-18T12:00:00Z",
                ),
            ),
        )

        self.assertFalse(readiness.ready)
        self.assertEqual(
            [violation.code for violation in readiness.violations],
            ["home_directory_outside_allowed_roots"],
        )

    def test_readiness_allows_absolute_home_directory_under_root(self) -> None:
        readiness = evaluate_runner_lane_baseline(
            policy=RunnerLaneBaselinePolicy(
                allowed_service_users=("gha",),
                allowed_home_roots=("/",),
            ),
            observations=(
                RunnerLaneBaselineObservation(
                    runner_name="launchplane-runner-1",
                    labels=("self-hosted", "launchplane"),
                    docker_config_isolated=True,
                    service_user="GHA ",
                    home_directory="/home/gha",
                    observed_at="2026-05-18T12:00:00Z",
                ),
            ),
        )

        self.assertTrue(readiness.ready)
        self.assertEqual(readiness.violations, ())

    def test_readiness_counts_duplicate_runner_observations_once(self) -> None:
        readiness = evaluate_runner_lane_baseline(
            policy=RunnerLaneBaselinePolicy(),
            observations=(
                RunnerLaneBaselineObservation(
                    runner_name="launchplane-runner-1",
                    labels=("self-hosted", "launchplane"),
                    docker_config_isolated=True,
                    observed_at="2026-05-18T12:00:00Z",
                ),
                RunnerLaneBaselineObservation(
                    runner_name="launchplane-runner-1",
                    labels=("self-hosted", "launchplane"),
                    docker_config_isolated=True,
                    observed_at="2026-05-18T12:01:00Z",
                ),
            ),
        )

        self.assertTrue(readiness.ready)
        self.assertEqual(readiness.observed_lanes, 1)
        self.assertEqual(readiness.compliant_lanes, 1)
        self.assertEqual(readiness.violations, ())

    def test_readiness_rejects_symlinked_home_directory_outside_allowed_roots(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            allowed_root = root / "actions-runners"
            outside_root = root / "outside-home"
            linked_home = allowed_root / "runner-home"
            allowed_root.mkdir()
            outside_root.mkdir()
            linked_home.symlink_to(outside_root, target_is_directory=True)

            readiness = evaluate_runner_lane_baseline(
                policy=RunnerLaneBaselinePolicy(
                    allowed_home_roots=(allowed_root.as_posix(),),
                ),
                observations=(
                    RunnerLaneBaselineObservation(
                        runner_name="launchplane-runner-1",
                        labels=("self-hosted", "launchplane"),
                        docker_config_isolated=True,
                        home_directory=linked_home.as_posix(),
                        observed_at="2026-05-18T12:00:00Z",
                    ),
                ),
            )

        self.assertFalse(readiness.ready)
        self.assertEqual(
            [violation.code for violation in readiness.violations],
            ["home_directory_outside_allowed_roots"],
        )


class RunnerLaneBaselineCliTests(unittest.TestCase):
    def test_cli_observes_runner_baseline_from_job_environment(self) -> None:
        with TemporaryDirectory() as temp_dir:
            home_directory = Path(temp_dir) / "gha"
            docker_config = Path(temp_dir) / "docker-config"
            home_directory.mkdir()
            docker_config.mkdir()
            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "runner-baseline-observe",
                    "--observed-at",
                    "2026-05-18T18:28:00Z",
                    "--allowed-service-user",
                    "gha",
                    "--allowed-home-root",
                    temp_dir,
                ],
                env={
                    "RUNNER_NAME": "chris-testing-odoo-tenant-cm",
                    "RUNNER_LABELS": "self-hosted,Linux,X64,launchplane,chris-testing",
                    "USER": "gha",
                    "HOME": home_directory.as_posix(),
                    "DOCKER_CONFIG": docker_config.as_posix(),
                    "LAUNCHPLANE_ISOLATED_DOCKER_CONFIG": docker_config.as_posix(),
                },
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["observation"]["runner_name"], "chris-testing-odoo-tenant-cm")
        self.assertTrue(payload["observation"]["docker_config_isolated"])
        self.assertTrue(payload["readiness"]["ready"])
        self.assertEqual(payload["readiness"]["violations"], [])

    def test_cli_enforces_minimum_docker_buildx_version_from_explicit_evidence(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "runner-baseline-observe",
                    "--runner-name",
                    "chris-testing-runtime-smoke",
                    "--label",
                    "self-hosted",
                    "--label",
                    "launchplane",
                    "--docker-config-isolated",
                    "--docker-engine-version",
                    "26.1.5+dfsg1",
                    "--docker-cli-version",
                    "26.1.5+dfsg1",
                    "--docker-buildx-version",
                    "0.13.1+ds1",
                    "--docker-buildx-plugin-path",
                    "/usr/libexec/docker/cli-plugins/docker-buildx",
                    "--docker-buildx-package",
                    "docker-buildx 0.13.1+ds1-3",
                    "--docker-buildx-source",
                    "Debian trixie",
                    "--buildkit-version",
                    "0.30.0",
                    "--minimum-docker-buildx-version",
                    "0.23.0",
                    "--home-directory",
                    temp_dir,
                    "--observed-at",
                    "2026-06-04T12:00:00Z",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertFalse(payload["readiness"]["ready"])
        self.assertEqual(
            [violation["code"] for violation in payload["readiness"]["violations"]],
            ["docker_buildx_version_below_minimum"],
        )
        self.assertEqual(
            payload["observation"]["docker_toolchain"]["docker_buildx_plugin_path"],
            "/usr/libexec/docker/cli-plugins/docker-buildx",
        )

    def test_cli_observes_docker_toolchain_with_read_only_probe(self) -> None:
        outputs: dict[tuple[str, ...], str] = {
            ("docker", "version", "--format", "{{.Server.Version}}"): "26.1.5+dfsg1\n",
            ("docker", "version", "--format", "{{.Client.Version}}"): "26.1.5+dfsg1\n",
            ("docker", "buildx", "version"): "github.com/docker/buildx 0.13.1+ds1 0.13.1+ds1-3\n",
            ("docker", "buildx", "inspect"): "Driver: docker-container\nBuildKit: v0.30.0\n",
            (
                "sh",
                "-c",
                'command -v docker-buildx 2>/dev/null || for path in /usr/libexec/docker/cli-plugins/docker-buildx /usr/local/lib/docker/cli-plugins/docker-buildx $HOME/.docker/cli-plugins/docker-buildx; do [ -x "$path" ] && { printf \'%s\\n\' "$path"; break; }; done',
            ): "/usr/libexec/docker/cli-plugins/docker-buildx\n",
            (
                "sh",
                "-c",
                "dpkg-query -W -f='${Package} ${Version}' docker-buildx 2>/dev/null",
            ): "docker-buildx 0.13.1+ds1-3",
            ("sh", "-c", "rpm -q docker-buildx 2>/dev/null"): "",
        }

        observed_timeouts: list[int] = []

        def runner(command: Sequence[str], timeout: int) -> RemoteCommandResult:
            observed_timeouts.append(timeout)
            return RemoteCommandResult(returncode=0, stdout=outputs.get(tuple(command), ""))

        with patch(
            "control_plane.cli_runner_lanes.build_local_command_runner", return_value=runner
        ):
            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "runner-baseline-observe",
                    "--runner-name",
                    "chris-testing-runtime-smoke",
                    "--label",
                    "self-hosted",
                    "--label",
                    "launchplane",
                    "--docker-config-isolated",
                    "--observe-docker-toolchain",
                    "--docker-toolchain-timeout-seconds",
                    "30",
                    "--minimum-docker-buildx-version",
                    "0.23.0",
                    "--observed-at",
                    "2026-06-04T12:00:00Z",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(set(observed_timeouts), {30})
        self.assertFalse(payload["readiness"]["ready"])
        self.assertEqual(
            payload["observation"]["docker_toolchain"]["docker_engine_version"], "26.1.5+dfsg1"
        )
        self.assertEqual(
            payload["observation"]["docker_toolchain"]["docker_buildx_version"], "0.13.1+ds1"
        )
        self.assertEqual(
            payload["observation"]["docker_toolchain"]["docker_buildx_source"], "system package"
        )
        self.assertEqual(payload["observation"]["docker_toolchain"]["buildkit_version"], "0.30.0")

    def test_cli_fails_closed_without_docker_isolation_evidence(self) -> None:
        with TemporaryDirectory() as temp_dir:
            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "runner-baseline-observe",
                    "--runner-name",
                    "chris-testing-odoo-tenant-cm",
                    "--label",
                    "self-hosted",
                    "--label",
                    "launchplane",
                    "--home-directory",
                    temp_dir,
                    "--observed-at",
                    "2026-05-18T18:28:00Z",
                ],
                env={
                    "DOCKER_CONFIG": "",
                    "LAUNCHPLANE_ISOLATED_DOCKER_CONFIG": "",
                },
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertFalse(payload["readiness"]["ready"])
        self.assertEqual(
            [violation["code"] for violation in payload["readiness"]["violations"]],
            ["docker_config_isolation_missing"],
        )


if __name__ == "__main__":
    unittest.main()
