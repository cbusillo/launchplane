from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Sequence
from collections.abc import Mapping
import json
import os
import pwd
import shutil
import subprocess
import sys
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
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationAuditRecord
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationPlan
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
    RunnerLaneRegistrationExecutorResult,
)
from control_plane.workflows.runner_lane_registration_executor import (
    execute_runner_lane_registration_executor,
)
from control_plane.workflows.runner_lane_registration_executor import (
    validate_local_executor_environment,
)


CLI_MAIN = cast(Command, main)
REGISTRATION_TOKEN = "secret-token"


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
            responses=({"token": REGISTRATION_TOKEN, "expires_at": "2026-06-08T18:30:00Z"},)
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

    def test_policy_rejects_parent_directory_components(self) -> None:
        for root in ("/opt/actions-runners/..", "/opt/.."):
            with self.subTest(root=root):
                with self.assertRaisesRegex(ValueError, "parent-directory components"):
                    RunnerLaneRegistrationPolicy(allowed_registration_roots=(root,))

    def test_request_rejects_parent_directory_components(self) -> None:
        with self.assertRaisesRegex(ValueError, "parent-directory components"):
            _request(registration_root="/opt/actions-runners/../tmp", mutate=True)

    def test_contract_roots_require_absolute_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "paths must be absolute"):
            RunnerLaneRegistrationPolicy(allowed_registration_roots=("opt/actions-runners",))
        with self.assertRaisesRegex(ValueError, "paths must be absolute"):
            _request(registration_root="opt/actions-runners", mutate=True)

    def test_contract_roots_reject_root_equivalent_normalization(self) -> None:
        for root in ("/.", "/"):
            with self.subTest(root=root):
                with self.assertRaisesRegex(ValueError, "scoped absolute paths"):
                    RunnerLaneRegistrationPolicy(allowed_registration_roots=(root,))
                with self.assertRaisesRegex(ValueError, "scoped absolute paths"):
                    _request(registration_root=root, mutate=True)

    def test_contract_roots_preserve_benign_normalization(self) -> None:
        policy = RunnerLaneRegistrationPolicy(
            allowed_registration_roots=("/opt//actions-runners/./",)
        )
        request = _request(registration_root="/opt//actions-runners/./", mutate=True)

        self.assertEqual(policy.allowed_registration_roots, ("/opt/actions-runners",))
        self.assertEqual(request.registration_root, "/opt/actions-runners")

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

    def test_plan_blocks_canonical_path_outside_allowed_root(self) -> None:
        plan = plan_runner_lane_registration(
            policy=_policy(),
            request=_request(registration_root="/srv/actions-runners", mutate=True),
            inventory=_inventory(lanes=()),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(
            [blocker.code for blocker in plan.blockers],
            ["registration_root_not_allowed"],
        )

    def test_plan_blocks_sibling_prefix_outside_allowed_root(self) -> None:
        plan = plan_runner_lane_registration(
            policy=_policy(),
            request=_request(registration_root="/opt/actions-runners-evil", mutate=True),
            inventory=_inventory(lanes=()),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(
            [blocker.code for blocker in plan.blockers],
            ["registration_root_not_allowed"],
        )

    def test_plan_rejects_parent_directory_components(self) -> None:
        plan = plan_runner_lane_registration(
            policy=_policy(),
            request=_request(mutate=True),
            inventory=_inventory(lanes=()),
        )
        payload = plan.model_dump(mode="json")
        payload["registration_root"] = "/opt/actions-runners/../../etc"

        with self.assertRaisesRegex(ValueError, "parent-directory components"):
            RunnerLaneRegistrationPlan.model_validate(payload)

    def test_audit_rejects_plan_registration_root_mismatch(self) -> None:
        request = _request(mutate=True)
        plan = plan_runner_lane_registration(
            policy=_policy(),
            request=request,
            inventory=_inventory(lanes=()),
        )
        plan_payload = plan.model_dump(mode="json")
        plan_payload["registration_root"] = "/opt/other-actions-runners"

        with self.assertRaisesRegex(ValueError, "must match plan registration_root"):
            RunnerLaneRegistrationAuditRecord(
                audit_record_key=request.audit_record_key,
                status="planned",
                request=request,
                plan=RunnerLaneRegistrationPlan.model_validate(plan_payload),
                pre_inventory=_inventory(lanes=()),
                message="registration planned",
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
            responses=({"token": REGISTRATION_TOKEN, "expires_at": "2026-06-08T18:30:00Z"},)
        )

        token, record = GitHubRunnerLaneRegistrationTokenFetcher(
            transport=transport,
            clock=_Clock(),
        ).fetch_registration_token(repository="cbusillo/odoo-tenant-cm-website")

        self.assertEqual(token, REGISTRATION_TOKEN)
        self.assertEqual(record.repository, "cbusillo/odoo-tenant-cm-website")
        self.assertEqual(record.fetched_at, "2026-06-08T17:30:00Z")
        self.assertNotIn(REGISTRATION_TOKEN, json.dumps(record.model_dump(mode="json")))
        self.assertEqual(
            transport.requests[0].path,
            "/repos/cbusillo/odoo-tenant-cm-website/actions/runners/registration-token",
        )


class RunnerLaneRegistrationExecutorTests(unittest.TestCase):
    def test_executor_request_rejects_unsafe_execution_lane(self) -> None:
        with self.assertRaisesRegex(ValueError, "execution_lane must use"):
            _executor_request(mutate=True, execution_lane="ops-gate;touch bad")

    def test_executor_request_rejects_parent_directory_components(self) -> None:
        with self.assertRaisesRegex(ValueError, "parent-directory components"):
            _executor_request(
                mutate=True,
                registration_root="/home/launchplane-runner-hygiene/actions-runners/../tmp",
            )

    def test_executor_request_requires_absolute_registration_root(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires absolute registration_root"):
            _executor_request(mutate=True, registration_root="opt/actions-runners")

    def test_executor_request_rejects_root_equivalent_normalization(self) -> None:
        for root in ("/.", "/"):
            with self.subTest(root=root):
                with self.assertRaisesRegex(ValueError, "scoped absolute registration_root"):
                    _executor_request(mutate=True, registration_root=root)

    def test_executor_request_preserves_benign_root_normalization(self) -> None:
        request = _executor_request(
            mutate=True,
            registration_root="/home//launchplane-runner-hygiene/./actions-runners/",
        )

        self.assertEqual(
            request.registration_root,
            "/home/launchplane-runner-hygiene/actions-runners",
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

    def test_blocked_plan_allows_invalid_runner_package_url_without_token_or_command(
        self,
    ) -> None:
        token_fetcher = _TokenFetcher()
        command_runner = _CommandRunner()
        audit_poster = _AuditPoster()

        result = execute_runner_lane_registration_executor(
            request=_executor_request(
                mutate=True,
                runner_package_url=(
                    "https://github.com/actions/runner/releases/download/"
                    "../download/actions-runner-linux-x64-2.327.1.tar.gz"
                ),
            ),
            policy=_policy(),
            pre_inventory=_inventory(lanes=(_lane(),)),
            inventory_reader=lambda _repo: _inventory(lanes=(_lane(),)),
            token_fetcher=token_fetcher,  # type: ignore[arg-type]
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(token_fetcher.calls, [])
        self.assertEqual(command_runner.commands, [])
        self.assertEqual([record[0] for record in audit_poster.records], ["planned"])

    def test_ready_executor_posts_failed_audit_without_runner_package_url(self) -> None:
        token_fetcher = _TokenFetcher()
        command_runner = _CommandRunner()
        audit_poster = _AuditPoster()

        result = execute_runner_lane_registration_executor(
            request=_executor_request(mutate=True, runner_package_url=""),
            policy=_policy(),
            pre_inventory=_inventory(lanes=()),
            inventory_reader=lambda _repo: _inventory(lanes=(_lane(),)),
            token_fetcher=token_fetcher,  # type: ignore[arg-type]
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("requires runner_package_url", result.message)
        self.assertEqual(token_fetcher.calls, [])
        self.assertEqual(command_runner.commands, [])
        self.assertEqual([record[0] for record in audit_poster.records], ["planned", "failed"])

    def test_ready_executor_posts_failed_audit_for_unsafe_runner_package_url(self) -> None:
        token_fetcher = _TokenFetcher()
        command_runner = _CommandRunner()
        audit_poster = _AuditPoster()

        result = execute_runner_lane_registration_executor(
            request=_executor_request(
                mutate=True,
                runner_package_url=(
                    "https://github.com/actions/runner/releases/download/"
                    "../download/actions-runner-linux-x64-2.327.1.tar.gz"
                ),
            ),
            policy=_policy(),
            pre_inventory=_inventory(lanes=()),
            inventory_reader=lambda _repo: _inventory(lanes=(_lane(),)),
            token_fetcher=token_fetcher,  # type: ignore[arg-type]
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("actions/runner tarball URL", result.message)
        self.assertEqual(token_fetcher.calls, [])
        self.assertEqual(command_runner.commands, [])
        self.assertEqual([record[0] for record in audit_poster.records], ["planned", "failed"])

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
        configuration_calls = [
            (command, env)
            for command, env in zip(command_runner.commands, command_runner.envs, strict=True)
            if "RUNNER_REGISTRATION_TOKEN" in env
        ]
        self.assertEqual(len(configuration_calls), 1)
        configuration_command, configuration_env = configuration_calls[0]
        self.assertIn("config.sh", configuration_command[3])
        self.assertIn(
            (
                "sudo",
                "-n",
                "systemctl",
                "enable",
                "--now",
                "launchplane-runner@cm-website-runner-1.service",
            ),
            command_runner.commands,
        )
        self.assertIn(
            (
                "sudo",
                "-n",
                "systemctl",
                "is-active",
                "--quiet",
                "launchplane-runner@cm-website-runner-1.service",
            ),
            command_runner.commands,
        )
        self.assertEqual(
            configuration_env["RUNNER_DIRECTORY"],
            "/home/launchplane-runner-hygiene/actions-runners/cm-website-runner-1",
        )
        self.assertEqual(configuration_env["RUNNER_REGISTRATION_TOKEN"], REGISTRATION_TOKEN)
        self.assertNotIn(REGISTRATION_TOKEN, " ".join(configuration_command))
        self.assertIsNotNone(result.token_record)
        if result.token_record is not None:
            self.assertEqual(result.token_record.repository, "cbusillo/odoo-tenant-cm-website")
        self.assertNotIn(REGISTRATION_TOKEN, result.model_dump_json())
        self.assertEqual([record[0] for record in audit_poster.records], ["planned", "completed"])

    def test_ready_executor_posts_failed_audit_when_config_fails(self) -> None:
        token_fetcher = _TokenFetcher()
        command_runner = _CommandRunner(returncodes=[0, 0, 1])
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
        self.assertNotIn(REGISTRATION_TOKEN, result.model_dump_json())

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
        self.assertIn("verify registration target", result.message)
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

    def test_local_environment_fails_closed_without_runner_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "approved execution lane"):
            validate_local_executor_environment(
                request=_executor_request(mutate=True),
                env={"GITHUB_REPOSITORY": "cbusillo/launchplane"},
                current_user="launchplane-runner-hygiene",
            )

    def test_local_environment_fails_closed_without_execution_lane_label(self) -> None:
        with self.assertRaisesRegex(ValueError, "approved execution lane"):
            validate_local_executor_environment(
                request=_executor_request(mutate=True),
                env={
                    "GITHUB_REPOSITORY": "cbusillo/launchplane",
                    "RUNNER_NAME": "chris-testing-launchplane",
                },
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

    def test_local_environment_accepts_expected_runner_name(self) -> None:
        validate_local_executor_environment(
            request=_executor_request(mutate=True),
            env={
                "GITHUB_REPOSITORY": "cbusillo/launchplane",
                "RUNNER_NAME": "chris-testing-ops-gate",
            },
            current_user="launchplane-runner-hygiene",
        )


_FILESYSTEM_COMMAND = r"""import io
import json
import os
from pathlib import Path
import pwd
import sys
import tarfile

command = Path(sys.argv[0]).name
args = sys.argv[1:]
if command == "realpath":
    try:
        print(Path(args[-1]).resolve(strict=True))
    except (OSError, RuntimeError):
        sys.exit(1)
elif command == "stat":
    owner = Path(args[-1]).stat().st_uid
    print(pwd.getpwuid(owner).pw_name if "%U" in args else owner)
else:
    with Path(os.environ["FIXTURE_LOG"]).open("a") as stream:
        stream.write(json.dumps(command) + "\n")
    if command == "curl":
        archive = args[args.index("--output") + 1]
        config = b'#!/bin/sh\nprintf \'"configure"\\n\' >> "$FIXTURE_LOG"\n'
        if os.environ.get("FAIL_CONFIG"):
            config = b'#!/bin/sh\nprintf %s "$RUNNER_REGISTRATION_TOKEN" >&2\nexit 1\n'
        with tarfile.open(archive, "w:gz") as stream:
            member = tarfile.TarInfo("config.sh")
            member.mode = 0o700
            member.size = len(config)
            stream.addfile(member, io.BytesIO(config))
        if os.environ.get("SWAP_AFTER_DOWNLOAD"):
            root = Path(os.environ["FIXTURE_ROOT"])
            root.rename(root.with_name(root.name + "-retained"))
            root.symlink_to(os.environ["FIXTURE_OUTSIDE"], target_is_directory=True)
    elif command == "tar":
        os.execv(os.environ["FIXTURE_REAL_TAR"], ["tar", *args])
    elif command != "sudo":
        raise AssertionError(command)
"""


class RunnerLaneRegistrationFilesystemTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # macOS temporary paths can include the /var symlink. The fixture's
        # approved root must itself be canonical on both supported platforms.
        self.base = Path(temporary.name).resolve()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.root = self.base / "runners"
        self.outside = self.base / "outside"
        self.outside.mkdir()
        self.log = self.base / "commands.jsonl"
        self.request = _executor_request(
            mutate=True, registration_root=str(self.root), timeout_seconds=10
        ).model_copy(update={"service_user": pwd.getpwuid(os.getuid()).pw_name})
        self.policy = _policy().model_copy(update={"allowed_registration_roots": (str(self.root),)})
        real_tar = shutil.which("tar", path=os.defpath)
        if real_tar is None:
            self.fail("Filesystem behavior tests require tar")
        for name in ("realpath", "stat", "curl", "tar", "sudo"):
            executable = self.bin / name
            executable.write_text(f"#!{sys.executable}\n{_FILESYSTEM_COMMAND}")
            executable.chmod(0o700)
        self.env = {
            "PATH": f"{self.bin}{os.pathsep}{os.defpath}",
            "FIXTURE_LOG": str(self.log),
            "FIXTURE_ROOT": str(self.root),
            "FIXTURE_OUTSIDE": str(self.outside),
            "FIXTURE_REAL_TAR": real_tar,
        }
        self.tokens = _TokenFetcher()
        self.audits = _AuditPoster()
        self.audit_messages: list[str] = []
        self.swap_before_configuration = False
        self.swap_after_configuration = False
        self.swap_during_inventory = False

    def swap_root(self) -> None:
        self.root.rename(self.base / "retained")
        self.root.symlink_to(self.outside, target_is_directory=True)

    def run_executor(self) -> RunnerLaneRegistrationExecutorResult:
        def run_command(
            command: Sequence[str], timeout_seconds: int, env: Mapping[str, str]
        ) -> RemoteCommandResult:
            if self.swap_before_configuration and "RUNNER_REGISTRATION_TOKEN" in env:
                self.swap_root()
            completed = subprocess.run(
                command,
                env={**self.env, **env},
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            if self.swap_after_configuration and "RUNNER_REGISTRATION_TOKEN" in env:
                self.swap_root()
            return RemoteCommandResult(
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )

        def post_audit(
            audit: RunnerLaneRegistrationAuditRecord, idempotency_key: str
        ) -> dict[str, object]:
            self.audit_messages.append(audit.message)
            return self.audits(audit, idempotency_key)

        def read_inventory(_repository: str) -> RunnerLaneInventory:
            if self.swap_during_inventory:
                self.swap_root()
            return _inventory(lanes=(_lane(),))

        return execute_runner_lane_registration_executor(
            request=self.request,
            policy=self.policy,
            pre_inventory=_inventory(lanes=()),
            inventory_reader=read_inventory,
            token_fetcher=self.tokens,  # type: ignore[arg-type]
            remote_runner=run_command,
            audit_poster=post_audit,
        )

    def commands(self) -> list[str]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def assert_static_refusal(self) -> None:
        result = self.run_executor()
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.tokens.calls, [])
        self.assertEqual(self.commands(), [])
        self.assertEqual([status for status, _key in self.audits.records], ["planned", "failed"])
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_creates_absent_canonical_root_and_new_lane(self) -> None:
        result = self.run_executor()
        self.assertEqual(result.status, "completed")
        self.assertTrue((self.root / self.request.lane_name / "config.sh").is_file())
        self.assertEqual(self.tokens.calls, [self.request.repository])
        self.assertIn("configure", self.commands())

    def test_token_acquisition_follows_read_only_validation(self) -> None:
        original_fetch = self.tokens.fetch_registration_token

        def fetch_after_validation(
            *, repository: str
        ) -> tuple[str, RunnerLaneRegistrationTokenRecord]:
            self.assertFalse(self.root.exists())
            self.assertEqual(self.commands(), [])
            return original_fetch(repository=repository)

        with patch.object(
            self.tokens, "fetch_registration_token", side_effect=fetch_after_validation
        ):
            result = self.run_executor()
        self.assertEqual(result.status, "completed")

    def test_token_failure_leaves_absent_target_available_for_retry(self) -> None:
        with patch.object(
            self.tokens,
            "fetch_registration_token",
            side_effect=RuntimeError("fixture token unavailable"),
        ):
            failed = self.run_executor()
        self.assertEqual(failed.status, "failed")
        self.assertFalse(self.root.exists())
        self.assertEqual(self.commands(), [])
        self.request.audit_record_key += "/retry"
        self.assertEqual(self.run_executor().status, "completed")

    def test_configuration_failure_redacts_registration_token_from_evidence(self) -> None:
        self.env["FAIL_CONFIG"] = "1"
        result = self.run_executor()
        self.assertEqual(result.status, "failed")
        self.assertTrue(self.audit_messages)
        exposed = [
            surface
            for surface, evidence in (
                ("result", result.model_dump_json()),
                ("audit", " ".join(self.audit_messages)),
            )
            if REGISTRATION_TOKEN in evidence
        ]
        self.assertEqual(exposed, [])

    def test_rejects_symlinked_root_before_token_or_download(self) -> None:
        self.root.symlink_to(self.outside, target_is_directory=True)
        self.assert_static_refusal()

    def test_rejects_dangling_root_before_token_or_download(self) -> None:
        self.root.symlink_to(self.outside / "missing", target_is_directory=True)
        self.assert_static_refusal()

    def test_rejects_symlinked_ancestor_of_absent_root(self) -> None:
        ancestor = self.base / "linked"
        ancestor.symlink_to(self.outside, target_is_directory=True)
        self.request.registration_root = str(ancestor / "missing" / "runners")
        self.policy.allowed_registration_roots = (str(ancestor),)
        self.assert_static_refusal()

    def test_rejects_dangling_ancestor_of_absent_root(self) -> None:
        ancestor = self.base / "linked"
        ancestor.symlink_to(self.outside / "missing", target_is_directory=True)
        self.request.registration_root = str(ancestor / "runners")
        self.policy.allowed_registration_roots = (str(ancestor),)
        self.assert_static_refusal()

    def test_rejects_file_root_before_token_or_download(self) -> None:
        self.root.write_text("not a directory")
        self.assert_static_refusal()

    def test_rejects_file_ancestor_before_token_or_download(self) -> None:
        ancestor = self.base / "file"
        ancestor.write_text("not a directory")
        self.request.registration_root = str(ancestor / "runners")
        self.policy.allowed_registration_roots = (str(ancestor),)
        self.assert_static_refusal()

    def test_rejects_wrong_owner_before_token_or_download(self) -> None:
        self.root.mkdir()
        self.request.service_user += "-different-owner"
        self.assert_static_refusal()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_rejects_existing_lane_before_token_or_download(self) -> None:
        lane = self.root / self.request.lane_name
        lane.mkdir(parents=True)
        sentinel = lane / "keep"
        sentinel.write_text("existing registration")
        self.assert_static_refusal()
        self.assertEqual(sentinel.read_text(), "existing registration")

    def test_rejects_dangling_lane_before_token_or_download(self) -> None:
        self.root.mkdir()
        (self.root / self.request.lane_name).symlink_to(self.outside / "missing")
        self.assert_static_refusal()

    def test_rechecks_root_after_download_before_extraction(self) -> None:
        (self.outside / self.request.lane_name).mkdir()
        self.env["SWAP_AFTER_DOWNLOAD"] = "1"
        result = self.run_executor()
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.commands(), ["curl"])
        self.assertEqual(self.tokens.calls, [self.request.repository])
        self.assertFalse((self.outside / self.request.lane_name / "config.sh").exists())

    def test_rechecks_target_after_token_before_preparation(self) -> None:
        self.root.mkdir()
        original_fetch = self.tokens.fetch_registration_token

        def fetch_and_swap(*, repository: str) -> tuple[str, RunnerLaneRegistrationTokenRecord]:
            token = original_fetch(repository=repository)
            self.root.rename(self.base / "retained")
            self.root.symlink_to(self.outside, target_is_directory=True)
            return token

        with patch.object(self.tokens, "fetch_registration_token", side_effect=fetch_and_swap):
            result = self.run_executor()
        self.assertEqual(result.status, "failed")
        self.assertNotIn("configure", self.commands())
        self.assertNotIn("sudo", self.commands())
        self.assertEqual(self.tokens.calls, [self.request.repository])

    def test_rechecks_root_before_configuration_with_executable_outside_target(self) -> None:
        outside_lane = self.outside / self.request.lane_name
        outside_lane.mkdir()
        config = outside_lane / "config.sh"
        config.write_text('#!/bin/sh\nprintf \'"configure"\\n\' >> "$FIXTURE_LOG"\n')
        config.chmod(0o700)
        self.swap_before_configuration = True

        result = self.run_executor()

        self.assertEqual(result.status, "failed")
        self.assertNotIn("configure", self.commands())
        self.assertNotIn("sudo", self.commands())
        self.assertEqual(self.tokens.calls, [self.request.repository])

    def test_rechecks_root_after_configuration_before_starting_service(self) -> None:
        self.swap_after_configuration = True

        result = self.run_executor()

        self.assertEqual(result.status, "failed")
        self.assertIn("configure", self.commands())
        self.assertNotIn("sudo", self.commands())
        self.assertEqual(self.audits.records[-1][0], "failed")

    def test_rechecks_root_after_inventory_before_completed_audit(self) -> None:
        self.swap_during_inventory = True

        result = self.run_executor()

        self.assertEqual(result.status, "failed")
        self.assertEqual(self.audits.records[-1][0], "failed")


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
    registration_root: str = "/home/launchplane-runner-hygiene/actions-runners",
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
        registration_root=registration_root,
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
