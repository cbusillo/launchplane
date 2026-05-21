from datetime import datetime, timezone
import json
import unittest
from typing import cast
from unittest.mock import patch

from click import Command
from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.runner_queue_wait import build_runner_queue_wait_job
from control_plane.contracts.runner_queue_wait import build_runner_queue_wait_summary
from control_plane.merge_train_github import MergeTrainGitHubError
from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.runner_queue_wait_github import GitHubRunnerQueueWaitReader


CLI_MAIN = cast(Command, main)


class _FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 5, 18, 14, 30, tzinfo=timezone.utc)


class RunnerQueueWaitContractTests(unittest.TestCase):
    def test_summary_reports_normal_queue_waits_below_threshold(self) -> None:
        summary = build_runner_queue_wait_summary(
            repository="cbusillo/launchplane",
            observed_at="2026-05-18T14:30:00Z",
            workflow_runs_scanned=1,
            jobs=(
                build_runner_queue_wait_job(
                    github_id=101,
                    run_id=1001,
                    repository="cbusillo/launchplane",
                    job_name="test",
                    status="completed",
                    created_at="2026-05-18T14:00:00Z",
                    started_at="2026-05-18T14:00:42Z",
                ),
            ),
            constrained_threshold_seconds=300,
        )

        self.assertEqual(summary.queue_wait_status, "not_capacity_constrained")
        self.assertFalse(summary.capacity_constrained)
        self.assertEqual(summary.known_wait_jobs, 1)
        self.assertEqual(summary.unknown_wait_jobs, 0)
        self.assertEqual(summary.max_queue_wait_seconds, 42)

    def test_missing_timestamps_are_unknown_not_zero(self) -> None:
        summary = build_runner_queue_wait_summary(
            repository="cbusillo/launchplane",
            observed_at="2026-05-18T14:30:00Z",
            workflow_runs_scanned=1,
            jobs=(
                build_runner_queue_wait_job(
                    github_id=102,
                    run_id=1001,
                    repository="cbusillo/launchplane",
                    job_name="static_checks",
                    status="queued",
                    created_at="2026-05-18T14:00:00Z",
                ),
            ),
        )

        self.assertEqual(summary.queue_wait_status, "unknown")
        self.assertIsNone(summary.capacity_constrained)
        self.assertEqual(summary.known_wait_jobs, 0)
        self.assertEqual(summary.unknown_wait_jobs, 1)
        self.assertIsNone(summary.max_queue_wait_seconds)
        self.assertEqual(summary.jobs[0].queue_wait_reason, "missing started_at")

    def test_summary_marks_capacity_constrained_sample(self) -> None:
        summary = build_runner_queue_wait_summary(
            repository="cbusillo/launchplane",
            observed_at="2026-05-18T14:30:00Z",
            workflow_runs_scanned=1,
            jobs=(
                build_runner_queue_wait_job(
                    github_id=103,
                    run_id=1002,
                    repository="cbusillo/launchplane",
                    job_name="container_scan",
                    status="completed",
                    created_at="2026-05-18T14:00:00Z",
                    started_at="2026-05-18T14:07:00Z",
                ),
            ),
            constrained_threshold_seconds=300,
        )

        self.assertEqual(summary.queue_wait_status, "capacity_constrained")
        self.assertTrue(summary.capacity_constrained)
        self.assertEqual(summary.max_queue_wait_seconds, 420)
        self.assertIn("meets or exceeds", summary.capacity_reason)


class GitHubRunnerQueueWaitReaderTests(unittest.TestCase):
    def test_reader_builds_summary_from_actions_runs_and_jobs(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                {"workflow_runs": [{"id": 1001, "name": "CI"}]},
                {
                    "jobs": [
                        _job(
                            501,
                            "test",
                            created_at="2026-05-18T14:00:00Z",
                            started_at="2026-05-18T14:01:00Z",
                        ),
                    ],
                },
            )
        )

        summary = GitHubRunnerQueueWaitReader(
            transport=transport, clock=_FixedClock()
        ).read_runner_queue_wait(
            repository=" cbusillo / launchplane ",
            workflow_run_limit=10,
            inventory_capacity_constrained=True,
            inventory_capacity_reason="all online self-hosted runner lanes are busy",
        )

        self.assertEqual(summary.repository, "cbusillo/launchplane")
        self.assertEqual(summary.observed_at, "2026-05-18T14:30:00Z")
        self.assertEqual(summary.workflow_runs_scanned, 1)
        self.assertEqual(summary.jobs_scanned, 1)
        self.assertEqual(summary.jobs[0].workflow_name, "CI")
        self.assertEqual(summary.jobs[0].queue_wait_seconds, 60)
        self.assertTrue(summary.inventory_capacity_constrained)
        self.assertEqual(
            [request.path for request in transport.requests],
            [
                "/repos/cbusillo/launchplane/actions/runs?per_page=10",
                "/repos/cbusillo/launchplane/actions/runs/1001/jobs?per_page=100&page=1",
            ],
        )

    def test_reader_rejects_malformed_jobs_response(self) -> None:
        reader = GitHubRunnerQueueWaitReader(
            transport=RecordingMergeTrainGitHubTransport(
                responses=({"workflow_runs": [{"id": 1001}]}, {"jobs": {}})
            ),
            clock=_FixedClock(),
        )

        with self.assertRaisesRegex(MergeTrainGitHubError, "must include jobs"):
            reader.read_runner_queue_wait(repository="cbusillo/repo")


class RunnerQueueWaitCliTests(unittest.TestCase):
    def test_cli_prints_runner_queue_wait_json(self) -> None:
        class _FakeQueueWaitReader:
            def __init__(self, *, transport: object) -> None:
                self.transport = transport

            def read_runner_queue_wait(
                self,
                *,
                repository: str,
                workflow_run_limit: int,
                constrained_threshold_seconds: int,
                inventory_capacity_constrained: bool | None,
                inventory_capacity_reason: str,
            ) -> object:
                return GitHubRunnerQueueWaitReader(
                    transport=RecordingMergeTrainGitHubTransport(
                        responses=(
                            {"workflow_runs": [{"id": 1001, "name": "CI"}]},
                            {
                                "jobs": [
                                    _job(
                                        501,
                                        "test",
                                        created_at="2026-05-18T14:00:00Z",
                                        started_at="2026-05-18T14:02:00Z",
                                    )
                                ]
                            },
                        )
                    ),
                    clock=_FixedClock(),
                ).read_runner_queue_wait(
                    repository=repository,
                    workflow_run_limit=workflow_run_limit,
                    constrained_threshold_seconds=constrained_threshold_seconds,
                    inventory_capacity_constrained=inventory_capacity_constrained,
                    inventory_capacity_reason=inventory_capacity_reason,
                )

        with (
            patch(
                "control_plane.cli_runner_lanes.UrllibMergeTrainGitHubTransport",
                return_value=object(),
            ),
            patch(
                "control_plane.cli_runner_lanes.GitHubRunnerQueueWaitReader",
                _FakeQueueWaitReader,
            ),
            patch.dict("os.environ", {"GITHUB_TOKEN": "token"}, clear=True),
        ):
            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "runner-queue-wait",
                    "--repository",
                    "cbusillo/repo",
                    "--skip-runner-inventory",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["repository"], "cbusillo/repo")
        self.assertEqual(payload["known_wait_jobs"], 1)
        self.assertEqual(payload["max_queue_wait_seconds"], 120)

    def test_cli_requires_github_token(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            result = CliRunner().invoke(
                CLI_MAIN,
                ["work-graph", "runner-queue-wait", "--repository", "cbusillo/repo"],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Missing GitHub token", result.output)


def _job(
    job_id: int,
    name: str,
    *,
    status: str = "completed",
    created_at: str = "",
    started_at: str = "",
) -> dict[str, object]:
    return {
        "id": job_id,
        "name": name,
        "status": status,
        "conclusion": "success",
        "labels": ["self-hosted", "launchplane"],
        "runner_name": "launchplane-runner-1",
        "runner_group_name": "Default",
        "html_url": f"https://github.com/cbusillo/launchplane/actions/runs/1001/job/{job_id}",
        "created_at": created_at,
        "started_at": started_at,
        "completed_at": "2026-05-18T14:05:00Z",
    }


if __name__ == "__main__":
    unittest.main()
