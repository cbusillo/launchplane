import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest

from click import Command
from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneObservation
from control_plane.contracts.runner_host_hygiene import RunnerHostHygienePolicy
from control_plane.contracts.runner_host_hygiene import evaluate_runner_host_hygiene


CLI_MAIN = cast(Command, main)


class RunnerHostHygieneTests(unittest.TestCase):
    def test_report_is_healthy_when_observation_satisfies_policy(self) -> None:
        report = evaluate_runner_host_hygiene(
            policy=RunnerHostHygienePolicy(
                minimum_free_disk_bytes=100,
                maximum_docker_reclaimable_bytes=50,
                maximum_runner_workdir_bytes=75,
                required_warm_builders=("odoo-docker-chris-testing",),
            ),
            observation=RunnerHostHygieneObservation(
                host_name="chris-testing",
                observed_at="2026-05-23T13:00:00Z",
                free_disk_bytes=200,
                docker_reclaimable_bytes=25,
                runner_workdir_bytes=50,
                warm_builders=(" Odoo-Docker-Chris-Testing ",),
            ),
        )

        self.assertEqual(report.status, "healthy")
        self.assertEqual(report.host_name, "chris-testing")
        self.assertEqual(report.findings, ())
        self.assertIn("report-only", report.summary)

    def test_report_finds_missing_builder_and_low_free_disk(self) -> None:
        report = evaluate_runner_host_hygiene(
            policy=RunnerHostHygienePolicy(
                minimum_free_disk_bytes=500,
                required_warm_builders=("odoo-docker-chris-testing",),
            ),
            observation=RunnerHostHygieneObservation(
                host_name="chris-testing",
                observed_at="2026-05-23T13:00:00Z",
                free_disk_bytes=100,
                warm_builders=(),
            ),
        )

        self.assertEqual(report.status, "attention")
        self.assertEqual(
            [finding.code for finding in report.findings],
            ["free_disk_below_minimum", "required_warm_builder_missing"],
        )

    def test_report_flags_orphan_buildkit_by_default(self) -> None:
        report = evaluate_runner_host_hygiene(
            policy=RunnerHostHygienePolicy(),
            observation=RunnerHostHygieneObservation(
                host_name="chris-testing",
                observed_at="2026-05-23T13:00:00Z",
                free_disk_bytes=100,
                orphan_buildkit_containers=1,
                orphan_buildkit_volumes=2,
            ),
        )

        self.assertEqual(report.status, "attention")
        self.assertEqual(
            [finding.code for finding in report.findings],
            ["orphan_buildkit_present"],
        )

    def test_report_can_allow_orphan_buildkit_for_observation_only(self) -> None:
        report = evaluate_runner_host_hygiene(
            policy=RunnerHostHygienePolicy(allow_orphan_buildkit=True),
            observation=RunnerHostHygieneObservation(
                host_name="chris-testing",
                observed_at="2026-05-23T13:00:00Z",
                free_disk_bytes=100,
                orphan_buildkit_containers=1,
            ),
        )

        self.assertEqual(report.status, "healthy")


class RunnerHostHygieneCliTests(unittest.TestCase):
    def test_cli_builds_report_from_observation_and_flags(self) -> None:
        with TemporaryDirectory() as temp_dir:
            observation_file = Path(temp_dir) / "observation.json"
            observation_file.write_text(
                json.dumps(
                    RunnerHostHygieneObservation(
                        host_name="chris-testing",
                        observed_at="2026-05-23T13:00:00Z",
                        free_disk_bytes=100,
                        docker_reclaimable_bytes=80,
                    ).model_dump(mode="json")
                ),
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "runner-host-hygiene-report",
                    "--observation-file",
                    observation_file.as_posix(),
                    "--minimum-free-disk-bytes",
                    "200",
                    "--maximum-docker-reclaimable-bytes",
                    "50",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["mode"], "report-only")
        self.assertEqual(payload["report"]["status"], "attention")
        self.assertEqual(
            [finding["code"] for finding in payload["report"]["findings"]],
            ["docker_reclaimable_above_limit", "free_disk_below_minimum"],
        )

    def test_cli_accepts_policy_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            observation_file = root / "observation.json"
            policy_file = root / "policy.json"
            observation_file.write_text(
                json.dumps(
                    RunnerHostHygieneObservation(
                        host_name="chris-testing",
                        observed_at="2026-05-23T13:00:00Z",
                        free_disk_bytes=500,
                        warm_builders=("odoo-enterprise-chris-testing",),
                    ).model_dump(mode="json")
                ),
                encoding="utf-8",
            )
            policy_file.write_text(
                json.dumps(
                    RunnerHostHygienePolicy(
                        required_warm_builders=("odoo-enterprise-chris-testing",)
                    ).model_dump(mode="json")
                ),
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "runner-host-hygiene-report",
                    "--observation-file",
                    observation_file.as_posix(),
                    "--policy-file",
                    policy_file.as_posix(),
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["report"]["status"], "healthy")


if __name__ == "__main__":
    unittest.main()
