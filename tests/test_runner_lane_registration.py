from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Sequence
from collections.abc import Mapping
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from typing import Any
from unittest.mock import patch

from click import Command
from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.runner_lane_inventory import RunnerLaneInventory
from control_plane.contracts.runner_lane_inventory import RunnerLaneRecord
from control_plane.contracts.runner_lane_inventory import build_runner_lane_inventory
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationPolicy
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationRequest
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationTokenRecord
from control_plane.contracts.runner_lane_registration import plan_runner_lane_registration
from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.runner_lane_github import GitHubRunnerLaneRegistrationTokenFetcher
from control_plane.workflows.runner_host_hygiene_executor import RemoteCommandResult
from control_plane.workflows.runner_lane_registration_executor import (
    RunnerLaneRegistrationExecutorRequest,
)
from control_plane.workflows.runner_lane_registration_executor import (
    execute_runner_lane_registration_executor,
)


CLI_MAIN = cast(Command, main)


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 6, 8, 17, 30, tzinfo=timezone.utc)


class _TokenFetcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch_registration_token(
        self, *, repository: str
    ) -> tuple[str, RunnerLaneRegistrationTokenRecord]:
        transport = RecordingMergeTrainGitHubTransport(
            responses=({"token": "secret-token", "expires_at": "2026-06-08T18:30:00Z"},)
        )
        self.calls.append(repository)
        return GitHubRunnerLaneRegistrationTokenFetcher(
            transport=transport,
            clock=_Clock(),
        ).fetch_registration_token(repository=repository)


class _CommandRunner:
    def __init__(self, *, returncode: int = 0) -> None:
        self.returncode = returncode
        self.commands: list[tuple[str, ...]] = []
        self.envs: list[dict[str, str]] = []

    def __call__(
        self, command: Sequence[str], _timeout_seconds: int, env: Mapping[str, str]
    ) -> RemoteCommandResult:
        self.commands.append(tuple(command))
        self.envs.append(dict(env))
        return RemoteCommandResult(returncode=self.returncode, stderr="registration failed")


class _AuditPoster:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def __call__(self, audit: Any, idempotency_key: str) -> dict[str, object]:
        self.records.append((audit.status, idempotency_key))
        return {"status": "accepted", "audit_record_key": audit.audit_record_key}


class RunnerLaneRegistrationPlanTests(unittest.TestCase):
    def test_plan_blocks_without_mutate_and_without_allowed_repo(self) -> None:
        plan = plan_runner_lane_registration(
            policy=RunnerLaneRegistrationPolicy(
                approved_hosts=("chris-testing",),
                allowed_registration_roots=("/opt/actions-runners",),
            ),
            request=_request(mutate=False),
            inventory=_inventory(lanes=()),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(
            [blocker.code for blocker in plan.blockers],
            ["mutate_not_requested", "repository_not_allowed"],
        )

    def test_plan_blocks_path_traversal_outside_allowed_root(self) -> None:
        plan = plan_runner_lane_registration(
            policy=_policy(),
            request=_request(registration_root="/opt/actions-runners/../tmp", mutate=True),
            inventory=_inventory(lanes=()),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(
            [blocker.code for blocker in plan.blockers],
            ["registration_root_not_allowed"],
        )

    def test_plan_is_ready_for_empty_inventory_and_required_labels(self) -> None:
        plan = plan_runner_lane_registration(
            policy=_policy(),
            request=_request(mutate=True),
            inventory=_inventory(lanes=()),
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.blockers, ())
        self.assertIn("request a short-lived", plan.next_steps[0])

    def test_plan_blocks_when_lane_already_exists(self) -> None:
        plan = plan_runner_lane_registration(
            policy=_policy(),
            request=_request(mutate=True),
            inventory=_inventory(lanes=(_lane(),)),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual([blocker.code for blocker in plan.blockers], ["lane_already_exists"])


class GitHubRunnerLaneRegistrationTokenFetcherTests(unittest.TestCase):
    def test_fetch_registration_token_returns_token_and_safe_record(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=({"token": "secret-token", "expires_at": "2026-06-08T18:30:00Z"},)
        )

        token, record = GitHubRunnerLaneRegistrationTokenFetcher(
            transport=transport,
            clock=_Clock(),
        ).fetch_registration_token(repository="cbusillo/odoo-tenant-cm-website")

        self.assertEqual(token, "secret-token")
        self.assertEqual(record.repository, "cbusillo/odoo-tenant-cm-website")
        self.assertEqual(record.fetched_at, "2026-06-08T17:30:00Z")
        self.assertNotIn("secret-token", json.dumps(record.model_dump(mode="json")))
        self.assertEqual(
            transport.requests[0].path,
            "/repos/cbusillo/odoo-tenant-cm-website/actions/runners/registration-token",
        )


class RunnerLaneRegistrationExecutorTests(unittest.TestCase):
    def test_blocked_plan_posts_planned_audit_without_token_or_command(self) -> None:
        token_fetcher = _TokenFetcher()
        command_runner = _CommandRunner()
        audit_poster = _AuditPoster()

        result = execute_runner_lane_registration_executor(
            request=_executor_request(mutate=False),
            policy=_policy(),
            pre_inventory=_inventory(lanes=()),
            inventory_reader=lambda _repo: _inventory(lanes=(_lane(),)),
            token_fetcher=token_fetcher,  # type: ignore[arg-type]
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(token_fetcher.calls, [])
        self.assertEqual(command_runner.commands, [])
        self.assertEqual(audit_poster.records[0][0], "planned")

    def test_ready_executor_registers_and_verifies_inventory(self) -> None:
        token_fetcher = _TokenFetcher()
        command_runner = _CommandRunner()
        audit_poster = _AuditPoster()

        with patch(
            "control_plane.workflows.runner_lane_registration_executor.validate_local_executor_environment"
        ):
            result = execute_runner_lane_registration_executor(
                request=_executor_request(mutate=True),
                policy=_policy(),
                pre_inventory=_inventory(lanes=()),
                inventory_reader=lambda _repo: _inventory(lanes=(_lane(),)),
                token_fetcher=token_fetcher,  # type: ignore[arg-type]
                remote_runner=command_runner,
                audit_poster=audit_poster,
            )

        self.assertEqual(result.status, "completed")
        self.assertEqual(token_fetcher.calls, ["cbusillo/odoo-tenant-cm-website"])
        self.assertEqual(len(command_runner.commands), 1)
        command_text = " ".join(command_runner.commands[0])
        self.assertIn("actions-runner-linux-${runner_arch}-${runner_version}.tar.gz", command_text)
        self.assertIn("./config.sh", command_text)
        self.assertIn("nohup ./run.sh", command_text)
        self.assertIn("--labels launchplane,launchplane-managed,self-hosted", command_text)
        self.assertIn("RUNNER_REGISTRATION_TOKEN", command_runner.envs[0])
        self.assertEqual(command_runner.envs[0]["RUNNER_REGISTRATION_TOKEN"], "secret-token")
        self.assertNotIn("secret-token", command_text)
        self.assertNotIn("secret-token", result.model_dump_json())
        self.assertEqual([record[0] for record in audit_poster.records], ["planned", "completed"])

    def test_executor_fails_when_post_inventory_does_not_show_lane(self) -> None:
        with patch(
            "control_plane.workflows.runner_lane_registration_executor.validate_local_executor_environment"
        ):
            result = execute_runner_lane_registration_executor(
                request=_executor_request(mutate=True),
                policy=_policy(),
                pre_inventory=_inventory(lanes=()),
                inventory_reader=lambda _repo: _inventory(lanes=()),
                token_fetcher=_TokenFetcher(),  # type: ignore[arg-type]
                remote_runner=_CommandRunner(),
                audit_poster=_AuditPoster(),
            )

        self.assertEqual(result.status, "failed")
        self.assertIn("inventory", result.message)


class RunnerLaneRegistrationCliTests(unittest.TestCase):
    def test_cli_dry_run_emits_blocked_result_without_github_token(self) -> None:
        with TemporaryDirectory() as temp_dir:
            inventory_file = Path(temp_dir) / "inventory.json"
            inventory_file.write_text(
                json.dumps(_inventory(lanes=()).model_dump(mode="json")),
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "runner-lane-registration-executor",
                    "--repository",
                    "cbusillo/odoo-tenant-cm-website",
                    "--host-name",
                    "chris-testing",
                    "--execution-lane",
                    "chris-testing-ops-gate",
                    "--service-user",
                    "launchplane-runner-hygiene",
                    "--lane-name",
                    "cm-website-runner-1",
                    "--registration-root",
                    "/opt/actions-runners",
                    "--label",
                    "self-hosted",
                    "--label",
                    "launchplane",
                    "--label",
                    "launchplane-managed",
                    "--audit-record-key",
                    "runner-lane-registration/2026-06-08/cm-website/dry-run",
                    "--allowed-repository",
                    "cbusillo/odoo-tenant-cm-website",
                    "--approved-host",
                    "chris-testing",
                    "--allowed-registration-root",
                    "/opt/actions-runners",
                    "--inventory-file",
                    str(inventory_file),
                ],
                env={},
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("planned_response", payload)


def _policy() -> RunnerLaneRegistrationPolicy:
    return RunnerLaneRegistrationPolicy(
        allowed_repositories=("cbusillo/odoo-tenant-cm-website",),
        approved_hosts=("chris-testing",),
        allowed_registration_roots=(
            "/home/launchplane-runner-hygiene/actions-runners",
            "/opt/actions-runners",
        ),
    )


def _request(
    *,
    registration_root: str = "/opt/actions-runners",
    mutate: bool,
) -> RunnerLaneRegistrationRequest:
    return RunnerLaneRegistrationRequest(
        repository="cbusillo/odoo-tenant-cm-website",
        host_name="chris-testing",
        lane_name="cm-website-runner-1",
        registration_root=registration_root,
        labels=("self-hosted", "launchplane", "launchplane-managed"),
        mutate=mutate,
        audit_record_key="runner-lane-registration/2026-06-08/cm-website/test",
    )


def _executor_request(*, mutate: bool) -> RunnerLaneRegistrationExecutorRequest:
    return RunnerLaneRegistrationExecutorRequest(
        repository="cbusillo/odoo-tenant-cm-website",
        host_name="chris-testing",
        execution_lane="chris-testing-ops-gate",
        service_user="launchplane-runner-hygiene",
        lane_name="cm-website-runner-1",
        registration_root="/home/launchplane-runner-hygiene/actions-runners",
        labels=("self-hosted", "launchplane", "launchplane-managed"),
        mutate=mutate,
        audit_record_key="runner-lane-registration/2026-06-08/cm-website/test",
    )


def _inventory(*, lanes: tuple[RunnerLaneRecord, ...]) -> RunnerLaneInventory:
    return build_runner_lane_inventory(
        repository="cbusillo/odoo-tenant-cm-website",
        observed_at="2026-06-08T17:30:00Z",
        lanes=lanes,
    )


def _lane() -> RunnerLaneRecord:
    return RunnerLaneRecord(
        github_id=1,
        name="cm-website-runner-1",
        repository="cbusillo/odoo-tenant-cm-website",
        status="online",
        busy=False,
        labels=("self-hosted", "launchplane", "launchplane-managed"),
        observed_at="2026-06-08T17:31:00Z",
    )


if __name__ == "__main__":
    unittest.main()
