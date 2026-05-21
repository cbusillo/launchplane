from datetime import datetime, timezone
import json
import unittest
from typing import cast
from unittest.mock import patch

from click import Command
from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.runner_lane_inventory import RunnerLaneInventory
from control_plane.runner_lane_github import GitHubRunnerLaneInventoryReader
from control_plane.merge_train_github import MergeTrainGitHubError
from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport


CLI_MAIN = cast(Command, main)


class _FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 5, 9, 12, 45, tzinfo=timezone.utc)


class GitHubRunnerLaneInventoryReaderTests(unittest.TestCase):
    def test_reader_builds_capacity_summary_from_github_runners(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                {
                    "total_count": 3,
                    "runners": [
                        _runner(101, "chris-testing-syo-1", busy=True),
                        _runner(102, "chris-testing-syo-2", busy=False),
                        _runner(103, "chris-testing-syo-3", status="offline"),
                    ],
                },
            )
        )

        inventory = GitHubRunnerLaneInventoryReader(
            transport=transport, clock=_FixedClock()
        ).read_runner_lane_inventory(repository="cbusillo/sellyouroutboard")

        self.assertEqual(inventory.repository, "cbusillo/sellyouroutboard")
        self.assertEqual(inventory.observed_at, "2026-05-09T12:45:00Z")
        self.assertEqual(inventory.total_lanes, 3)
        self.assertEqual(inventory.online_lanes, 2)
        self.assertEqual(inventory.busy_lanes, 1)
        self.assertEqual(inventory.idle_lanes, 1)
        self.assertEqual(inventory.offline_lanes, 1)
        self.assertFalse(inventory.capacity_constrained)
        self.assertEqual(inventory.lanes[0].host_hint, "chris-testing")
        self.assertEqual(
            [request.path for request in transport.requests],
            ["/repos/cbusillo/sellyouroutboard/actions/runners?per_page=100&page=1"],
        )

    def test_reader_marks_all_busy_online_lanes_as_capacity_constrained(self) -> None:
        inventory = GitHubRunnerLaneInventoryReader(
            transport=RecordingMergeTrainGitHubTransport(
                responses=({"total_count": 1, "runners": [_runner(201, "lane-1", busy=True)]},)
            ),
            clock=_FixedClock(),
        ).read_runner_lane_inventory(repository="cbusillo/repo")

        self.assertTrue(inventory.capacity_constrained)
        self.assertEqual(inventory.capacity_reason, "all online self-hosted runner lanes are busy")

    def test_reader_paginates_runner_results(self) -> None:
        first_page = [_runner(number, f"lane-{number}") for number in range(1, 101)]
        second_page = [_runner(101, "lane-101")]
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                {"total_count": 101, "runners": first_page},
                {"total_count": 101, "runners": second_page},
            )
        )

        inventory = GitHubRunnerLaneInventoryReader(
            transport=transport, clock=_FixedClock()
        ).read_runner_lane_inventory(repository="cbusillo/repo")

        self.assertEqual(inventory.total_lanes, 101)
        self.assertEqual(
            [request.path for request in transport.requests],
            [
                "/repos/cbusillo/repo/actions/runners?per_page=100&page=1",
                "/repos/cbusillo/repo/actions/runners?per_page=100&page=2",
            ],
        )

    def test_reader_normalizes_pasted_repository_components(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=({"total_count": 1, "runners": [_runner(301, "lane-1")]},)
        )

        inventory = GitHubRunnerLaneInventoryReader(
            transport=transport, clock=_FixedClock()
        ).read_runner_lane_inventory(repository=" cbusillo / launchplane ")

        self.assertEqual(inventory.repository, "cbusillo/launchplane")
        self.assertEqual(inventory.lanes[0].repository, "cbusillo/launchplane")
        self.assertEqual(
            [request.path for request in transport.requests],
            ["/repos/cbusillo/launchplane/actions/runners?per_page=100&page=1"],
        )

    def test_reader_rejects_malformed_runner_response(self) -> None:
        reader = GitHubRunnerLaneInventoryReader(
            transport=RecordingMergeTrainGitHubTransport(responses=({"runners": {}},)),
            clock=_FixedClock(),
        )

        with self.assertRaisesRegex(MergeTrainGitHubError, "must include runners"):
            reader.read_runner_lane_inventory(repository="cbusillo/repo")

    def test_reader_rejects_missing_busy_flag(self) -> None:
        runner = _runner(401, "lane-1")
        runner.pop("busy")
        reader = GitHubRunnerLaneInventoryReader(
            transport=RecordingMergeTrainGitHubTransport(
                responses=({"total_count": 1, "runners": [runner]},)
            ),
            clock=_FixedClock(),
        )

        with self.assertRaisesRegex(MergeTrainGitHubError, "requires busy"):
            reader.read_runner_lane_inventory(repository="cbusillo/repo")

    def test_reader_rejects_non_boolean_busy_flag(self) -> None:
        runner = _runner(402, "lane-1")
        runner["busy"] = "false"
        reader = GitHubRunnerLaneInventoryReader(
            transport=RecordingMergeTrainGitHubTransport(
                responses=({"total_count": 1, "runners": [runner]},)
            ),
            clock=_FixedClock(),
        )

        with self.assertRaisesRegex(MergeTrainGitHubError, "requires busy"):
            reader.read_runner_lane_inventory(repository="cbusillo/repo")


class RunnerLaneInventoryCliTests(unittest.TestCase):
    def test_cli_prints_runner_inventory_json(self) -> None:
        class _FakeInventoryReader:
            def __init__(self, *, transport: object) -> None:
                self.transport = transport

            def read_runner_lane_inventory(self, *, repository: str) -> RunnerLaneInventory:
                return GitHubRunnerLaneInventoryReader(
                    transport=RecordingMergeTrainGitHubTransport(
                        responses=({"total_count": 1, "runners": [_runner(301, "lane-1")]},)
                    ),
                    clock=_FixedClock(),
                ).read_runner_lane_inventory(repository=repository)

        with (
            patch(
                "control_plane.cli_runner_lanes.UrllibMergeTrainGitHubTransport",
                return_value=object(),
            ),
            patch(
                "control_plane.cli_runner_lanes.GitHubRunnerLaneInventoryReader",
                _FakeInventoryReader,
            ),
            patch.dict("os.environ", {"GITHUB_TOKEN": "token"}, clear=True),
        ):
            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "runner-inventory",
                    "--repository",
                    "cbusillo/repo",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["repository"], "cbusillo/repo")
        self.assertEqual(payload["total_lanes"], 1)
        self.assertEqual(payload["idle_lanes"], 1)

    def test_cli_requires_github_token(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            result = CliRunner().invoke(
                CLI_MAIN,
                ["work-graph", "runner-inventory", "--repository", "cbusillo/repo"],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Missing GitHub token", result.output)


def _runner(
    runner_id: int, name: str, *, status: str = "online", busy: bool = False
) -> dict[str, object]:
    return {
        "id": runner_id,
        "name": name,
        "status": status,
        "busy": busy,
        "labels": [{"name": "self-hosted"}, {"name": "launchplane"}],
    }


if __name__ == "__main__":
    unittest.main()
