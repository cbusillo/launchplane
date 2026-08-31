from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest

from click import Command
from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.runner_lane_inventory import RunnerLaneInventory
from control_plane.contracts.runner_lane_inventory import RunnerLaneRecord
from control_plane.contracts.runner_lane_inventory import build_runner_lane_inventory
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationPolicy
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationRequest
from control_plane.contracts.runner_lane_registration import plan_runner_lane_retirement
from control_plane.merge_train_github import MergeTrainGitHubError
from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.runner_lane_github import GitHubRepositoryActiveRunReader
from control_plane.runner_lane_github import GitHubRunnerLaneRetirer
from control_plane.workflows.runner_host_hygiene_executor import RemoteCommandResult
from control_plane.workflows.runner_lane_retirement_executor import (
    RunnerLaneRetirementExecutorRequest,
)
from control_plane.workflows.runner_lane_retirement_executor import (
    execute_runner_lane_retirement_executor,
)
from tests.support.workflows import load_workflow


CLI_MAIN = cast(Command, main)


class _CommandRunner:
    def __init__(
        self,
        *,
        events: list[str],
        responses: Sequence[RemoteCommandResult],
    ) -> None:
        self.events = events
        self.responses = list(responses)
        self.commands: list[tuple[str, ...]] = []
        self.envs: list[dict[str, str]] = []

    def __call__(
        self,
        command: Sequence[str],
        _timeout_seconds: int,
        env: Mapping[str, str],
    ) -> RemoteCommandResult:
        self.commands.append(tuple(command))
        self.envs.append(dict(env))
        self.events.append(f"command:{command[0]}")
        if not self.responses:
            raise AssertionError(f"unexpected command: {command}")
        return self.responses.pop(0)


class _AuditPoster:
    def __init__(self, *, events: list[str], fail_terminal: bool = False) -> None:
        self.events = events
        self.fail_terminal = fail_terminal
        self.records: list[Any] = []

    def __call__(self, audit: Any, idempotency_key: str) -> dict[str, object]:
        self.records.append(audit)
        self.events.append(f"audit:{audit.status}")
        if self.fail_terminal and audit.status != "planned":
            raise OSError("service unavailable")
        return {
            "status": "accepted",
            "audit_record_key": audit.audit_record_key,
            "idempotency_key": idempotency_key,
        }


class _ActiveRunReader:
    def __init__(self, *, events: list[str], responses: Sequence[tuple[int, ...]]) -> None:
        self.events = events
        self.responses = list(responses)

    def __call__(self, repository: str) -> tuple[int, ...]:
        self.events.append(f"active:{repository}")
        if not self.responses:
            raise AssertionError("unexpected active-run read")
        return self.responses.pop(0)


class _InventoryReader:
    def __init__(
        self,
        *,
        events: list[str],
        responses: Sequence[RunnerLaneInventory],
    ) -> None:
        self.events = events
        self.responses = list(responses)

    def __call__(self, repository: str) -> RunnerLaneInventory:
        self.events.append(f"inventory:{repository}")
        if not self.responses:
            raise AssertionError("unexpected inventory read")
        return self.responses.pop(0)


class _RunnerRetirer:
    def __init__(self, *, events: list[str]) -> None:
        self.events = events
        self.calls: list[tuple[str, int]] = []

    def __call__(self, repository: str, runner_id: int) -> None:
        self.calls.append((repository, runner_id))
        self.events.append(f"delete:{repository}:{runner_id}")


class RunnerLaneRetirementPlanTests(unittest.TestCase):
    def test_plan_is_ready_for_exact_idle_managed_lane(self) -> None:
        plan = plan_runner_lane_retirement(
            policy=_policy(),
            request=_retirement_request(mutate=True),
            inventory=_inventory(lanes=(_lane(labels=("Launchplane", "Launchplane-Managed")),)),
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.operation, "retire")
        self.assertEqual(plan.blockers, ())

    def test_plan_blocks_busy_unmanaged_or_active_target(self) -> None:
        plan = plan_runner_lane_retirement(
            policy=_policy(),
            request=_retirement_request(
                mutate=True,
                active_run_ids=(902, 901),
                target_worker_active=True,
            ),
            inventory=_inventory(lanes=(_lane(busy=True, labels=("launchplane",)),)),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(
            {blocker.code for blocker in plan.blockers},
            {"label_missing", "lane_busy", "repository_runs_active", "target_worker_active"},
        )
        self.assertEqual(plan.active_run_ids, (901, 902))

    def test_plan_blocks_missing_or_ambiguous_lane(self) -> None:
        missing = plan_runner_lane_retirement(
            policy=_policy(),
            request=_retirement_request(mutate=True),
            inventory=_inventory(lanes=()),
        )
        ambiguous = plan_runner_lane_retirement(
            policy=_policy(),
            request=_retirement_request(mutate=True),
            inventory=_inventory(lanes=(_lane(github_id=21), _lane(github_id=22))),
        )

        self.assertEqual([blocker.code for blocker in missing.blockers], ["lane_missing"])
        self.assertEqual([blocker.code for blocker in ambiguous.blockers], ["lane_ambiguous"])

    def test_plan_blocks_canonical_path_outside_allowed_root(self) -> None:
        plan = plan_runner_lane_retirement(
            policy=_policy(),
            request=_retirement_request(
                mutate=True,
                registration_root="/srv/actions-runners",
            ),
            inventory=_inventory(lanes=(_lane(),)),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(
            [blocker.code for blocker in plan.blockers],
            ["registration_root_not_allowed"],
        )


class GitHubRunnerLaneRetirementTests(unittest.TestCase):
    def test_retirer_deletes_exact_runner_and_treats_not_found_as_complete(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(MergeTrainGitHubError("not found", status_code=404),)
        )

        GitHubRunnerLaneRetirer(transport=transport).delete_runner(
            repository="cbusillo/code",
            runner_id=23,
        )

        self.assertEqual(transport.requests[0].method, "DELETE")
        self.assertEqual(
            transport.requests[0].path,
            "/repos/cbusillo/code/actions/runners/23",
        )

    def test_active_run_reader_collects_all_nonterminal_statuses(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=tuple({"workflow_runs": [{"id": run_id}]} for run_id in (9, 4, 7, 4, 2))
        )

        run_ids = GitHubRepositoryActiveRunReader(transport=transport).read_active_run_ids(
            repository="cbusillo/code"
        )

        self.assertEqual(run_ids, (2, 4, 7, 9))
        self.assertEqual(len(transport.requests), 5)
        self.assertIn("status=queued", transport.requests[0].path)
        self.assertIn("status=requested", transport.requests[-1].path)


class RunnerLaneRetirementExecutorTests(unittest.TestCase):
    def test_executor_request_rejects_parent_directory_components(self) -> None:
        with self.assertRaisesRegex(ValueError, "parent-directory components"):
            _executor_request(
                mutate=True,
                registration_root="/home/launchplane-runner-hygiene/actions-runners/../tmp",
            )

    def test_executor_request_rejects_unscoped_roots(self) -> None:
        for root in ("/", "/.", "home/launchplane-runner-hygiene/actions-runners"):
            with self.subTest(root=root):
                with self.assertRaisesRegex(ValueError, "requires scoped absolute root"):
                    _executor_request(mutate=True, registration_root=root)

    def test_executor_request_preserves_benign_root_normalization(self) -> None:
        request = _executor_request(
            mutate=True,
            registration_root="//home//launchplane-runner-hygiene/./actions-runners/",
        )

        self.assertEqual(
            request.registration_root,
            "/home/launchplane-runner-hygiene/actions-runners",
        )

    def test_executor_rechecks_live_state_before_delete_and_removes_root(self) -> None:
        events: list[str] = []
        command_runner = _CommandRunner(
            events=events,
            responses=(
                RemoteCommandResult(returncode=1),
                RemoteCommandResult(returncode=0),
                RemoteCommandResult(returncode=0),
                RemoteCommandResult(returncode=1),
                RemoteCommandResult(returncode=0),
            ),
        )
        audit_poster = _AuditPoster(events=events)
        runner_retirer = _RunnerRetirer(events=events)

        result = execute_runner_lane_retirement_executor(
            request=_executor_request(mutate=True),
            policy=_policy(),
            pre_inventory=_inventory(lanes=(_lane(),)),
            inventory_reader=_InventoryReader(
                events=events,
                responses=(_inventory(lanes=(_lane(),)), _inventory(lanes=())),
            ),
            active_run_reader=_ActiveRunReader(events=events, responses=((), ())),
            runner_retirer=runner_retirer,
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(runner_retirer.calls, [("cbusillo/code", 23)])
        self.assertEqual(
            [record.status for record in audit_poster.records], ["planned", "completed"]
        )
        helper_command = command_runner.commands[2]
        self.assertEqual(
            helper_command,
            (
                "sudo",
                "-n",
                "/usr/local/sbin/launchplane-runner-service-retire",
                "cbusillo/code",
                "code-ci-1",
                "/home/launchplane-runner-hygiene/actions-runners",
                "launchplane-runner-hygiene",
            ),
        )
        delete_index = events.index("delete:cbusillo/code:23")
        self.assertLess(events.index("active:cbusillo/code", 2), delete_index)
        self.assertLess(events.index("inventory:cbusillo/code"), delete_index)
        self.assertLess(delete_index, len(events) - 1)
        self.assertEqual(events[-1], "audit:completed")

    def test_executor_rolls_service_back_when_run_appears_after_stop(self) -> None:
        events: list[str] = []
        command_runner = _CommandRunner(
            events=events,
            responses=(
                RemoteCommandResult(returncode=1),
                RemoteCommandResult(returncode=0),
                RemoteCommandResult(returncode=0),
                RemoteCommandResult(returncode=1),
                RemoteCommandResult(returncode=0),
            ),
        )
        audit_poster = _AuditPoster(events=events)
        runner_retirer = _RunnerRetirer(events=events)

        result = execute_runner_lane_retirement_executor(
            request=_executor_request(mutate=True),
            policy=_policy(),
            pre_inventory=_inventory(lanes=(_lane(),)),
            inventory_reader=_InventoryReader(
                events=events,
                responses=(_inventory(lanes=(_lane(),)),),
            ),
            active_run_reader=_ActiveRunReader(events=events, responses=((), (551,))),
            runner_retirer=runner_retirer,
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("active repository runs after service stop: 551", result.message)
        self.assertEqual(runner_retirer.calls, [])
        self.assertEqual(
            command_runner.commands[-1][:5], ("sudo", "-n", "systemctl", "enable", "--now")
        )
        self.assertEqual([record.status for record in audit_poster.records], ["planned", "failed"])

    def test_executor_blocks_dry_run_without_host_commands(self) -> None:
        events: list[str] = []
        command_runner = _CommandRunner(
            events=events,
            responses=(RemoteCommandResult(returncode=1),),
        )
        audit_poster = _AuditPoster(events=events)

        result = execute_runner_lane_retirement_executor(
            request=_executor_request(mutate=False),
            policy=_policy(),
            pre_inventory=_inventory(lanes=(_lane(),)),
            inventory_reader=_InventoryReader(events=events, responses=()),
            active_run_reader=_ActiveRunReader(events=events, responses=((),)),
            runner_retirer=_RunnerRetirer(events=events),
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(len(command_runner.commands), 1)
        self.assertEqual(command_runner.commands[0][0], "pgrep")
        self.assertEqual([record.status for record in audit_poster.records], ["planned"])

    def test_executor_preserves_terminal_audit_when_service_delivery_fails(self) -> None:
        events: list[str] = []
        command_runner = _CommandRunner(
            events=events,
            responses=(
                RemoteCommandResult(returncode=1),
                RemoteCommandResult(returncode=0),
                RemoteCommandResult(returncode=0),
                RemoteCommandResult(returncode=1),
                RemoteCommandResult(returncode=0),
            ),
        )

        result = execute_runner_lane_retirement_executor(
            request=_executor_request(mutate=True),
            policy=_policy(),
            pre_inventory=_inventory(lanes=(_lane(),)),
            inventory_reader=_InventoryReader(
                events=events,
                responses=(_inventory(lanes=(_lane(),)), _inventory(lanes=())),
            ),
            active_run_reader=_ActiveRunReader(events=events, responses=((), ())),
            runner_retirer=_RunnerRetirer(events=events),
            remote_runner=command_runner,
            audit_poster=_AuditPoster(events=events, fail_terminal=True),
        )

        self.assertEqual(result.status, "audit_delivery_pending")
        self.assertIsNone(result.terminal_response)
        self.assertIsNotNone(result.terminal_audit)
        assert result.terminal_audit is not None
        self.assertEqual(result.terminal_audit.status, "completed")
        self.assertIn("workflow artifact", result.message)

    def test_process_match_does_not_confuse_lane_name_prefix(self) -> None:
        events: list[str] = []
        command_runner = _CommandRunner(
            events=events,
            responses=(
                RemoteCommandResult(
                    returncode=0,
                    stdout=(
                        "/home/launchplane-runner-hygiene/actions-runners/"
                        "code-ci-10/bin/Runner.Worker spawnclient"
                    ),
                ),
            ),
        )
        audit_poster = _AuditPoster(events=events)

        result = execute_runner_lane_retirement_executor(
            request=_executor_request(mutate=False),
            policy=_policy(),
            pre_inventory=_inventory(lanes=(_lane(),)),
            inventory_reader=_InventoryReader(events=events, responses=()),
            active_run_reader=_ActiveRunReader(events=events, responses=((),)),
            runner_retirer=_RunnerRetirer(events=events),
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "blocked")
        self.assertNotIn(
            "target_worker_active", {b.code for b in audit_poster.records[0].plan.blockers}
        )


class RunnerLaneRetirementCliTests(unittest.TestCase):
    def test_cli_rejects_mutation_without_service_audit(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            inventory_file = Path(temporary_directory) / "inventory.json"
            inventory_file.write_text(
                _inventory(lanes=(_lane(),)).model_dump_json(),
                encoding="utf-8",
            )
            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "runner-lane-retirement-executor",
                    "--repository",
                    "cbusillo/code",
                    "--host-name",
                    "chris-testing",
                    "--execution-lane",
                    "chris-testing-ops-gate",
                    "--service-user",
                    "launchplane-runner-hygiene",
                    "--lane-name",
                    "code-ci-1",
                    "--registration-root",
                    "/home/launchplane-runner-hygiene/actions-runners",
                    "--mutate",
                    "--audit-record-key",
                    "runner-lane-retirement/2026-07-26/code-ci-1",
                    "--allowed-repository",
                    "cbusillo/code",
                    "--approved-host",
                    "chris-testing",
                    "--allowed-registration-root",
                    "/home/launchplane-runner-hygiene/actions-runners",
                    "--inventory-file",
                    str(inventory_file),
                ],
                env={"GITHUB_TOKEN": "github-token"},
            )

        self.assertEqual(result.exit_code, 1)
        self.assertIn("requires --audit-mode service", result.output)


class RunnerLaneLifecycleWorkflowTests(unittest.TestCase):
    def test_workflow_preserves_authorized_path_and_serializes_host_mutations(self) -> None:
        workflow = load_workflow(".github/workflows/runner-lane-registration.yml")
        workflow_text = workflow.path.read_text(encoding="utf-8")

        self.assertEqual(workflow.name, "Runner Lane Lifecycle")
        self.assertIn("operation:", workflow_text)
        self.assertIn("- retire", workflow_text)
        self.assertIn("retire ${TARGET_REPOSITORY} ${LANE_NAME}", workflow_text)
        self.assertEqual(workflow.permissions.get("id-token"), "write")
        self.assertIn(
            'approved_root="${RUNNER_REGISTRATION_ALLOWED_ROOT:-$HOME/actions-runners}"',
            workflow_text,
        )
        self.assertIn("/tmp/launchplane-runner-host-hygiene.lock", workflow_text)
        self.assertIn("runner-lane-retirement-executor", workflow_text)
        self.assertIn("if: always()", workflow_text)
        self.assertNotIn("runner-lane-retirement.yml", workflow_text)

    def test_privileged_helper_requires_root_owned_exact_target_binding(self) -> None:
        helper_text = Path("scripts/runner-lane-service-retire.sh").read_text(encoding="utf-8")

        self.assertIn("runner-lane-retirement-targets", helper_text)
        self.assertIn("target_record=", helper_text)
        self.assertIn("grep -Fxq", helper_text)
        self.assertIn('"${SUDO_USER:-}" != "$service_user"', helper_text)
        self.assertIn("--property=ExecStart", helper_text)
        self.assertIn("systemctl disable", helper_text)


def _policy() -> RunnerLaneRegistrationPolicy:
    return RunnerLaneRegistrationPolicy(
        allowed_repositories=("cbusillo/code",),
        approved_hosts=("chris-testing",),
        allowed_registration_roots=("/home/launchplane-runner-hygiene/actions-runners",),
        required_labels=("launchplane", "launchplane-managed"),
    )


def _retirement_request(
    *,
    mutate: bool,
    active_run_ids: tuple[int, ...] = (),
    target_worker_active: bool = False,
    registration_root: str = "/home/launchplane-runner-hygiene/actions-runners",
) -> RunnerLaneRegistrationRequest:
    return RunnerLaneRegistrationRequest(
        operation="retire",
        repository="cbusillo/code",
        host_name="chris-testing",
        lane_name="code-ci-1",
        registration_root=registration_root,
        labels=(),
        active_run_ids=active_run_ids,
        target_worker_active=target_worker_active,
        mutate=mutate,
        audit_record_key="runner-lane-retirement/2026-07-26/code-ci-1",
    )


def _executor_request(
    *,
    mutate: bool,
    registration_root: str = "/home/launchplane-runner-hygiene/actions-runners",
) -> RunnerLaneRetirementExecutorRequest:
    return RunnerLaneRetirementExecutorRequest(
        repository="cbusillo/code",
        host_name="chris-testing",
        execution_lane="chris-testing-ops-gate",
        service_user="launchplane-runner-hygiene",
        lane_name="code-ci-1",
        registration_root=registration_root,
        mutate=mutate,
        audit_record_key="runner-lane-retirement/2026-07-26/code-ci-1",
    )


def _inventory(*, lanes: tuple[RunnerLaneRecord, ...]) -> RunnerLaneInventory:
    return build_runner_lane_inventory(
        repository="cbusillo/code",
        observed_at="2026-07-26T20:00:00Z",
        lanes=lanes,
    )


def _lane(
    *,
    github_id: int = 23,
    busy: bool = False,
    labels: tuple[str, ...] = ("launchplane", "launchplane-managed"),
) -> RunnerLaneRecord:
    return RunnerLaneRecord(
        github_id=github_id,
        name="code-ci-1",
        repository="cbusillo/code",
        status="online",
        busy=busy,
        labels=labels,
        observed_at="2026-07-26T20:00:00Z",
    )


if __name__ == "__main__":
    unittest.main()
