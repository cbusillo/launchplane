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
import getpass

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
from control_plane.workflows.runner_lane_registration_executor import (
    validate_local_executor_environment,
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
    def __init__(
        self,
        *,
        returncode: int = 0,
        returncodes: Sequence[int] | None = None,
    ) -> None:
        self.returncode = returncode
        self.returncodes = list(returncodes or ())
        self.commands: list[tuple[str, ...]] = []
        self.envs: list[dict[str, str]] = []

    def __call__(
        self, command: Sequence[str], _timeout_seconds: int, env: Mapping[str, str]
    ) -> RemoteCommandResult:
        self.commands.append(tuple(command))
        self.envs.append(dict(env))
        returncode = self.returncodes.pop(0) if self.returncodes else self.returncode
        return RemoteCommandResult(returncode=returncode, stderr="registration failed")


class _InventoryReader:
    def __init__(self, inventories: Sequence[RunnerLaneInventory]) -> None:
        self.inventories = list(inventories)
        self.calls: list[str] = []

    def __call__(self, repository: str) -> RunnerLaneInventory:
        self.calls.append(repository)
        if len(self.inventories) > 1:
            return self.inventories.pop(0)
        return self.inventories[0]


class _AuditPoster:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def __call__(self, audit: Any, idempotency_key: str) -> dict[str, object]:
        self.records.append((audit.status, idempotency_key))
        return {"status": "accepted", "audit_record_key": audit.audit_record_key}


class RunnerLaneRegistrationPlanTests(unittest.TestCase):
    def test_request_rejects_unsafe_lane_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "lane_name must use"):
            _request(lane_name="cm-website;rm -rf /", mutate=True)

    def test_request_rejects_unsafe_label(self) -> None:
        with self.assertRaisesRegex(ValueError, "labels must use"):
            RunnerLaneRegistrationRequest(
                repository="cbusillo/odoo-tenant-cm-website",
                host_name="chris-testing",
                lane_name="cm-website-runner-1",
                registration_root="/opt/actions-runners",
                labels=("self-hosted", "launchplane $(touch bad)"),
                mutate=True,
                audit_record_key="runner-lane-registration/2026-06-08/cm-website/test",
            )

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
            inventory=_inventory(lanes=(_lane(name="CM-Website-Runner-1"),)),
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
    def test_executor_request_rejects_unsafe_execution_lane(self) -> None:
        with self.assertRaisesRegex(ValueError, "execution_lane must use"):
            _executor_request(mutate=True, execution_lane="ops-gate;touch bad")

    def test_executor_request_rejects_runner_package_path_traversal(self) -> None:
        with self.assertRaisesRegex(ValueError, "actions/runner tarball URL"):
            _executor_request(
                mutate=True,
                runner_package_url=(
                    "https://github.com/actions/runner/releases/download/"
                    "../download/actions-runner-linux-x64-2.327.1.tar.gz"
                ),
            )

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

    def test_ready_executor_completes_supervised_registration(self) -> None:
        token_fetcher = _TokenFetcher()
        command_runner = _CommandRunner()
        audit_poster = _AuditPoster()

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
        self.assertEqual(len(command_runner.commands), 4)
        self.assertEqual(command_runner.commands[0][:3], ("sh", "-eu", "-c"))
        self.assertIn("config.sh", command_runner.commands[1][3])
        self.assertEqual(
            command_runner.commands[2],
            (
                "sudo",
                "-n",
                "systemctl",
                "enable",
                "--now",
                "launchplane-runner@cm-website-runner-1.service",
            ),
        )
        self.assertEqual(
            command_runner.commands[3],
            (
                "sudo",
                "-n",
                "systemctl",
                "is-active",
                "--quiet",
                "launchplane-runner@cm-website-runner-1.service",
            ),
        )
        self.assertEqual(
            command_runner.envs[1]["RUNNER_DIRECTORY"],
            "/home/launchplane-runner-hygiene/actions-runners/cm-website-runner-1",
        )
        self.assertEqual(command_runner.envs[1]["RUNNER_REGISTRATION_TOKEN"], "secret-token")
        self.assertNotIn("secret-token", " ".join(command_runner.commands[1]))
        self.assertIsNotNone(result.token_record)
        if result.token_record is not None:
            self.assertEqual(result.token_record.repository, "cbusillo/odoo-tenant-cm-website")
        self.assertNotIn("secret-token", result.model_dump_json())
        self.assertEqual([record[0] for record in audit_poster.records], ["planned", "completed"])

    def test_ready_executor_posts_failed_audit_when_config_fails(self) -> None:
        token_fetcher = _TokenFetcher()
        command_runner = _CommandRunner(returncodes=[0, 1])
        audit_poster = _AuditPoster()

        result = execute_runner_lane_registration_executor(
            request=_executor_request(mutate=True),
            policy=_policy(),
            pre_inventory=_inventory(lanes=()),
            inventory_reader=lambda _repo: _inventory(lanes=(_lane(),)),
            token_fetcher=token_fetcher,  # type: ignore[arg-type]
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("configure runner", result.message)
        self.assertEqual([record[0] for record in audit_poster.records], ["planned", "failed"])
        self.assertNotIn("secret-token", result.model_dump_json())

    def test_ready_executor_reports_command_stderr_in_failed_audit(self) -> None:
        token_fetcher = _TokenFetcher()
        command_runner = _CommandRunner(returncodes=[1])
        audit_poster = _AuditPoster()

        result = execute_runner_lane_registration_executor(
            request=_executor_request(mutate=True),
            policy=_policy(),
            pre_inventory=_inventory(lanes=()),
            inventory_reader=lambda _repo: _inventory(lanes=(_lane(),)),
            token_fetcher=token_fetcher,  # type: ignore[arg-type]
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("prepare runner directory", result.message)
        self.assertIn("registration failed", result.message)

    def test_ready_executor_posts_failed_audit_when_post_inventory_is_missing_lane(self) -> None:
        token_fetcher = _TokenFetcher()
        command_runner = _CommandRunner()
        audit_poster = _AuditPoster()

        with patch("control_plane.workflows.runner_lane_registration_executor.sleep"):
            result = execute_runner_lane_registration_executor(
                request=_executor_request(mutate=True),
                policy=_policy(),
                pre_inventory=_inventory(lanes=()),
                inventory_reader=lambda _repo: _inventory(lanes=()),
                token_fetcher=token_fetcher,  # type: ignore[arg-type]
                remote_runner=command_runner,
                audit_poster=audit_poster,
            )

        self.assertEqual(result.status, "failed")
        self.assertIn("exactly one matching lane", result.message)
        self.assertEqual([record[0] for record in audit_poster.records], ["planned", "failed"])

    def test_ready_executor_polls_until_post_inventory_reports_online_lane(self) -> None:
        token_fetcher = _TokenFetcher()
        command_runner = _CommandRunner()
        audit_poster = _AuditPoster()
        inventory_reader = _InventoryReader(
            (
                _inventory(lanes=()),
                _inventory(lanes=(_lane(),)),
            )
        )

        with patch("control_plane.workflows.runner_lane_registration_executor.sleep"):
            result = execute_runner_lane_registration_executor(
                request=_executor_request(mutate=True),
                policy=_policy(),
                pre_inventory=_inventory(lanes=()),
                inventory_reader=inventory_reader,
                token_fetcher=token_fetcher,  # type: ignore[arg-type]
                remote_runner=command_runner,
                audit_poster=audit_poster,
            )

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(inventory_reader.calls), 2)

    def test_local_environment_fails_closed_without_actions_repository(self) -> None:
        with self.assertRaisesRegex(ValueError, "must run from cbusillo/launchplane"):
            validate_local_executor_environment(
                request=_executor_request(mutate=True),
                env={},
                current_user="launchplane-runner-hygiene",
            )

    def test_local_environment_fails_closed_without_execution_lane_label(self) -> None:
        with self.assertRaisesRegex(ValueError, "approved execution lane"):
            validate_local_executor_environment(
                request=_executor_request(mutate=True),
                env={"GITHUB_REPOSITORY": "cbusillo/launchplane"},
                current_user="launchplane-runner-hygiene",
            )

    def test_local_environment_accepts_expected_actions_scope(self) -> None:
        validate_local_executor_environment(
            request=_executor_request(mutate=True),
            env={
                "GITHUB_REPOSITORY": "cbusillo/launchplane",
                "RUNNER_LABELS": "self-hosted,launchplane,chris-testing-ops-gate",
            },
            current_user="launchplane-runner-hygiene",
        )


class RunnerLaneRegistrationCliTests(unittest.TestCase):
    def test_cli_dry_run_requires_github_token(self) -> None:
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

        self.assertEqual(result.exit_code, 1)
        self.assertIn("Missing GitHub token", result.output)

    def test_cli_dry_run_requires_matching_actions_execution_context(self) -> None:
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
                    getpass.getuser(),
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
                    "runner-lane-registration/2026-06-08/cm-website/mutate",
                    "--allowed-repository",
                    "cbusillo/odoo-tenant-cm-website",
                    "--approved-host",
                    "chris-testing",
                    "--allowed-registration-root",
                    "/opt/actions-runners",
                    "--inventory-file",
                    str(inventory_file),
                ],
                env={"GITHUB_TOKEN": "github-token"},
            )

        self.assertEqual(result.exit_code, 1)
        self.assertTrue(
            "must run from cbusillo/launchplane" in result.output
            or "approved execution lane" in result.output,
            result.output,
        )


class RunnerLaneRegistrationWorkflowTests(unittest.TestCase):
    def test_workflow_uses_env_confirmation_and_preserves_artifacts(self) -> None:
        workflow_text = Path(".github/workflows/runner-lane-registration.yml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn('${{ inputs.confirmation }}" !=', workflow_text)
        self.assertIn("CONFIRMATION: ${{ inputs.confirmation }}", workflow_text)
        self.assertIn("if: always()", workflow_text)
        self.assertIn("if-no-files-found: warn", workflow_text)
        self.assertIn("runner lane registration executor did not produce a result", workflow_text)
        self.assertIn("RUNNER_PACKAGE_URL: ${{ inputs.runner_package_url }}", workflow_text)
        self.assertIn('--runner-package-url "$RUNNER_PACKAGE_URL"', workflow_text)
        self.assertIn('[ "$MUTATE_REQUESTED" = "true" ]', workflow_text)
        self.assertIn("IFS=',' read -r -a raw_labels", workflow_text)
        self.assertNotIn("python - <<'PY'", workflow_text)
        self.assertIn(
            "    runs-on:\n"
            "      - self-hosted\n"
            "      - ${{ vars.LAUNCHPLANE_RUNNER_HOST_HYGIENE_EXECUTION_LANE }}\n",
            workflow_text,
        )
        self.assertNotIn("${{ vars.LAUNCHPLANE_RUNNER_LABEL }}", workflow_text)

    def test_workflow_does_not_allowlist_user_registration_root(self) -> None:
        workflow_text = Path(".github/workflows/runner-lane-registration.yml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn('--allowed-registration-root "$REGISTRATION_ROOT"', workflow_text)
        self.assertIn('--allowed-registration-root "$approved_root"', workflow_text)


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
    lane_name: str = "cm-website-runner-1",
) -> RunnerLaneRegistrationRequest:
    return RunnerLaneRegistrationRequest(
        repository="cbusillo/odoo-tenant-cm-website",
        host_name="chris-testing",
        lane_name=lane_name,
        registration_root=registration_root,
        labels=("self-hosted", "launchplane", "launchplane-managed"),
        mutate=mutate,
        audit_record_key="runner-lane-registration/2026-06-08/cm-website/test",
    )


def _executor_request(
    *,
    mutate: bool,
    execution_lane: str = "chris-testing-ops-gate",
    runner_package_url: str = (
        "https://github.com/actions/runner/releases/download/"
        "v2.327.1/actions-runner-linux-x64-2.327.1.tar.gz"
    ),
    timeout_seconds: int = 120,
) -> RunnerLaneRegistrationExecutorRequest:
    return RunnerLaneRegistrationExecutorRequest(
        repository="cbusillo/odoo-tenant-cm-website",
        host_name="chris-testing",
        execution_lane=execution_lane,
        service_user="launchplane-runner-hygiene",
        lane_name="cm-website-runner-1",
        registration_root="/home/launchplane-runner-hygiene/actions-runners",
        runner_package_url=runner_package_url,
        labels=("self-hosted", "launchplane", "launchplane-managed"),
        mutate=mutate,
        audit_record_key="runner-lane-registration/2026-06-08/cm-website/test",
        timeout_seconds=timeout_seconds,
    )


def _inventory(*, lanes: tuple[RunnerLaneRecord, ...]) -> RunnerLaneInventory:
    return build_runner_lane_inventory(
        repository="cbusillo/odoo-tenant-cm-website",
        observed_at="2026-06-08T17:30:00Z",
        lanes=lanes,
    )


def _lane(*, name: str = "cm-website-runner-1") -> RunnerLaneRecord:
    return RunnerLaneRecord(
        github_id=1,
        name=name,
        repository="cbusillo/odoo-tenant-cm-website",
        status="online",
        busy=False,
        labels=("self-hosted", "launchplane", "launchplane-managed"),
        observed_at="2026-06-08T17:31:00Z",
    )


if __name__ == "__main__":
    unittest.main()
