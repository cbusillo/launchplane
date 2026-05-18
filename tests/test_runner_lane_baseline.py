from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.runner_lane_baseline import RunnerLaneBaselineObservation
from control_plane.contracts.runner_lane_baseline import RunnerLaneBaselinePolicy
from control_plane.contracts.runner_lane_baseline import evaluate_runner_lane_baseline


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


if __name__ == "__main__":
    unittest.main()
