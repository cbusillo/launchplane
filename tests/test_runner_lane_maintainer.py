from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from click import Command
from click.testing import CliRunner
from click.testing import Result

from control_plane.cli import main
from control_plane.contracts.runner_lane_baseline import RunnerLaneBaselineReadiness
from control_plane.contracts.runner_lane_inventory import RunnerLaneInventory
from control_plane.contracts.runner_lane_inventory import RunnerLaneRecord
from control_plane.contracts.runner_lane_inventory import build_runner_lane_inventory
from control_plane.contracts.runner_lane_maintainer import RunnerLaneDesiredState
from control_plane.contracts.runner_lane_maintainer import RunnerLaneMaintainerPolicy
from control_plane.contracts.runner_lane_maintainer import plan_runner_lane_maintainer


CLI_MAIN = cast(Command, main)


class RunnerLaneMaintainerPlanTests(unittest.TestCase):
    def test_plan_decides_create_when_inventory_is_empty(self) -> None:
        plan = plan_runner_lane_maintainer(
            policy=_policy(),
            desired_state=_desired_state(),
            inventory=_inventory(lanes=()),
            baseline_readiness=_ready_baseline(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.decision, "recommend_create")
        self.assertTrue(plan.policy_ready)
        self.assertFalse(plan.capability_ready)
        self.assertEqual(
            [blocker.code for blocker in plan.blockers], ["supervised_maintainer_required"]
        )
        self.assertIn("systemd-backed", " ".join(plan.next_steps))

    def test_plan_decides_verify_adoption_for_online_managed_lane(self) -> None:
        plan = plan_runner_lane_maintainer(
            policy=_policy(),
            desired_state=_desired_state(),
            inventory=_inventory(lanes=(_lane(status="online"),)),
            baseline_readiness=_ready_baseline(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.decision, "recommend_verify_adoption")
        self.assertTrue(plan.policy_ready)
        self.assertFalse(plan.capability_ready)
        self.assertEqual(
            [blocker.code for blocker in plan.blockers], ["supervised_maintainer_required"]
        )
        self.assertIn("systemd unit", " ".join(plan.next_steps))

    def test_plan_decides_remove_recreate_for_offline_managed_lane(self) -> None:
        plan = plan_runner_lane_maintainer(
            policy=_policy(),
            desired_state=_desired_state(),
            inventory=_inventory(lanes=(_lane(status="offline", github_id=21),)),
            baseline_readiness=_ready_baseline(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.decision, "recommend_remove_recreate")
        self.assertEqual(plan.matching_lanes[0].github_id, 21)
        self.assertTrue(plan.policy_ready)
        self.assertFalse(plan.capability_ready)
        self.assertEqual(
            [blocker.code for blocker in plan.blockers], ["supervised_maintainer_required"]
        )
        self.assertIn("recreate", " ".join(plan.next_steps))

    def test_plan_blocks_existing_unmanaged_lane(self) -> None:
        plan = plan_runner_lane_maintainer(
            policy=_policy(),
            desired_state=_desired_state(),
            inventory=_inventory(lanes=(_lane(labels=("self-hosted", "launchplane")),)),
            baseline_readiness=_ready_baseline(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.decision, "blocked")
        self.assertFalse(plan.policy_ready)
        self.assertTrue(plan.capability_ready)
        self.assertEqual([blocker.code for blocker in plan.blockers], ["existing_lane_not_managed"])

    def test_plan_recommends_create_before_baseline_is_available(self) -> None:
        plan = plan_runner_lane_maintainer(
            policy=_policy(),
            desired_state=_desired_state(),
            inventory=_inventory(lanes=()),
            baseline_readiness=_unavailable_baseline(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.decision, "recommend_create")
        self.assertTrue(plan.policy_ready)
        self.assertFalse(plan.capability_ready)
        self.assertEqual(
            [blocker.code for blocker in plan.blockers], ["supervised_maintainer_required"]
        )

    def test_plan_blocks_offline_lane_without_baseline_readiness(self) -> None:
        plan = plan_runner_lane_maintainer(
            policy=_policy(),
            desired_state=_desired_state(),
            inventory=_inventory(lanes=(_lane(status="offline", github_id=21),)),
            baseline_readiness=_unavailable_baseline(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.decision, "blocked")
        self.assertFalse(plan.policy_ready)
        self.assertTrue(plan.capability_ready)
        self.assertEqual([blocker.code for blocker in plan.blockers], ["baseline_not_ready"])

    def test_plan_blocks_online_lane_without_baseline_readiness(self) -> None:
        plan = plan_runner_lane_maintainer(
            policy=_policy(),
            desired_state=_desired_state(),
            inventory=_inventory(lanes=(_lane(status="online"),)),
            baseline_readiness=_unavailable_baseline(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.decision, "blocked")
        self.assertFalse(plan.policy_ready)
        self.assertTrue(plan.capability_ready)
        self.assertEqual([blocker.code for blocker in plan.blockers], ["baseline_not_ready"])

    def test_plan_blocks_lane_with_unknown_status(self) -> None:
        plan = plan_runner_lane_maintainer(
            policy=_policy(),
            desired_state=_desired_state(),
            inventory=_inventory(lanes=(_lane(status="unknown"),)),
            baseline_readiness=_ready_baseline(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.decision, "blocked")
        self.assertFalse(plan.policy_ready)
        self.assertTrue(plan.capability_ready)
        self.assertEqual([blocker.code for blocker in plan.blockers], ["lane_status_unknown"])

    def test_plan_can_disable_baseline_gate_for_online_lane(self) -> None:
        plan = plan_runner_lane_maintainer(
            policy=_policy(require_baseline_readiness=False),
            desired_state=_desired_state(),
            inventory=_inventory(lanes=(_lane(status="online"),)),
            baseline_readiness=_unavailable_baseline(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.decision, "recommend_verify_adoption")
        self.assertTrue(plan.policy_ready)
        self.assertFalse(plan.capability_ready)
        self.assertEqual(
            [blocker.code for blocker in plan.blockers], ["supervised_maintainer_required"]
        )

    def test_plan_blocks_unsafe_or_out_of_policy_state(self) -> None:
        plan = plan_runner_lane_maintainer(
            policy=_policy(approved_hosts=("chris-testing",)),
            desired_state=_desired_state(host_name="other-host"),
            inventory=_inventory(lanes=()),
            baseline_readiness=_ready_baseline(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual([blocker.code for blocker in plan.blockers], ["host_not_allowed"])

    def test_plan_blocks_ambiguous_lane_name(self) -> None:
        plan = plan_runner_lane_maintainer(
            policy=_policy(),
            desired_state=_desired_state(),
            inventory=_inventory(
                lanes=(
                    _lane(status="offline", github_id=21),
                    _lane(status="online", github_id=22, name="CM-WEBSITE-CHRIS-TESTING"),
                )
            ),
            baseline_readiness=_ready_baseline(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.decision, "blocked")
        self.assertFalse(plan.policy_ready)
        self.assertTrue(plan.capability_ready)
        self.assertEqual([blocker.code for blocker in plan.blockers], ["lane_name_ambiguous"])

    def test_plan_requires_explicit_allowed_service_user(self) -> None:
        plan = plan_runner_lane_maintainer(
            policy=RunnerLaneMaintainerPolicy(
                allowed_repositories=("cbusillo/odoo-tenant-cm-website",),
                approved_hosts=("chris-testing",),
                allowed_registration_roots=("/home/launchplane-runner-hygiene/actions-runners",),
                allowed_service_users=(),
            ),
            desired_state=_desired_state(),
            inventory=_inventory(lanes=()),
            baseline_readiness=_ready_baseline(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertFalse(plan.policy_ready)
        self.assertTrue(plan.capability_ready)
        self.assertEqual([blocker.code for blocker in plan.blockers], ["service_user_not_allowed"])

    def test_desired_state_requires_systemd_template_unit(self) -> None:
        with self.assertRaisesRegex(ValueError, "launchplane-runner"):
            _desired_state(systemd_unit_name="actions.runner.cm.service")

    def test_desired_state_requires_full_runner_directory(self) -> None:
        with self.assertRaisesRegex(ValueError, "lane directory"):
            RunnerLaneDesiredState(
                repository="cbusillo/odoo-tenant-cm-website",
                host_name="chris-testing",
                lane_name="cm-website-chris-testing",
                runner_directory="/home/launchplane-runner-hygiene/actions-runners",
                service_user="launchplane-runner-hygiene",
                systemd_unit_name=_UNIT_NAME,
                labels=("self-hosted", "launchplane", "launchplane-managed"),
            )

    def test_desired_state_rejects_parent_path_components(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not contain '\\.\\.' components"):
            RunnerLaneDesiredState(
                repository="cbusillo/odoo-tenant-cm-website",
                host_name="chris-testing",
                lane_name="cm-website-chris-testing",
                runner_directory="/home/launchplane-runner-hygiene/actions-runners/../cm-website-chris-testing",
                service_user="launchplane-runner-hygiene",
                systemd_unit_name=_UNIT_NAME,
                labels=("self-hosted", "launchplane", "launchplane-managed"),
            )

    def test_policy_rejects_parent_path_components_in_allowed_roots(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not contain '\\.\\.' components"):
            RunnerLaneMaintainerPolicy(
                allowed_registration_roots=("/home/launchplane-runner-hygiene/../actions-runners",),
            )

    def test_desired_state_requires_systemd_unit_for_lane(self) -> None:
        with self.assertRaisesRegex(ValueError, "lane name"):
            _desired_state(systemd_unit_name="launchplane-runner@other-lane.service")


class RunnerLaneMaintainerCliTests(unittest.TestCase):
    def test_cli_emits_create_decision_for_empty_inventory(self) -> None:
        result = _invoke_cli(lanes=())

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["plan"]["status"], "blocked")
        self.assertEqual(payload["plan"]["decision"], "recommend_create")
        self.assertTrue(payload["plan"]["policy_ready"])
        self.assertFalse(payload["plan"]["capability_ready"])
        self.assertEqual(payload["plan"]["blockers"][0]["code"], "supervised_maintainer_required")
        self.assertEqual(payload["desired_state"]["systemd_unit_name"], _UNIT_NAME)

    def test_cli_emits_remove_recreate_decision_for_offline_lane(self) -> None:
        result = _invoke_cli(lanes=(_lane(status="offline", github_id=21),))

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["plan"]["decision"], "recommend_remove_recreate")
        self.assertEqual(payload["plan"]["matching_lanes"][0]["github_id"], 21)

    def test_cli_uses_nested_baseline_readiness_envelope(self) -> None:
        result = _invoke_cli(lanes=(), nested_baseline=True)

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["plan"]["decision"], "recommend_create")

    def test_cli_blocks_existing_lane_without_baseline_readiness(self) -> None:
        result = _invoke_cli(
            lanes=(_lane(status="online"),),
            baseline_readiness=_unavailable_baseline(),
        )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["plan"]["decision"], "blocked")
        self.assertEqual(payload["plan"]["blockers"][0]["code"], "baseline_not_ready")

    def test_cli_can_disable_baseline_gate_for_existing_lane(self) -> None:
        result = _invoke_cli(
            lanes=(_lane(status="online"),),
            baseline_readiness=_unavailable_baseline(),
            allow_missing_baseline=True,
        )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["plan"]["decision"], "recommend_verify_adoption")
        self.assertEqual(
            [blocker["code"] for blocker in payload["plan"]["blockers"]],
            ["supervised_maintainer_required"],
        )


_UNIT_NAME = "launchplane-runner@cm-website-chris-testing.service"


def _policy(
    *,
    approved_hosts: tuple[str, ...] = ("chris-testing",),
    require_baseline_readiness: bool = True,
) -> RunnerLaneMaintainerPolicy:
    return RunnerLaneMaintainerPolicy(
        allowed_repositories=("cbusillo/odoo-tenant-cm-website",),
        approved_hosts=approved_hosts,
        allowed_registration_roots=("/home/launchplane-runner-hygiene/actions-runners",),
        allowed_service_users=("launchplane-runner-hygiene",),
        require_baseline_readiness=require_baseline_readiness,
    )


def _desired_state(
    *,
    host_name: str = "chris-testing",
    systemd_unit_name: str = _UNIT_NAME,
) -> RunnerLaneDesiredState:
    return RunnerLaneDesiredState(
        repository="cbusillo/odoo-tenant-cm-website",
        host_name=host_name,
        lane_name="cm-website-chris-testing",
        runner_directory="/home/launchplane-runner-hygiene/actions-runners/cm-website-chris-testing",
        service_user="launchplane-runner-hygiene",
        systemd_unit_name=systemd_unit_name,
        labels=(
            "self-hosted",
            "launchplane",
            "launchplane-managed",
            "chris-testing",
            "cm-website",
        ),
    )


def _ready_baseline() -> RunnerLaneBaselineReadiness:
    return RunnerLaneBaselineReadiness(
        ready=True,
        observed_lanes=1,
        compliant_lanes=1,
        violations=(),
        summary="runner lane baseline satisfied",
    )


def _unavailable_baseline() -> RunnerLaneBaselineReadiness:
    return RunnerLaneBaselineReadiness(
        ready=False,
        observed_lanes=0,
        compliant_lanes=0,
        violations=(),
        summary="no runner lane baseline observations supplied",
    )


def _inventory(*, lanes: tuple[RunnerLaneRecord, ...]) -> RunnerLaneInventory:
    return build_runner_lane_inventory(
        repository="cbusillo/odoo-tenant-cm-website",
        observed_at="2026-06-08T21:15:00Z",
        lanes=lanes,
    )


def _lane(
    *,
    status: str = "online",
    github_id: int = 1,
    name: str = "cm-website-chris-testing",
    labels: tuple[str, ...] = (
        "self-hosted",
        "launchplane",
        "launchplane-managed",
    ),
) -> RunnerLaneRecord:
    return RunnerLaneRecord(
        github_id=github_id,
        name=name,
        repository="cbusillo/odoo-tenant-cm-website",
        status=status,
        busy=False,
        labels=labels,
        host_hint="chris-testing",
        observed_at="2026-06-08T21:15:00Z",
    )


def _invoke_cli(
    *,
    lanes: tuple[RunnerLaneRecord, ...],
    nested_baseline: bool = False,
    baseline_readiness: RunnerLaneBaselineReadiness | None = None,
    allow_missing_baseline: bool = False,
) -> Result:
    with TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        inventory_file = temp_path / "inventory.json"
        baseline_file = temp_path / "baseline.json"
        inventory_file.write_text(
            json.dumps(_inventory(lanes=lanes).model_dump(mode="json")),
            encoding="utf-8",
        )
        baseline_payload: dict[str, object] = (baseline_readiness or _ready_baseline()).model_dump(
            mode="json"
        )
        if nested_baseline:
            baseline_payload = {"readiness": baseline_payload}
        baseline_file.write_text(json.dumps(baseline_payload), encoding="utf-8")

        arguments = [
            "work-graph",
            "runner-maintainer-plan",
            "--repository",
            "cbusillo/odoo-tenant-cm-website",
            "--host-name",
            "chris-testing",
            "--lane-name",
            "cm-website-chris-testing",
            "--runner-directory",
            "/home/launchplane-runner-hygiene/actions-runners/cm-website-chris-testing",
            "--service-user",
            "launchplane-runner-hygiene",
            "--systemd-unit-name",
            _UNIT_NAME,
            "--label",
            "self-hosted",
            "--label",
            "launchplane",
            "--label",
            "launchplane-managed",
            "--label",
            "chris-testing",
            "--label",
            "cm-website",
            "--allowed-repository",
            "cbusillo/odoo-tenant-cm-website",
            "--approved-host",
            "chris-testing",
            "--allowed-registration-root",
            "/home/launchplane-runner-hygiene/actions-runners",
            "--allowed-service-user",
            "launchplane-runner-hygiene",
            "--inventory-file",
            str(inventory_file),
            "--baseline-readiness-file",
            str(baseline_file),
        ]
        if allow_missing_baseline:
            arguments.append("--allow-missing-baseline-readiness")
        return CliRunner().invoke(CLI_MAIN, arguments)


if __name__ == "__main__":
    unittest.main()
