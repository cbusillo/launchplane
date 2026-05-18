import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from click import Command
from click.testing import CliRunner

from control_plane.cli import main

from control_plane.contracts.runner_lane_baseline import RunnerLaneBaselineObservation
from control_plane.contracts.runner_lane_baseline import RunnerLaneBaselinePolicy
from control_plane.contracts.runner_lane_baseline import RunnerLaneBaselineReadiness
from control_plane.contracts.runner_lane_baseline import evaluate_runner_lane_baseline
from control_plane.contracts.runner_lane_control import RunnerLaneControlPolicy
from control_plane.contracts.runner_lane_control import RunnerLaneControlRequest
from control_plane.contracts.runner_lane_control import plan_runner_lane_control
from control_plane.contracts.runner_lane_inventory import RunnerLaneInventory
from control_plane.contracts.runner_lane_inventory import RunnerLaneRecord
from control_plane.contracts.runner_lane_inventory import build_runner_lane_inventory


CLI_MAIN = cast(Command, main)


class RunnerLaneControlPlanTests(unittest.TestCase):
    def test_dry_run_control_plan_is_blocked_without_mutate_request(self) -> None:
        plan = plan_runner_lane_control(
            policy=RunnerLaneControlPolicy(
                allowed_repositories=("cbusillo/repo",),
                allow_restart=True,
            ),
            request=RunnerLaneControlRequest(
                action="restart",
                repository="cbusillo/repo",
                lane_name="lane-1",
            ),
            inventory=_inventory(labels=("self-hosted", "launchplane", "launchplane-managed")),
            baseline_readiness=_ready_baseline(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(
            [blocker.code for blocker in plan.blockers],
            ["mutate_not_requested"],
        )
        self.assertIn("dry-run", plan.blockers[0].message)

    def test_ready_plan_requires_opted_in_repo_managed_lane_and_baseline(self) -> None:
        plan = plan_runner_lane_control(
            policy=RunnerLaneControlPolicy(
                allowed_repositories=("cbusillo/repo",),
                allow_restart=True,
            ),
            request=RunnerLaneControlRequest(
                action="restart",
                repository=" cbusillo/repo ",
                lane_name="lane-1",
                mutate=True,
            ),
            inventory=_inventory(labels=("self-hosted", "launchplane", "launchplane-managed")),
            baseline_readiness=_ready_baseline(),
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.blockers, ())
        self.assertIn("restart the approved runner service", plan.next_steps)

    def test_ready_plan_normalizes_repository_spacing_in_policy_request_and_inventory(
        self,
    ) -> None:
        plan = plan_runner_lane_control(
            policy=RunnerLaneControlPolicy(
                allowed_repositories=(" cbusillo / repo ",),
                allow_restart=True,
            ),
            request=RunnerLaneControlRequest(
                action="restart",
                repository=" cbusillo / repo ",
                lane_name="lane-1",
                mutate=True,
            ),
            inventory=_inventory(
                repository=" cbusillo / repo ",
                labels=("self-hosted", "launchplane", "launchplane-managed"),
            ),
            baseline_readiness=_ready_baseline(),
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.repository, "cbusillo/repo")
        self.assertEqual(plan.blockers, ())

    def test_control_plan_blocks_when_lane_name_is_ambiguous(self) -> None:
        plan = plan_runner_lane_control(
            policy=RunnerLaneControlPolicy(
                allowed_repositories=("cbusillo/repo",),
                allow_restart=True,
            ),
            request=RunnerLaneControlRequest(
                action="restart",
                repository="cbusillo/repo",
                lane_name="lane-1",
                mutate=True,
            ),
            inventory=_inventory(
                labels=("self-hosted", "launchplane", "launchplane-managed"),
                extra_lanes=(
                    RunnerLaneRecord(
                        github_id=2,
                        name="lane-1",
                        repository="cbusillo/repo",
                        status="online",
                        busy=False,
                        labels=("self-hosted", "launchplane", "launchplane-managed"),
                        observed_at="2026-05-18T20:30:00Z",
                    ),
                ),
            ),
            baseline_readiness=_ready_baseline(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(
            [blocker.code for blocker in plan.blockers],
            ["lane_name_ambiguous"],
        )

    def test_remove_blocks_unmanaged_busy_lane(self) -> None:
        plan = plan_runner_lane_control(
            policy=RunnerLaneControlPolicy(
                allowed_repositories=("cbusillo/repo",),
                allow_remove=True,
            ),
            request=RunnerLaneControlRequest(
                action="remove",
                repository="cbusillo/repo",
                lane_name="lane-1",
                mutate=True,
            ),
            inventory=_inventory(labels=("self-hosted", "launchplane"), busy=True),
            baseline_readiness=_ready_baseline(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(
            [blocker.code for blocker in plan.blockers],
            ["busy_lane_requires_drain_confirmation", "managed_label_missing"],
        )

    def test_control_plan_blocks_when_baseline_is_not_ready(self) -> None:
        plan = plan_runner_lane_control(
            policy=RunnerLaneControlPolicy(
                allowed_repositories=("cbusillo/repo",),
                allow_drain=True,
            ),
            request=RunnerLaneControlRequest(
                action="drain",
                repository="cbusillo/repo",
                lane_name="lane-1",
                mutate=True,
            ),
            inventory=_inventory(labels=("self-hosted", "launchplane", "launchplane-managed")),
            baseline_readiness=evaluate_runner_lane_baseline(
                policy=RunnerLaneBaselinePolicy(), observations=()
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(
            [blocker.code for blocker in plan.blockers],
            ["baseline_not_ready"],
        )

    def test_create_plan_blocks_when_lane_already_exists(self) -> None:
        plan = plan_runner_lane_control(
            policy=RunnerLaneControlPolicy(
                allowed_repositories=("cbusillo/repo",),
                allow_create=True,
            ),
            request=RunnerLaneControlRequest(
                action="create",
                repository="cbusillo/repo",
                lane_name="lane-1",
                mutate=True,
            ),
            inventory=_inventory(labels=("self-hosted", "launchplane", "launchplane-managed")),
            baseline_readiness=_ready_baseline(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(
            [blocker.code for blocker in plan.blockers],
            ["lane_already_exists"],
        )

    def test_control_plan_rejects_inventory_for_another_repository(self) -> None:
        plan = plan_runner_lane_control(
            policy=RunnerLaneControlPolicy(
                allowed_repositories=("cbusillo/repo",),
                allow_restart=True,
            ),
            request=RunnerLaneControlRequest(
                action="restart",
                repository="cbusillo/repo",
                lane_name="lane-1",
                mutate=True,
            ),
            inventory=_inventory(
                repository="cbusillo/other",
                labels=("self-hosted", "launchplane", "launchplane-managed"),
            ),
            baseline_readiness=_ready_baseline(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(
            [blocker.code for blocker in plan.blockers],
            ["inventory_repository_mismatch"],
        )


class RunnerLaneControlPlanCliTests(unittest.TestCase):
    def test_cli_builds_runner_control_plan_from_inventory_and_baseline_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            inventory_file = Path(temp_dir) / "inventory.json"
            baseline_file = Path(temp_dir) / "baseline.json"
            inventory_file.write_text(
                json.dumps(
                    _inventory(
                        repository=" cbusillo / repo ",
                        labels=("self-hosted", "launchplane", "launchplane-managed"),
                    ).model_dump(mode="json")
                ),
                encoding="utf-8",
            )
            baseline_file.write_text(
                json.dumps({"readiness": _ready_baseline().model_dump(mode="json")}),
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "runner-control-plan",
                    "--action",
                    "restart",
                    "--repository",
                    " cbusillo / repo ",
                    "--lane-name",
                    "lane-1",
                    "--mutate",
                    "--allow-restart",
                    "--allowed-repository",
                    "cbusillo/repo",
                    "--inventory-file",
                    str(inventory_file),
                    "--baseline-readiness-file",
                    str(baseline_file),
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["request"]["repository"], "cbusillo/repo")
        self.assertEqual(payload["plan"]["status"], "ready")
        self.assertEqual(payload["plan"]["blockers"], [])

    def test_cli_keeps_runner_control_plan_dry_run_by_default(self) -> None:
        with TemporaryDirectory() as temp_dir:
            inventory_file = Path(temp_dir) / "inventory.json"
            baseline_file = Path(temp_dir) / "baseline.json"
            inventory_file.write_text(
                json.dumps(
                    _inventory(
                        labels=("self-hosted", "launchplane", "launchplane-managed")
                    ).model_dump(mode="json")
                ),
                encoding="utf-8",
            )
            baseline_file.write_text(
                json.dumps(_ready_baseline().model_dump(mode="json")),
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "runner-control-plan",
                    "--action",
                    "restart",
                    "--repository",
                    "cbusillo/repo",
                    "--lane-name",
                    "lane-1",
                    "--allow-restart",
                    "--allowed-repository",
                    "cbusillo/repo",
                    "--inventory-file",
                    str(inventory_file),
                    "--baseline-readiness-file",
                    str(baseline_file),
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertFalse(payload["request"]["mutate"])
        self.assertEqual(payload["plan"]["status"], "blocked")
        self.assertEqual(
            [blocker["code"] for blocker in payload["plan"]["blockers"]],
            ["mutate_not_requested"],
        )


def _ready_baseline() -> RunnerLaneBaselineReadiness:
    return evaluate_runner_lane_baseline(
        policy=RunnerLaneBaselinePolicy(),
        observations=(
            RunnerLaneBaselineObservation(
                runner_name="lane-1",
                labels=("self-hosted", "launchplane"),
                docker_config_isolated=True,
                observed_at="2026-05-18T20:30:00Z",
            ),
        ),
    )


def _inventory(
    *,
    repository: str = "cbusillo/repo",
    labels: tuple[str, ...],
    busy: bool = False,
    extra_lanes: tuple[RunnerLaneRecord, ...] = (),
) -> RunnerLaneInventory:
    return build_runner_lane_inventory(
        repository=repository,
        observed_at="2026-05-18T20:30:00Z",
        lanes=(
            RunnerLaneRecord(
                github_id=1,
                name="lane-1",
                repository=repository,
                status="online",
                busy=busy,
                labels=labels,
                observed_at="2026-05-18T20:30:00Z",
            ),
            *extra_lanes,
        ),
    )


if __name__ == "__main__":
    unittest.main()
