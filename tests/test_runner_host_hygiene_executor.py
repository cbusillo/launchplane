from __future__ import annotations

import json
from email.message import Message
from functools import partial
from io import BytesIO
import tempfile
import unittest

from collections.abc import Sequence
from collections.abc import Mapping
import subprocess
from pathlib import Path
import time
from typing import Any, Literal
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

import click

from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.contracts.runner_host_hygiene_idle import (
    RunnerHostHygieneIdleObservation,
)
from control_plane.workflows.runner_host_hygiene_executor import RemoteCommandResult
from control_plane.workflows.runner_host_hygiene_executor import RunnerHostHygieneExecutorRequest
from control_plane.workflows.runner_host_hygiene_executor import GeneratedRunCacheRoot
from control_plane.workflows.runner_host_hygiene_executor import RunnerHostGitHubBinding
from control_plane.workflows.runner_host_hygiene_executor import RunnerWorkdirRoot
from control_plane.workflows.runner_host_hygiene_executor import AuditDeliveryError
from control_plane.workflows.runner_host_hygiene_executor import (
    build_refreshing_service_audit_poster,
)
from control_plane.workflows.runner_host_hygiene_executor import (
    build_github_idle_evidence_reader,
)
from control_plane.workflows.runner_host_hygiene_executor import build_github_run_state_reader
from control_plane.workflows.runner_host_hygiene_executor import (
    execute_runner_host_hygiene_executor as _execute_runner_host_hygiene_executor,
)
from control_plane.workflows.runner_host_hygiene_executor import _DOCKER_DISK_USAGE_COMMAND
from control_plane.workflows.runner_host_hygiene_executor import _GENERATED_RUN_CACHE_HELPER
from control_plane.workflows.runner_host_hygiene_executor import (
    MAX_DOCKER_IMAGE_INVENTORY_ITEMS,
)
from control_plane.workflows.runner_host_hygiene_executor import (
    MAX_DOCKER_VOLUME_INVENTORY_ITEMS,
)
from control_plane.workflows.runner_host_hygiene_executor import _parse_docker_disk_usage
from control_plane.workflows.runner_host_hygiene_executor import _RUNNER_WORKDIR_USAGE_HELPER
from control_plane.workflows.runner_host_hygiene_executor import (
    collect_runner_host_hygiene_report as _collect_runner_host_hygiene_report,
)
from control_plane.workflows.runner_host_hygiene_executor import _public_identity_key
from control_plane.workflows.runner_host_hygiene_executor import _aggregate_github_idle_samples
from control_plane.workflows.runner_host_hygiene_executor import validate_local_executor_environment
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.workflows.runner_host_hygiene_audit_spool import (
    RunnerHostHygieneAuditSpool,
)
from tests.support.workflows import load_workflow


_VOLUME_RM_PREFIX = (
    "flock",
    "-n",
    "/tmp/launchplane-runner-host-hygiene.lock",
    "docker",
    "volume",
    "rm",
)
_ENGINE_PRUNE_PREFIX = (
    "flock",
    "-n",
    "/tmp/launchplane-runner-host-hygiene.lock",
    "curl",
    "--silent",
)
_GENERATED_CACHE_PRUNE_PREFIX = (
    "flock",
    "-n",
    "/tmp/launchplane-runner-host-hygiene.lock",
    "/usr/bin/sudo",
    "-n",
    _GENERATED_RUN_CACHE_HELPER,
    "prune",
)


class _CommandRunner:
    def __init__(
        self,
        *,
        prune_returncode: int = 0,
        prune_stderr: str = "prune failed",
        image_present_after: bool = True,
        active_build_processes: str = "",
        volume_remove_returncode: int = 0,
        volume_remove_partial_failure: bool = False,
        docker_disk_usage: str | None = None,
        image_inventory: str | None = None,
        container_inventory: str | None = None,
        volume_inventory: str | None = None,
        runner_workdir_bytes: int = 0,
        runner_workdir_usage: Mapping[str, tuple[int, int]] | None = None,
        buildctl_bounded_prune_supported: bool = True,
        runner_workdir_status: Literal["complete", "partial"] = "complete",
        generated_cache_before: str = "",
        generated_cache_after: str = "",
        runner_service_output: str = ("launchplane-runner@ops-gate.service active running\n"),
    ) -> None:
        self.commands: list[tuple[str, ...]] = []
        self._prune_returncode = prune_returncode
        self._prune_stderr = prune_stderr
        self._volume_remove_returncode = volume_remove_returncode
        self._volume_remove_partial_failure = volume_remove_partial_failure
        self._image_present_after = image_present_after
        self._active_build_processes = active_build_processes
        self._image_inventory = image_inventory or _json_lines(
            {
                "CreatedAt": "2026-05-23 12:00:00 +0000 UTC",
                "ID": "sha256:warm-builder-id",
                "Repository": "odoo-docker-chris-testing",
                "Size": "1.2GB",
                "Tag": "latest",
            },
            {
                "CreatedAt": "2026-05-20 12:00:00 +0000 UTC",
                "ID": "sha256:dangling-id",
                "Repository": "<none>",
                "Size": "500MB",
                "Tag": "<none>",
            },
        )
        self._container_inventory = container_inventory or _json_lines(
            {
                "ID": "container-id",
                "Image": "odoo-docker-chris-testing:latest",
                "ImageID": "sha256:warm-builder-id",
            }
        )
        self._volume_inventory = volume_inventory or json.dumps(
            [
                {
                    "Driver": "local",
                    "Labels": {"launchplane.scope": "test"},
                    "Mountpoint": "/var/lib/docker/volumes/runner-cache/_data",
                    "Name": "runner-cache",
                    "UsageData": {"RefCount": 0, "Size": 45_500_000_000},
                }
            ]
        )
        self._docker_disk_usage = docker_disk_usage or _docker_disk_usage_json(
            images=[{"Containers": 0, "SharedSize": 0, "Size": 500_000_000}],
            volumes=json.loads(self._volume_inventory),
            build_cache=[{"InUse": False, "Shared": False, "Size": 1_000_000_000}],
        )
        self._runner_workdir_bytes = runner_workdir_bytes
        self._runner_workdir_usage = dict(runner_workdir_usage or {})
        self._buildctl_bounded_prune_supported = buildctl_bounded_prune_supported
        self._runner_workdir_status = runner_workdir_status
        self._generated_cache_before = generated_cache_before
        self._generated_cache_after = generated_cache_after
        self._runner_service_output = runner_service_output
        self._generated_cache_pruned = False
        self._pruned = False
        self.removed_volumes: list[str] = []

    def __call__(self, command: Sequence[str], _timeout_seconds: int) -> RemoteCommandResult:
        command_tuple = tuple(command)
        self.commands.append(command_tuple)
        if command_tuple == ("df", "-B1", "-P", "/"):
            return RemoteCommandResult(
                returncode=0,
                stdout="Filesystem 1B-blocks Used Available Use% Mounted on\n/dev/disk 1000 100 900 10% /\n",
            )
        if command_tuple == ("docker", "system", "df"):
            return RemoteCommandResult(returncode=0, stdout="TYPE TOTAL ACTIVE SIZE RECLAIMABLE\n")
        if command_tuple == _DOCKER_DISK_USAGE_COMMAND:
            return RemoteCommandResult(
                returncode=0,
                stdout=_without_removed_disk_usage_volumes(
                    self._docker_disk_usage,
                    self.removed_volumes,
                ),
            )
        if command_tuple == (
            "docker",
            "image",
            "ls",
            "--all",
            "--no-trunc",
            "--format",
            "{{json .}}",
        ):
            return RemoteCommandResult(returncode=0, stdout=self._image_inventory)
        if command_tuple == (
            "docker",
            "ps",
            "--all",
            "--no-trunc",
            "--format",
            "{{json .}}",
        ):
            return RemoteCommandResult(returncode=0, stdout=self._container_inventory)
        if command_tuple[:3] == (
            "/usr/bin/sudo",
            "-n",
            _RUNNER_WORKDIR_USAGE_HELPER,
        ):
            apparent_bytes = self._runner_workdir_bytes
            allocated_bytes = self._runner_workdir_bytes
            for root_path, root_usage in self._runner_workdir_usage.items():
                if command_tuple[3].endswith(f"={root_path}"):
                    apparent_bytes, allocated_bytes = root_usage
                    break
            return RemoteCommandResult(
                returncode=0,
                stdout=(
                    f"{apparent_bytes}\n{allocated_bytes}\n"
                    if self._runner_workdir_status == "complete"
                    else f"{apparent_bytes}\n{allocated_bytes}\npartial\n"
                ),
            )
        if command_tuple[:4] == (
            "/usr/bin/sudo",
            "-n",
            _GENERATED_RUN_CACHE_HELPER,
            "observe",
        ):
            return RemoteCommandResult(
                returncode=0,
                stdout=(
                    self._generated_cache_after
                    if self._generated_cache_pruned
                    else self._generated_cache_before
                ),
            )
        if command_tuple[:3] == ("docker", "image", "inspect"):
            if not self._pruned or self._image_present_after:
                return RemoteCommandResult(returncode=0, stdout="[]\n")
            return RemoteCommandResult(returncode=1, stderr="image missing")
        if command_tuple[:2] == ("bash", "-lc") and "pgrep -af" in command_tuple[2]:
            return RemoteCommandResult(returncode=0, stdout=self._active_build_processes)
        if command_tuple[:2] == ("bash", "-lc") and "systemctl list-units" in command_tuple[2]:
            return RemoteCommandResult(
                returncode=0,
                stdout=self._runner_service_output,
            )
        if command_tuple[:4] == ("docker", "inspect", "--format", "{{.State.Running}}"):
            return RemoteCommandResult(returncode=0, stdout="true\n")
        if command_tuple[:2] == ("docker", "exec") and command_tuple[3:] == (
            "buildctl",
            "prune",
            "--help",
        ):
            return RemoteCommandResult(
                returncode=0,
                stdout=(
                    "--keep-duration value\n--keep-storage value\n"
                    if self._buildctl_bounded_prune_supported
                    else "--filter filter\n"
                ),
            )
        if command_tuple[:5] == _ENGINE_PRUNE_PREFIX:
            self._pruned = True
            return RemoteCommandResult(
                returncode=self._prune_returncode,
                stderr=self._prune_stderr,
            )
        if command_tuple[:7] == _GENERATED_CACHE_PRUNE_PREFIX:
            self._generated_cache_pruned = True
            return RemoteCommandResult(
                returncode=self._prune_returncode,
                stderr=self._prune_stderr,
            )
        if command_tuple[:7] == (
            "flock",
            "-n",
            "/tmp/launchplane-runner-host-hygiene.lock",
            "docker",
            "image",
            "prune",
            "--force",
        ):
            self._pruned = True
            return RemoteCommandResult(
                returncode=self._prune_returncode,
                stderr=self._prune_stderr,
            )
        if command_tuple[:7] == (
            "flock",
            "-n",
            "/tmp/launchplane-runner-host-hygiene.lock",
            "docker",
            "exec",
            "buildx_buildkit_odoo-docker-chris-testing0",
            "buildctl",
        ):
            self._pruned = True
            return RemoteCommandResult(
                returncode=self._prune_returncode,
                stderr=self._prune_stderr,
            )
        if command_tuple[:6] == _VOLUME_RM_PREFIX:
            if self._volume_remove_partial_failure and len(command_tuple[6:]) > 1:
                self.removed_volumes.append(command_tuple[6])
                return RemoteCommandResult(
                    returncode=1,
                    stderr="second volume still in use",
                )
            self.removed_volumes.extend(command_tuple[6:])
            return RemoteCommandResult(
                returncode=self._volume_remove_returncode,
                stderr="volume rm failed",
            )
        return RemoteCommandResult(returncode=127, stderr="unexpected command")


def _github_idle_evidence_reader(
    repository: str,
    execution_lane: str,
    runner_name: str = "test-runner",
    _exclude_current: bool = True,
) -> tuple[RunnerHostHygieneIdleObservation, ...]:
    observed_at = utc_now_timestamp()
    subject_key = _public_identity_key(
        "runner",
        "|".join((repository.lower(), execution_lane.lower(), runner_name)),
    )
    return tuple(
        RunnerHostHygieneIdleObservation(
            source=source,
            scope="full_host",
            scope_key="host",
            subject_key=subject_key,
            observed_at=observed_at,
            availability="available",
            state="idle",
            active_count=0,
            total_count=1,
        )
        for source in ("github_jobs", "github_runners")
    )


def execute_runner_host_hygiene_executor(
    *,
    request: RunnerHostHygieneExecutorRequest,
    **kwargs: Any,
) -> Any:
    kwargs.setdefault("audit_delivery_sleeper", lambda _seconds: None)
    kwargs.setdefault("github_idle_evidence_reader", _github_idle_evidence_reader)
    if request.mutate and kwargs.get("audit_spool") is None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            kwargs["audit_spool"] = RunnerHostHygieneAuditSpool(
                root=Path(temporary_directory) / "spool"
            )
            return _execute_runner_host_hygiene_executor(request=request, **kwargs)
    return _execute_runner_host_hygiene_executor(request=request, **kwargs)


collect_runner_host_hygiene_report = partial(
    _collect_runner_host_hygiene_report,
    idle_observation_sleeper=lambda _seconds: None,
    github_idle_evidence_reader=_github_idle_evidence_reader,
)


def _buildkit_volume_evidence(
    volume_name: str, *, size_bytes: int = 50_490_000_000, links: int = 0
) -> tuple[str, Mapping[str, object]]:
    summary_row = f"{volume_name}    {links}         {size_bytes}B\n"
    inventory_row = {
        "Driver": "local",
        "Labels": {},
        "Mountpoint": f"/var/lib/docker/volumes/{volume_name}/_data",
        "Name": volume_name,
        "UsageData": {"RefCount": links, "Size": size_bytes},
    }
    return summary_row, inventory_row


class _AuditPoster:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []
        self.audits: list[RunnerHostHygieneApplyAuditRecord] = []

    def __call__(
        self, audit: RunnerHostHygieneApplyAuditRecord, idempotency_key: str
    ) -> dict[str, object]:
        self.records.append((audit.status, idempotency_key))
        self.audits.append(audit)
        return {
            "status": "accepted",
            "records": {"runner_host_hygiene_audit_record_key": audit.audit_record_key},
        }


class _TerminalFailingAuditPoster(_AuditPoster):
    def __call__(
        self, audit: RunnerHostHygieneApplyAuditRecord, idempotency_key: str
    ) -> dict[str, object]:
        if audit.status == "planned":
            return super().__call__(audit, idempotency_key)
        self.records.append((audit.status, idempotency_key))
        self.audits.append(audit)
        raise AuditDeliveryError("temporary audit service failure", retryable=True)


class _PermanentlyFailingAuditPoster(_AuditPoster):
    def __call__(
        self, audit: RunnerHostHygieneApplyAuditRecord, idempotency_key: str
    ) -> dict[str, object]:
        self.records.append((audit.status, idempotency_key))
        self.audits.append(audit)
        raise AuditDeliveryError("audit route rejected request", retryable=False)


class _FlakyTerminalAuditPoster(_AuditPoster):
    def __init__(self, *, failures: int) -> None:
        super().__init__()
        self._failures = failures

    def __call__(
        self, audit: RunnerHostHygieneApplyAuditRecord, idempotency_key: str
    ) -> dict[str, object]:
        if audit.status != "planned" and self._failures > 0:
            self._failures -= 1
            self.records.append((audit.status, idempotency_key))
            self.audits.append(audit)
            raise AuditDeliveryError("temporary audit service failure", retryable=True)
        return super().__call__(audit, idempotency_key)


class _SpoolAwareCommandRunner(_CommandRunner):
    def __init__(
        self,
        *,
        spool: RunnerHostHygieneAuditSpool,
        request: RunnerHostHygieneExecutorRequest,
    ) -> None:
        super().__init__()
        self._spool = spool
        self._request = request

    def __call__(self, command: Sequence[str], timeout_seconds: int) -> RemoteCommandResult:
        command_tuple = tuple(command)
        if command_tuple[:5] == _ENGINE_PRUNE_PREFIX:
            envelope = self._spool.read(
                host_name=self._request.host_name,
                action=self._request.action,
                audit_record_key=self._request.audit_record_key,
            )
            if envelope is None or envelope.execution_state != "action_started":
                return RemoteCommandResult(
                    returncode=99,
                    stderr="audit intent was not durable before mutation",
                )
        return super().__call__(command_tuple, timeout_seconds)


class _CrashingCommandRunner(_CommandRunner):
    def __call__(self, command: Sequence[str], timeout_seconds: int) -> RemoteCommandResult:
        command_tuple = tuple(command)
        if command_tuple[:5] == _ENGINE_PRUNE_PREFIX:
            raise RuntimeError("simulated runner termination")
        return super().__call__(command_tuple, timeout_seconds)


class _PreActionCrashingCommandRunner(_CommandRunner):
    def __init__(self) -> None:
        super().__init__()
        self._disk_observations = 0

    def __call__(self, command: Sequence[str], timeout_seconds: int) -> RemoteCommandResult:
        command_tuple = tuple(command)
        if command_tuple == ("df", "-B1", "-P", "/"):
            self._disk_observations += 1
            if self._disk_observations == 2:
                raise RuntimeError("simulated pre-action termination")
        return super().__call__(command_tuple, timeout_seconds)


class RunnerHostHygieneWorkflowTests(unittest.TestCase):
    def test_workflow_runs_on_ops_lane_without_xargs_dependency(self) -> None:
        workflow_path = Path(".github/workflows/runner-host-hygiene.yml")
        workflow_text = workflow_path.read_text(encoding="utf-8")
        workflow = load_workflow(workflow_path)

        self.assertIn(
            "    runs-on:\n"
            "      - self-hosted\n"
            "      - ${{ vars.LAUNCHPLANE_RUNNER_HOST_HYGIENE_EXECUTION_LANE }}\n",
            workflow_text,
        )
        self.assertNotIn("${{ vars.LAUNCHPLANE_RUNNER_LABEL }}", workflow_text)
        self.assertNotIn("xargs <<<", workflow_text)
        self.assertIn('trimmed="${builder#', workflow_text)
        self.assertIn("LAUNCHPLANE_RUNNER_HOST_HYGIENE_RUNNER_WORKDIR_ROOTS", workflow_text)
        self.assertIn("--runner-workdir-root", workflow_text)
        self.assertIn("--audit-spool-root", workflow_text)
        self.assertIn("--audit-artifact-file", workflow_text)
        self.assertIn("    timeout-minutes: 180\n", workflow_text)
        self.assertIn(
            "      - name: Upload executor result\n"
            "        if: always() && steps.executor.outcome != 'skipped'\n",
            workflow_text,
        )
        self.assertIn('    - cron: "17 10 * * 1-6"\n', workflow_text)
        self.assertIn('    - cron: "17 10 * * 0"\n', workflow_text)
        self.assertIn('if [ "$SCHEDULE_EXPRESSION" = "17 10 * * 0" ]; then\n', workflow_text)
        self.assertIn("prune_dangling_images", workflow_text)
        self.assertIn("RUNNER_BUILDKIT_CACHE_BUDGETS", workflow_text)
        self.assertIn("RUNNER_DEFAULT_CACHE_BUDGET_BYTES", workflow_text)
        self.assertIn("--target-buildkit-builder", workflow_text)
        self.assertIn("--max-used-space-bytes", workflow_text)
        self.assertIn(
            '--max-used-space-bytes "$RUNNER_DEFAULT_CACHE_BUDGET_BYTES"',
            workflow_text,
        )
        self.assertIn("maintenance_failures=$((maintenance_failures + 1))", workflow_text)
        self.assertIn("scheduled hygiene action(s) failed", workflow_text)
        self.assertIn("BuildKit cache budgets must not repeat a builder.", workflow_text)
        self.assertIn("LAUNCHPLANE_RUNNER_HOST_HYGIENE_GENERATED_RUN_CACHE_ROOTS", workflow_text)
        self.assertIn("prune_generated_run_cache", workflow_text)
        self.assertIn("--generated-run-cache-root", workflow_text)
        self.assertIn("--target-generated-cache-key", workflow_text)
        self.assertNotIn("LAUNCHPLANE_RUNNER_REGISTRATION_GITHUB_TOKEN", workflow_text)
        self.assertNotIn("LAUNCHPLANE_RUNNER_HOST_HYGIENE_GITHUB_READ_TOKEN", workflow_text)

        token_step = workflow.step_named("runner-host-hygiene", "Mint GitHub read token")
        self.assertIsNotNone(token_step)
        assert token_step is not None
        self.assertEqual(
            token_step.uses,
            "actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1",
        )
        self.assertEqual(token_step.data.get("id"), "github-read-token")
        self.assertEqual(
            token_step.with_values.get("client-id"),
            "${{ vars.LAUNCHPLANE_RUNNER_HOST_HYGIENE_GITHUB_APP_CLIENT_ID }}",
        )
        self.assertEqual(
            token_step.with_values.get("private-key"),
            "${{ secrets.LAUNCHPLANE_RUNNER_HOST_HYGIENE_GITHUB_APP_PRIVATE_KEY }}",
        )
        self.assertEqual(token_step.with_values.get("owner"), "${{ github.repository_owner }}")
        self.assertNotIn("repositories", token_step.with_values)
        self.assertEqual(token_step.with_values.get("permission-actions"), "read")
        self.assertEqual(token_step.with_values.get("permission-administration"), "read")
        self.assertNotIn("skip-token-revoke", token_step.with_values)

        installation_step = workflow.step_named(
            "runner-host-hygiene", "Validate GitHub App installation scope"
        )
        self.assertIsNotNone(installation_step)
        assert installation_step is not None
        self.assertIn(
            "control_plane.workflows.runner_host_github_app",
            installation_step.run,
        )
        self.assertIn("GH_TOKEN: ${{ steps.github-read-token.outputs.token }}", workflow_text)


class RunnerHostHygieneExecutorTests(unittest.TestCase):
    def test_full_host_idle_requires_complete_binding_manifest(self) -> None:
        report = collect_runner_host_hygiene_report(
            request=_request(
                mutate=False,
                current_runner_name="",
                github_idle_bindings=(),
            ),
            remote_runner=_CommandRunner(),
        )

        convergence = report.idle_convergences[0]
        self.assertEqual(convergence.status, "incomplete")
        self.assertIn("source_missing", convergence.blocker_codes)
        self.assertEqual(
            {
                observation.source
                for observation in convergence.observations
                if observation.reason_code == "source_missing"
            },
            {"github_jobs", "github_runners", "runner_services"},
        )

    def test_full_host_idle_covers_every_repository_binding(self) -> None:
        bindings = (
            RunnerHostGitHubBinding(
                repository_scope="cbusillo/launchplane",
                execution_lane="shared-lane",
                runner_name="primary-runner",
                service_unit="launchplane-runner@ops-gate.service",
            ),
            RunnerHostGitHubBinding(
                repository_scope="example/secondary",
                execution_lane="shared-lane",
                runner_name="secondary-runner",
                service_unit="launchplane-runner@secondary.service",
            ),
        )

        def github_reader(
            repository: str,
            execution_lane: str,
            runner_name: str,
            _exclude_current: bool,
        ) -> tuple[RunnerHostHygieneIdleObservation, ...]:
            subject_key = _public_identity_key(
                "runner",
                "|".join((repository, execution_lane, runner_name)),
            )
            active = repository == "example/secondary"
            return tuple(
                RunnerHostHygieneIdleObservation(
                    source=source,
                    scope="full_host",
                    scope_key="host",
                    subject_key=subject_key,
                    observed_at=utc_now_timestamp(),
                    availability="available",
                    state="active" if active and source == "github_jobs" else "idle",
                    active_count=1 if active and source == "github_jobs" else 0,
                    total_count=1,
                )
                for source in ("github_jobs", "github_runners")
            )

        report = _collect_runner_host_hygiene_report(
            request=_request(
                mutate=False,
                execution_lane="shared-lane",
                current_runner_name="primary-runner",
                github_idle_bindings=bindings,
            ),
            remote_runner=_CommandRunner(
                runner_service_output=(
                    "launchplane-runner@ops-gate.service active running\n"
                    "launchplane-runner@secondary.service active running\n"
                )
            ),
            github_idle_evidence_reader=github_reader,
            idle_observation_sleeper=lambda _seconds: None,
        )

        convergence = report.idle_convergences[0]
        self.assertEqual(convergence.status, "active")
        github_observations = tuple(
            observation
            for observation in convergence.observations
            if observation.source in {"github_jobs", "github_runners"}
        )
        self.assertEqual(len(github_observations), 4)
        self.assertEqual(len({item.subject_key for item in github_observations}), 2)

    def test_full_host_idle_excludes_current_job_across_same_repository_lane(self) -> None:
        bindings = (
            RunnerHostGitHubBinding(
                repository_scope="cbusillo/launchplane",
                execution_lane="shared-lane",
                runner_name="primary-runner",
                service_unit="launchplane-runner@primary.service",
            ),
            RunnerHostGitHubBinding(
                repository_scope="cbusillo/launchplane",
                execution_lane="shared-lane",
                runner_name="secondary-runner",
                service_unit="launchplane-runner@secondary.service",
            ),
        )
        exclusions: list[tuple[str, bool]] = []

        def github_reader(
            repository: str,
            execution_lane: str,
            runner_name: str,
            exclude_current_job: bool,
        ) -> tuple[RunnerHostHygieneIdleObservation, ...]:
            exclusions.append((runner_name, exclude_current_job))
            subject_key = _public_identity_key(
                "runner",
                "|".join((repository, execution_lane, runner_name)),
            )
            return tuple(
                RunnerHostHygieneIdleObservation(
                    source=source,
                    scope="full_host",
                    scope_key="host",
                    subject_key=subject_key,
                    observed_at=utc_now_timestamp(),
                    availability="available",
                    state="idle",
                    active_count=0,
                    total_count=1,
                )
                for source in ("github_jobs", "github_runners")
            )

        report = _collect_runner_host_hygiene_report(
            request=_request(
                mutate=False,
                repository_scope="cbusillo/launchplane",
                execution_lane="shared-lane",
                current_runner_name="primary-runner",
                github_idle_bindings=bindings,
            ),
            remote_runner=_CommandRunner(
                runner_service_output=(
                    "launchplane-runner@primary.service active running\n"
                    "launchplane-runner@secondary.service active running\n"
                )
            ),
            github_idle_evidence_reader=github_reader,
            idle_observation_sleeper=lambda _seconds: None,
        )

        self.assertEqual(report.idle_convergences[0].status, "idle")
        self.assertEqual(
            set(exclusions),
            {("primary-runner", True), ("secondary-runner", True)},
        )

    def test_executor_posts_planned_and_completed_audits(self) -> None:
        command_runner = _CommandRunner()
        audit_poster = _AuditPoster()

        result = execute_runner_host_hygiene_executor(
            request=_request(mutate=True),
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "completed")
        self.assertIn(
            ("planned", "runner-host-hygiene:runner-host-hygiene/2026-05-23/chris-testing:planned"),
            audit_poster.records,
        )
        self.assertIn(
            (
                "completed",
                "runner-host-hygiene:runner-host-hygiene/2026-05-23/chris-testing:completed",
            ),
            audit_poster.records,
        )
        self.assertIn(
            (
                "flock",
                "-n",
                "/tmp/launchplane-runner-host-hygiene.lock",
                "curl",
                "--silent",
                "--show-error",
                "--fail",
                "--request",
                "POST",
                "--unix-socket",
                "/var/run/docker.sock",
                (
                    "http://localhost/v1.45/build/prune?all=true&filters="
                    "%7B%22until%22%3A%5B%22168h%22%5D%7D"
                ),
            ),
            command_runner.commands,
        )

    def test_executor_bounds_one_allowlisted_buildx_builder(self) -> None:
        command_runner = _CommandRunner()
        audit_poster = _AuditPoster()

        result = execute_runner_host_hygiene_executor(
            request=_request(
                mutate=True,
                target_buildkit_builder="odoo-docker-chris-testing",
                allowed_buildkit_builders=("odoo-docker-chris-testing",),
                max_used_space_bytes=64_424_509_440,
            ),
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "completed")
        self.assertIn(
            (
                "flock",
                "-n",
                "/tmp/launchplane-runner-host-hygiene.lock",
                "docker",
                "exec",
                "buildx_buildkit_odoo-docker-chris-testing0",
                "buildctl",
                "prune",
                "--all",
                "--keep-duration",
                "168h",
                "--keep-storage",
                "64425",
            ),
            command_runner.commands,
        )
        self.assertEqual(
            audit_poster.audits[0].request.target_buildkit_builder,
            "odoo-docker-chris-testing",
        )
        self.assertEqual(audit_poster.audits[0].request.max_used_space_bytes, 64_424_509_440)

    def test_executor_bounds_default_daemon_cache_without_age_filter(self) -> None:
        command_runner = _CommandRunner()

        result = execute_runner_host_hygiene_executor(
            request=_request(mutate=True, max_used_space_bytes=17_179_869_184),
            remote_runner=command_runner,
            audit_poster=_AuditPoster(),
        )

        self.assertEqual(result.status, "completed")
        prune_command = next(
            command for command in command_runner.commands if command[:5] == _ENGINE_PRUNE_PREFIX
        )
        self.assertEqual(
            prune_command[-1],
            "http://localhost/v1.45/build/prune?all=true&keep-storage=17179869184",
        )
        self.assertNotIn("filters", prune_command[-1])

    def test_executor_rejects_unsafe_default_daemon_cache_budget(self) -> None:
        with self.assertRaisesRegex(ValueError, "retain at least 8 GiB"):
            _request(mutate=True, max_used_space_bytes=(8 * 1024**3) - 1)

    def test_executor_rejects_prune_age_below_retention_floor(self) -> None:
        with self.assertRaisesRegex(ValueError, "retain at least 168 hours"):
            _request(mutate=True, prune_until="167h")

    def test_executor_rejects_zero_idle_observation_interval(self) -> None:
        with self.assertRaisesRegex(ValueError, "idle_observation_interval_seconds"):
            _request(mutate=True, idle_observation_interval_seconds=0)

    def test_executor_normalizes_prune_age_hours(self) -> None:
        request = _request(mutate=True, prune_until="168H")

        self.assertEqual(request.prune_until, "168h")

    def test_executor_blocks_unallowlisted_buildx_builder(self) -> None:
        command_runner = _CommandRunner()

        result = execute_runner_host_hygiene_executor(
            request=_request(
                mutate=True,
                target_buildkit_builder="unapproved-builder",
                allowed_buildkit_builders=("odoo-docker-chris-testing",),
                max_used_space_bytes=64_424_509_440,
            ),
            remote_runner=command_runner,
            audit_poster=_AuditPoster(),
        )

        self.assertEqual(result.status, "blocked")
        self.assertFalse(
            any(command[4:6] == ("buildx", "prune") for command in command_runner.commands)
        )

    def test_executor_fails_before_prune_when_buildctl_lacks_bounded_flags(self) -> None:
        command_runner = _CommandRunner(buildctl_bounded_prune_supported=False)

        result = execute_runner_host_hygiene_executor(
            request=_request(
                mutate=True,
                target_buildkit_builder="odoo-docker-chris-testing",
                allowed_buildkit_builders=("odoo-docker-chris-testing",),
                max_used_space_bytes=64_424_509_440,
            ),
            remote_runner=command_runner,
            audit_poster=_AuditPoster(),
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("does not support bounded duration and storage", result.message)
        self.assertFalse(
            any(
                command[:8]
                == (
                    "flock",
                    "-n",
                    "/tmp/launchplane-runner-host-hygiene.lock",
                    "docker",
                    "exec",
                    "buildx_buildkit_odoo-docker-chris-testing0",
                    "buildctl",
                    "prune",
                )
                for command in command_runner.commands
            )
        )

    def test_executor_prunes_only_dangling_images(self) -> None:
        command_runner = _CommandRunner()

        result = execute_runner_host_hygiene_executor(
            request=_request(mutate=True, action="prune_dangling_images"),
            remote_runner=command_runner,
            audit_poster=_AuditPoster(),
        )

        self.assertEqual(result.status, "completed")
        command = (
            "flock",
            "-n",
            "/tmp/launchplane-runner-host-hygiene.lock",
            "docker",
            "image",
            "prune",
            "--force",
            "--filter",
            "until=168h",
        )
        self.assertIn(command, command_runner.commands)
        image_prune_commands = [
            invoked for invoked in command_runner.commands if invoked[4:6] == ("image", "prune")
        ]
        self.assertEqual(image_prune_commands, [command])
        self.assertTrue(
            all("-a" not in invoked and "--all" not in invoked for invoked in image_prune_commands)
        )

    def test_executor_records_typed_docker_reclaimable_bytes(self) -> None:
        command_runner = _CommandRunner(
            docker_disk_usage=_docker_disk_usage_json(
                images=[{"Containers": 0, "SharedSize": 0, "Size": 23_120_000_000}],
                volumes=[
                    {
                        "Driver": "local",
                        "Labels": {},
                        "Mountpoint": "/var/lib/docker/volumes/reclaimable/_data",
                        "Name": "reclaimable",
                        "UsageData": {"RefCount": 0, "Size": 97_470_000_000},
                    }
                ],
                build_cache=[{"InUse": False, "Shared": False, "Size": 27_880_000_000}],
            )
        )
        audit_poster = _AuditPoster()

        result = execute_runner_host_hygiene_executor(
            request=_request(mutate=True),
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "completed")
        reclaimable_bytes = 23_120_000_000 + 97_470_000_000 + 27_880_000_000
        for audit in audit_poster.audits:
            pre_apply_report = getattr(audit, "pre_apply_report")
            self.assertEqual(
                pre_apply_report.docker_reclaimable_bytes,
                reclaimable_bytes,
            )
        terminal_audit = audit_poster.audits[-1]
        post_apply_report = terminal_audit.post_apply_report
        self.assertIsNotNone(post_apply_report)
        assert post_apply_report is not None
        self.assertEqual(
            post_apply_report.docker_reclaimable_bytes,
            reclaimable_bytes,
        )
        self.assertEqual(
            post_apply_report.docker_reclaimable_breakdown.images_bytes,
            23_120_000_000,
        )
        self.assertEqual(
            post_apply_report.docker_reclaimable_breakdown.local_volumes_bytes,
            97_470_000_000,
        )
        self.assertEqual(
            post_apply_report.docker_reclaimable_breakdown.build_cache_bytes,
            27_880_000_000,
        )

    def test_executor_records_read_only_resource_inventory(self) -> None:
        command_runner = _CommandRunner()
        audit_poster = _AuditPoster()

        result = execute_runner_host_hygiene_executor(
            request=_request(mutate=True),
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "completed")
        terminal_audit = audit_poster.audits[-1]
        post_apply_report = terminal_audit.post_apply_report
        self.assertIsNotNone(post_apply_report)
        assert post_apply_report is not None
        warm_image = next(
            image for image in post_apply_report.image_inventory if image.is_warm_builder
        )
        self.assertEqual(warm_image.repository, "")
        self.assertEqual(warm_image.tag, "")
        self.assertEqual(warm_image.created_at, "")
        self.assertEqual(warm_image.size_bytes, 1_200_000_000)
        self.assertTrue(warm_image.in_use)
        self.assertFalse(warm_image.dangling)
        self.assertTrue(warm_image.is_warm_builder)
        dangling_image = next(
            image for image in post_apply_report.image_inventory if image.image_id == "dangling-id"
        )
        self.assertTrue(dangling_image.dangling)
        volume = post_apply_report.volume_inventory[0]
        self.assertEqual(volume.name, "runner-cache")
        self.assertEqual(volume.size_bytes, 45_500_000_000)
        self.assertEqual(volume.referenced_by_containers, 0)
        self.assertTrue(volume.dangling)
        self.assertEqual(volume.mountpoint, "")
        self.assertEqual(volume.labels, ())
        self.assertEqual(command_runner.commands.count(_DOCKER_DISK_USAGE_COMMAND), 3)

    def test_report_bounds_docker_inventories_and_marks_telemetry_partial(self) -> None:
        image_rows: list[dict[str, object]] = [
            {
                "CreatedAt": "2026-07-30 01:00:00 +0000 UTC",
                "ID": f"sha256:image-{index}",
                "Repository": f"example/image-{index}",
                "Size": "1MB",
                "Tag": "latest",
            }
            for index in range(MAX_DOCKER_IMAGE_INVENTORY_ITEMS + 2)
        ]
        image_inventory = _json_lines(*image_rows)
        volume_inventory = [
            {
                "Driver": "local",
                "Labels": {},
                "Mountpoint": f"/var/lib/docker/volumes/cache-{index}/_data",
                "Name": f"cache-{index}",
                "UsageData": {"RefCount": 0, "Size": 1_000_000},
            }
            for index in range(MAX_DOCKER_VOLUME_INVENTORY_ITEMS + 2)
        ]
        command_runner = _CommandRunner(
            image_inventory=image_inventory,
            volume_inventory=json.dumps(volume_inventory),
        )

        report = collect_runner_host_hygiene_report(
            request=_request(mutate=False),
            remote_runner=command_runner,
        )

        self.assertEqual(len(report.image_inventory), MAX_DOCKER_IMAGE_INVENTORY_ITEMS)
        self.assertTrue(report.image_inventory_truncated)
        self.assertEqual(len(report.volume_inventory), MAX_DOCKER_VOLUME_INVENTORY_ITEMS)
        self.assertTrue(report.volume_inventory_truncated)
        telemetry = {item.cache_class: item for item in report.cache_class_telemetry}
        self.assertEqual(telemetry["docker_images"].availability, "available")
        self.assertEqual(telemetry["docker_volumes"].availability, "available")
        self.assertEqual(telemetry["docker_images"].entry_count, 1)
        self.assertEqual(
            telemetry["docker_volumes"].entry_count,
            MAX_DOCKER_VOLUME_INVENTORY_ITEMS + 2,
        )

    def test_report_preserves_unavailable_docker_image_size(self) -> None:
        command_runner = _CommandRunner(
            image_inventory=_json_lines(
                {
                    "CreatedAt": "2026-07-30 01:00:00 +0000 UTC",
                    "ID": "sha256:image-without-size",
                    "Repository": "example/image",
                    "Size": "N/A",
                    "Tag": "latest",
                }
            )
        )

        report = collect_runner_host_hygiene_report(
            request=_request(mutate=False),
            remote_runner=command_runner,
        )

        self.assertIsNone(report.image_inventory[0].size_bytes)
        image_telemetry = next(
            item for item in report.cache_class_telemetry if item.cache_class == "docker_images"
        )
        self.assertEqual(image_telemetry.availability, "available")
        self.assertEqual(image_telemetry.logical_bytes, 500_000_000)

    def test_executor_records_runner_workdir_bytes(self) -> None:
        command_runner = _CommandRunner(runner_workdir_bytes=12_345_678)
        audit_poster = _AuditPoster()

        result = execute_runner_host_hygiene_executor(
            request=_request(mutate=True),
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "completed")
        terminal_audit = audit_poster.audits[-1]
        post_apply_report = terminal_audit.post_apply_report
        self.assertIsNotNone(post_apply_report)
        assert post_apply_report is not None
        self.assertEqual(post_apply_report.runner_workdir_bytes, 12_345_678)

    def test_generated_run_cache_prune_uses_completed_idle_targets(self) -> None:
        root = GeneratedRunCacheRoot(
            key="codex-lab-runs",
            path="/home/gha/.cache/codex-ci",
            source_repository="cbusillo/codex-lab",
            high_water_bytes=100,
            low_water_bytes=10,
            minimum_age_hours=1,
            cooldown_hours=1,
        )
        command_runner = _CommandRunner(
            generated_cache_before=_generated_cache_evidence(
                entries=((101, 80, 80, 7_200, (0, 0)), (102, 40, 40, 5_400, (0, 0))),
                worker_observations=(0, 0),
            ),
            generated_cache_after=_generated_cache_evidence(
                entries=(),
                worker_observations=(0, 0),
                last_cleanup_epoch_seconds=1_000,
                last_reclaimed_bytes=120,
            ),
        )
        audit_poster = _AuditPoster()

        with tempfile.TemporaryDirectory() as temporary_directory:
            result = execute_runner_host_hygiene_executor(
                request=_request(
                    mutate=True,
                    action="prune_generated_run_cache",
                    generated_run_cache_roots=(root,),
                    target_generated_cache_key=root.key,
                ),
                remote_runner=command_runner,
                audit_poster=audit_poster,
                audit_spool=RunnerHostHygieneAuditSpool(
                    root=Path(temporary_directory),
                    artifact_file=Path(temporary_directory) / "artifact.json",
                ),
                github_run_state_reader=lambda _repository, run_ids: {
                    run_id: "completed" for run_id in run_ids
                },
            )

        self.assertEqual(result.status, "completed")
        self.assertEqual(
            audit_poster.audits[0].request.target_generated_cache_run_ids,
            (101, 102),
        )
        pre_apply_idle = audit_poster.audits[0].pre_apply_report.idle_convergences
        self.assertEqual(len(pre_apply_idle), 1)
        self.assertEqual(pre_apply_idle[0].scope, "isolated_user")
        self.assertEqual(pre_apply_idle[0].scope_key, root.key)
        self.assertEqual(pre_apply_idle[0].status, "idle")
        self.assertEqual(
            {observation.source for observation in pre_apply_idle[0].observations},
            {"github_jobs", "local_user_processes", "open_handles"},
        )
        prune_command = next(
            command
            for command in command_runner.commands
            if command[:7] == _GENERATED_CACHE_PRUNE_PREFIX
        )
        self.assertEqual(prune_command[-1], "101,102")
        self.assertFalse(any(command[:2] == ("bash", "-lc") for command in command_runner.commands))
        serialized_audits = json.dumps(
            [audit.model_dump(mode="json") for audit in audit_poster.audits]
        )
        self.assertNotIn(root.path, serialized_audits)
        self.assertNotIn(root.source_repository, serialized_audits)

    def test_generated_run_cache_prune_blocks_busy_owner(self) -> None:
        root = GeneratedRunCacheRoot(
            key="codex-lab-runs",
            path="/home/gha/.cache/codex-ci",
            source_repository="cbusillo/codex-lab",
            high_water_bytes=100,
            low_water_bytes=10,
            minimum_age_hours=1,
            cooldown_hours=1,
        )
        command_runner = _CommandRunner(
            generated_cache_before=_generated_cache_evidence(
                entries=((101, 120, 120, 7_200, (0, 0)),),
                worker_observations=(1, 1),
            )
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            result = execute_runner_host_hygiene_executor(
                request=_request(
                    mutate=True,
                    action="prune_generated_run_cache",
                    generated_run_cache_roots=(root,),
                    target_generated_cache_key=root.key,
                ),
                remote_runner=command_runner,
                audit_poster=_AuditPoster(),
                audit_spool=RunnerHostHygieneAuditSpool(
                    root=Path(temporary_directory),
                    artifact_file=Path(temporary_directory) / "artifact.json",
                ),
                github_run_state_reader=lambda _repository, run_ids: {
                    run_id: "completed" for run_id in run_ids
                },
            )

        self.assertEqual(result.status, "blocked")
        self.assertFalse(
            any(command[:7] == _GENERATED_CACHE_PRUNE_PREFIX for command in command_runner.commands)
        )

    def test_generated_run_cache_prune_blocks_unknown_github_state(self) -> None:
        root = GeneratedRunCacheRoot(
            key="codex-lab-runs",
            path="/home/gha/.cache/codex-ci",
            source_repository="cbusillo/codex-lab",
            high_water_bytes=100,
            low_water_bytes=10,
            minimum_age_hours=1,
            cooldown_hours=1,
        )
        command_runner = _CommandRunner(
            generated_cache_before=_generated_cache_evidence(
                entries=((101, 120, 120, 7_200, (0, 0)),),
                worker_observations=(0, 0),
            )
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            result = execute_runner_host_hygiene_executor(
                request=_request(
                    mutate=True,
                    action="prune_generated_run_cache",
                    generated_run_cache_roots=(root,),
                    target_generated_cache_key=root.key,
                ),
                remote_runner=command_runner,
                audit_poster=_AuditPoster(),
                audit_spool=RunnerHostHygieneAuditSpool(
                    root=Path(temporary_directory),
                    artifact_file=Path(temporary_directory) / "artifact.json",
                ),
                github_run_state_reader=lambda _repository, run_ids: {
                    run_id: "unknown" for run_id in run_ids
                },
            )

        self.assertEqual(result.status, "blocked")
        self.assertFalse(
            any(command[:7] == _GENERATED_CACHE_PRUNE_PREFIX for command in command_runner.commands)
        )

    def test_generated_run_cache_preserves_unauthorized_github_state(self) -> None:
        root = GeneratedRunCacheRoot(
            key="codex-lab-runs",
            path="/home/gha/.cache/codex-ci",
            source_repository="cbusillo/codex-lab",
            high_water_bytes=100,
            low_water_bytes=10,
            minimum_age_hours=1,
            cooldown_hours=1,
        )
        command_runner = _CommandRunner(
            generated_cache_before=_generated_cache_evidence(
                entries=((101, 120, 120, 7_200, (0, 0)),),
                worker_observations=(0, 0),
            )
        )
        audit_poster = _AuditPoster()

        result = execute_runner_host_hygiene_executor(
            request=_request(
                mutate=True,
                action="prune_generated_run_cache",
                generated_run_cache_roots=(root,),
                target_generated_cache_key=root.key,
            ),
            remote_runner=command_runner,
            audit_poster=audit_poster,
            github_run_state_reader=lambda _repository, run_ids: {
                run_id: "unauthorized" for run_id in run_ids
            },
        )

        self.assertEqual(result.status, "blocked")
        cache = audit_poster.audits[0].pre_apply_report.generated_cache_usage[0]
        self.assertEqual(cache.entries[0].github_run_state, "unauthorized")
        assert cache.idle_convergence is not None
        github_observation = next(
            item for item in cache.idle_convergence.observations if item.source == "github_jobs"
        )
        self.assertEqual(github_observation.reason_code, "source_unauthorized")

    def test_generated_run_cache_rejects_unimplemented_isolated_lane_scope(self) -> None:
        root = GeneratedRunCacheRoot(
            key="codex-lab-runs",
            path="/home/gha/.cache/codex-ci",
            source_repository="cbusillo/codex-lab",
            high_water_bytes=100,
            low_water_bytes=10,
            minimum_age_hours=1,
            cooldown_hours=1,
            idle_scope="isolated_lane",
        )

        with self.assertRaisesRegex(ValueError, "isolated_user"):
            _request(
                mutate=False,
                action="prune_generated_run_cache",
                generated_run_cache_roots=(root,),
                target_generated_cache_key=root.key,
            )

    def test_generated_run_cache_mutation_requires_durable_spool(self) -> None:
        root = GeneratedRunCacheRoot(
            key="codex-lab-runs",
            path="/home/gha/.cache/codex-ci",
            source_repository="cbusillo/codex-lab",
            high_water_bytes=100,
            low_water_bytes=10,
            minimum_age_hours=1,
            cooldown_hours=1,
        )

        with self.assertRaisesRegex(click.ClickException, "requires durable"):
            _execute_runner_host_hygiene_executor(
                request=_request(
                    mutate=True,
                    action="prune_generated_run_cache",
                    generated_run_cache_roots=(root,),
                    target_generated_cache_key=root.key,
                ),
                remote_runner=_CommandRunner(),
                audit_poster=_AuditPoster(),
                audit_delivery_sleeper=lambda _seconds: None,
                github_idle_evidence_reader=_github_idle_evidence_reader,
            )

    def test_generated_run_cache_below_high_water_is_not_needed(self) -> None:
        root = GeneratedRunCacheRoot(
            key="codex-lab-runs",
            path="/home/gha/.cache/codex-ci",
            source_repository="cbusillo/codex-lab",
            high_water_bytes=100,
            low_water_bytes=10,
            minimum_age_hours=1,
            cooldown_hours=1,
        )
        command_runner = _CommandRunner(
            generated_cache_before=_generated_cache_evidence(
                entries=((101, 80, 80, 7_200, (0, 0)),),
                worker_observations=(0, 0),
            )
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            result = execute_runner_host_hygiene_executor(
                request=_request(
                    mutate=True,
                    action="prune_generated_run_cache",
                    generated_run_cache_roots=(root,),
                    target_generated_cache_key=root.key,
                ),
                remote_runner=command_runner,
                audit_poster=_AuditPoster(),
                audit_spool=RunnerHostHygieneAuditSpool(
                    root=Path(temporary_directory),
                    artifact_file=Path(temporary_directory) / "artifact.json",
                ),
                github_run_state_reader=lambda _repository, run_ids: {
                    run_id: "completed" for run_id in run_ids
                },
            )

        self.assertEqual(result.status, "not_needed")
        self.assertFalse(
            any(command[:7] == _GENERATED_CACHE_PRUNE_PREFIX for command in command_runner.commands)
        )

    def test_generated_run_cache_above_high_water_during_cooldown_is_blocked(self) -> None:
        root = GeneratedRunCacheRoot(
            key="codex-lab-runs",
            path="/home/gha/.cache/codex-ci",
            source_repository="cbusillo/codex-lab",
            high_water_bytes=100,
            low_water_bytes=10,
            minimum_age_hours=1,
            cooldown_hours=1,
        )
        command_runner = _CommandRunner(
            generated_cache_before=_generated_cache_evidence(
                entries=((101, 120, 120, 7_200, (0, 0)),),
                worker_observations=(0, 0),
                observed_epoch_seconds=10_000,
                last_cleanup_epoch_seconds=9_900,
            )
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            result = execute_runner_host_hygiene_executor(
                request=_request(
                    mutate=True,
                    action="prune_generated_run_cache",
                    generated_run_cache_roots=(root,),
                    target_generated_cache_key=root.key,
                ),
                remote_runner=command_runner,
                audit_poster=_AuditPoster(),
                audit_spool=RunnerHostHygieneAuditSpool(
                    root=Path(temporary_directory),
                    artifact_file=Path(temporary_directory) / "artifact.json",
                ),
                github_run_state_reader=lambda _repository, run_ids: {
                    run_id: "completed" for run_id in run_ids
                },
            )

        self.assertEqual(result.status, "blocked")
        self.assertFalse(
            any(command[:7] == _GENERATED_CACHE_PRUNE_PREFIX for command in command_runner.commands)
        )

    def test_cache_prune_completes_when_unrelated_workdir_evidence_is_partial(self) -> None:
        command_runner = _CommandRunner(runner_workdir_status="partial")

        result = execute_runner_host_hygiene_executor(
            request=_request(mutate=True),
            remote_runner=command_runner,
            audit_poster=_AuditPoster(),
        )

        self.assertEqual(result.status, "completed")
        self.assertIn("post evidence still needs attention", result.message)
        self.assertTrue(
            any(command[:5] == _ENGINE_PRUNE_PREFIX for command in command_runner.commands)
        )

    def test_executor_uses_root_owned_workdir_usage_helper(self) -> None:
        command_runner = _CommandRunner(runner_workdir_bytes=12_345)

        result = execute_runner_host_hygiene_executor(
            request=_request(mutate=True),
            remote_runner=command_runner,
            audit_poster=_AuditPoster(),
        )

        self.assertEqual(result.status, "completed")
        self.assertIn(
            (
                "/usr/bin/sudo",
                "-n",
                _RUNNER_WORKDIR_USAGE_HELPER,
                "legacy=/opt/actions-runners",
            ),
            command_runner.commands,
        )

    def test_workdir_usage_helper_path_and_security_invariants_are_pinned(self) -> None:
        helper_path = (
            Path(__file__).resolve().parents[1] / "scripts" / "runner-host-hygiene-workdir-usage.sh"
        )
        helper_text = helper_path.read_text(encoding="utf-8")

        self.assertEqual(
            _RUNNER_WORKDIR_USAGE_HELPER,
            "/usr/local/sbin/launchplane-runner-workdir-usage",
        )
        self.assertTrue(helper_path.stat().st_mode & 0o111)
        self.assertTrue(helper_text.startswith("#!/bin/bash\nset -euo pipefail\n"))
        self.assertIn('readonly config_directory="/etc/launchplane"', helper_text)
        self.assertIn('[[ "$directory_mode" == "700" ]]', helper_text)
        self.assertIn("/proc/self/fd/4", helper_text)
        self.assertIn("--files0-from=", helper_text)
        self.assertIn('run_du_measurement "$apparent_file" -s -b -x', helper_text)
        self.assertIn('run_du_measurement "$allocated_file" -s -B1 -x', helper_text)
        self.assertIn("((du_status == 1))", helper_text)
        self.assertIn("measurement_partial=1", helper_text)
        self.assertIn("((record_count <= expected_count))", helper_text)
        self.assertIn("else\n  /usr/bin/printf '%s\\n%s\\n'", helper_text)
        subprocess.run(
            ("/bin/bash", "-n", str(helper_path)),
            capture_output=True,
            text=True,
            check=True,
        )

    def test_generated_run_cache_helper_security_invariants_are_pinned(self) -> None:
        helper_path = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "runner-host-hygiene-generated-run-cache.sh"
        )
        helper_text = helper_path.read_text(encoding="utf-8")

        self.assertEqual(
            _GENERATED_RUN_CACHE_HELPER,
            "/usr/local/sbin/launchplane-generated-run-cache",
        )
        self.assertTrue(helper_path.stat().st_mode & 0o111)
        self.assertTrue(helper_text.startswith("#!/bin/bash\nset -euo pipefail\n"))
        self.assertIn('readonly config_directory="/etc/launchplane"', helper_text)
        self.assertIn("/proc/self/fd/4", helper_text)
        self.assertIn("owner_worker_observations", helper_text)
        self.assertIn("open_handle_observations", helper_text)
        self.assertIn(
            '[[ "$observation_interval_seconds" =~ ^([1-9]|[1-5][0-9]|60)$ ]]',
            helper_text,
        )
        self.assertIn(
            '[[ "$prune_observation_interval_seconds" =~ ^([1-9]|[1-5][0-9]|60)$ ]]',
            helper_text,
        )
        self.assertIn("--one-file-system", helper_text)
        self.assertIn("candidate_low_water < candidate_high_water", helper_text)
        self.assertIn("entry_identity_by_name", helper_text)
        self.assertIn("entry_version_by_name", helper_text)
        self.assertIn("last_attempt_epoch_seconds", helper_text)
        self.assertNotIn('findmnt -rn -R -o TARGET --target "$entry_path" || true', helper_text)
        self.assertNotIn('find "$entry_path" -xdev -type l', helper_text)
        self.assertIn(".launchplane-generated-run-cache-prune.", helper_text)
        self.assertIn("validate_quarantine_directory", helper_text)
        self.assertIn("quarantine_open_handle_count", helper_text)
        self.assertIn("restore_quarantined_entries", helper_text)
        self.assertIn("last_removed_run_ids", helper_text)
        self.assertIn('/usr/bin/sync -f "$state_file"', helper_text)
        subprocess.run(
            ("/bin/bash", "-n", str(helper_path)),
            capture_output=True,
            text=True,
            check=True,
        )

    def test_executor_rejects_noncanonical_or_duplicate_runner_roots(self) -> None:
        with self.assertRaisesRegex(ValueError, "scoped absolute path"):
            _request(
                mutate=False,
                runner_workdir_roots=(RunnerWorkdirRoot(key="legacy", path="/srv/runner roots"),),
            )
        with self.assertRaisesRegex(ValueError, "scoped absolute path"):
            _request(
                mutate=False, runner_workdir_roots=(RunnerWorkdirRoot(key="legacy", path="//"),)
            )
        with self.assertRaisesRegex(ValueError, "must be unique"):
            _request(
                mutate=False,
                runner_workdir_roots=(
                    RunnerWorkdirRoot(key="legacy", path="/srv/runners"),
                    RunnerWorkdirRoot(key="managed", path="/srv/runners/"),
                ),
            )

    def test_report_records_workdir_helper_failure_as_unavailable_without_detail(self) -> None:
        command_runner = _CommandRunner()

        def failing_runner(command: Sequence[str], timeout_seconds: int) -> RemoteCommandResult:
            if tuple(command)[:3] == (
                "/usr/bin/sudo",
                "-n",
                _RUNNER_WORKDIR_USAGE_HELPER,
            ):
                return RemoteCommandResult(
                    returncode=1,
                    stderr="sudo denied legacy=/private/runner/root",
                )
            return command_runner(command, timeout_seconds)

        report = collect_runner_host_hygiene_report(
            request=_request(mutate=False),
            remote_runner=failing_runner,
        )

        self.assertEqual(report.status, "attention")
        self.assertEqual(report.runner_workdir_usage[0].measurement_status, "unavailable")
        self.assertEqual(report.runner_workdir_usage[0].reason_code, "probe_failed")
        telemetry = next(
            item for item in report.cache_class_telemetry if item.cache_class == "runner_workdir"
        )
        self.assertEqual(telemetry.availability, "unavailable")
        self.assertEqual(telemetry.unavailable_reason, "probe_failed")
        self.assertNotIn("/private/runner/root", report.model_dump_json())

    def test_executor_records_failed_terminal_audit_when_post_report_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            command_runner = _CommandRunner()
            helper_calls = 0

            def post_failure_runner(
                command: Sequence[str], timeout_seconds: int
            ) -> RemoteCommandResult:
                nonlocal helper_calls
                if tuple(command)[:3] == (
                    "/usr/bin/sudo",
                    "-n",
                    _RUNNER_WORKDIR_USAGE_HELPER,
                ):
                    helper_calls += 1
                    if helper_calls == 3:
                        return RemoteCommandResult(
                            returncode=1,
                            stderr="sudo denied legacy=/private/runner/root",
                        )
                return command_runner(command, timeout_seconds)

            spool = RunnerHostHygieneAuditSpool(root=Path(temporary_directory) / "spool")
            poster = _AuditPoster()
            request = _request(mutate=True)

            result = execute_runner_host_hygiene_executor(
                request=request,
                remote_runner=post_failure_runner,
                audit_poster=poster,
                audit_spool=spool,
                audit_delivery_sleeper=lambda _seconds: None,
            )

            self.assertEqual(result.status, "failed")
            self.assertIn("terminal evidence collection failed", result.message)
            self.assertNotIn("/private/runner/root", result.message)
            terminal_audit = poster.audits[-1]
            self.assertEqual(terminal_audit.status, "failed")
            self.assertIsNone(terminal_audit.post_apply_report)
            envelope = spool.read(
                host_name=request.host_name,
                action=request.action,
                audit_record_key=request.audit_record_key,
            )
            assert envelope is not None
            self.assertFalse(RunnerHostHygieneAuditSpool.is_unresolved(envelope))

    def test_executor_records_all_approved_runner_roots(self) -> None:
        command_runner = _CommandRunner(
            runner_workdir_usage={
                "/opt/actions-runners": (12_000, 8_000),
                "/home/runner/actions-runners": (7_000, 5_000),
            }
        )
        audit_poster = _AuditPoster()

        result = execute_runner_host_hygiene_executor(
            request=_request(
                mutate=True,
                runner_workdir_roots=(
                    RunnerWorkdirRoot(key="legacy", path="/opt/actions-runners"),
                    RunnerWorkdirRoot(key="managed", path="/home/runner/actions-runners"),
                ),
            ),
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "completed")
        post_apply_report = audit_poster.audits[-1].post_apply_report
        assert post_apply_report is not None
        self.assertEqual(post_apply_report.runner_workdir_bytes, 19_000)
        self.assertEqual(post_apply_report.runner_workdir_allocated_bytes, 13_000)
        self.assertEqual(
            [item.root_key for item in post_apply_report.runner_workdir_usage],
            ["legacy", "managed"],
        )
        self.assertEqual(
            [item.apparent_bytes for item in post_apply_report.runner_workdir_usage],
            [12_000, 7_000],
        )

    def test_executor_flags_orphan_buildkit_artifacts_from_live_evidence(self) -> None:
        command_runner = _CommandRunner(
            container_inventory=_json_lines(
                {
                    "ID": "buildkit-container",
                    "Image": "moby/buildkit:buildx-stable-1",
                    "ImageID": "sha256:buildkit-id",
                    "Names": "buildx_buildkit_old0",
                    "State": "exited",
                    "Status": "Exited (0) 2 days ago",
                }
            ),
            volume_inventory=json.dumps(
                [
                    {
                        "Driver": "local",
                        "Labels": {},
                        "Mountpoint": "/var/lib/docker/volumes/buildx_buildkit_old0_state/_data",
                        "Name": "buildx_buildkit_old0_state",
                        "UsageData": {"RefCount": 0, "Size": 12_000_000_000},
                    }
                ]
            ),
        )
        audit_poster = _AuditPoster()

        result = execute_runner_host_hygiene_executor(
            request=_request(mutate=False),
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "blocked")
        planned_audit = audit_poster.audits[0]
        pre_apply_report = planned_audit.pre_apply_report
        self.assertEqual(pre_apply_report.status, "attention")
        self.assertEqual(pre_apply_report.orphan_buildkit_containers, 1)
        self.assertEqual(pre_apply_report.orphan_buildkit_volumes, 1)
        self.assertIn(
            "orphan_buildkit_present",
            [finding.code for finding in pre_apply_report.findings],
        )

    def test_docker_disk_usage_parser_preserves_volume_usage_data(self) -> None:
        snapshot = _parse_docker_disk_usage(
            _docker_disk_usage_json(
                volumes=[
                    {
                        "Driver": "local",
                        "Labels": {},
                        "Mountpoint": "/var/lib/docker/volumes/runner-cache/_data",
                        "Name": "runner-cache",
                        "UsageData": {"RefCount": 0, "Size": 45_500_000_000},
                    }
                ]
            )
        )

        self.assertEqual(snapshot.volume_inventory[0].name, "runner-cache")
        self.assertEqual(snapshot.volume_inventory[0].size_bytes, 45_500_000_000)
        self.assertEqual(snapshot.reclaimable_breakdown.local_volumes_bytes, 45_500_000_000)

    def test_docker_disk_usage_uses_legacy_api_class_semantics(self) -> None:
        snapshot = _parse_docker_disk_usage(
            _docker_disk_usage_json(
                images=[
                    {"Containers": 0, "SharedSize": 90, "Size": 100},
                    {"Containers": 1, "SharedSize": 90, "Size": 100},
                ],
                build_cache=[
                    {"InUse": False, "Shared": False, "Size": 100},
                    {"InUse": False, "Shared": True, "Size": 200},
                    {"InUse": True, "Shared": False, "Size": 40},
                ],
                layers_size=110,
            )
        )
        measurements = {item.cache_class: item for item in snapshot.cache_class_measurements}

        self.assertEqual(measurements["docker_images"].logical_bytes, 110)
        self.assertEqual(measurements["docker_images"].reclaimable_bytes, 10)
        self.assertEqual(measurements["docker_build_cache"].logical_bytes, 140)
        self.assertEqual(measurements["docker_build_cache"].reclaimable_bytes, 100)

    def test_docker_disk_usage_marks_unknown_values_partial(self) -> None:
        snapshot = _parse_docker_disk_usage(
            json.dumps(
                {
                    "Images": [{"Containers": 0, "SharedSize": -1, "Size": -1}],
                    "LayersSize": 100,
                    "Containers": [{"SizeRw": -1}],
                    "Volumes": [
                        {
                            "Driver": "local",
                            "Labels": {},
                            "Mountpoint": "/private/docker/volume",
                            "Name": "unknown-volume",
                            "UsageData": {"RefCount": -1, "Size": -1},
                        }
                    ],
                    "BuildCache": [{"Size": 100}],
                }
            )
        )
        measurements = {item.cache_class: item for item in snapshot.cache_class_measurements}

        self.assertTrue(all(item.availability == "partial" for item in measurements.values()))
        self.assertIsNone(measurements["docker_images"].reclaimable_bytes)
        self.assertIsNone(measurements["docker_containers"].logical_bytes)
        self.assertIsNone(measurements["docker_volumes"].logical_bytes)
        self.assertIsNone(measurements["docker_build_cache"].reclaimable_bytes)
        self.assertIsNone(snapshot.volume_inventory[0].referenced_by_containers)

    def test_docker_disk_usage_distinguishes_empty_and_unavailable_classes(self) -> None:
        empty_snapshot = _parse_docker_disk_usage(_docker_disk_usage_json())
        empty_measurements = {
            item.cache_class: item for item in empty_snapshot.cache_class_measurements
        }

        self.assertTrue(
            all(item.availability == "available" for item in empty_measurements.values())
        )
        self.assertTrue(all(item.logical_bytes == 0 for item in empty_measurements.values()))
        self.assertTrue(all(item.reclaimable_bytes == 0 for item in empty_measurements.values()))

        missing_snapshot = _parse_docker_disk_usage(json.dumps({"Images": [], "LayersSize": 0}))
        missing_measurements = {
            item.cache_class: item for item in missing_snapshot.cache_class_measurements
        }

        self.assertEqual(missing_measurements["docker_images"].availability, "available")
        for cache_class in (
            "docker_containers",
            "docker_volumes",
            "docker_build_cache",
        ):
            with self.subTest(cache_class=cache_class):
                measurement = missing_measurements[cache_class]
                self.assertEqual(measurement.availability, "unavailable")
                self.assertEqual(measurement.reason_code, "class_unavailable")
                self.assertIsNone(measurement.logical_bytes)
                self.assertIsNone(measurement.reclaimable_bytes)

    def test_executor_fails_closed_when_docker_summary_is_unparseable(self) -> None:
        command_runner = _CommandRunner(docker_disk_usage="not-json")
        audit_poster = _AuditPoster()

        with self.assertRaisesRegex(Exception, "was not valid JSON"):
            execute_runner_host_hygiene_executor(
                request=_request(mutate=True),
                remote_runner=command_runner,
                audit_poster=audit_poster,
            )

        self.assertEqual(audit_poster.records, [])

    def test_report_records_unavailable_docker_summary_without_aborting(self) -> None:
        report = collect_runner_host_hygiene_report(
            request=_request(mutate=False),
            remote_runner=_CommandRunner(docker_disk_usage="not-json"),
        )

        docker_telemetry = tuple(
            item
            for item in report.cache_class_telemetry
            if item.measurement_basis == "docker_engine"
        )
        self.assertEqual(len(docker_telemetry), 4)
        self.assertTrue(all(item.availability == "unavailable" for item in docker_telemetry))
        self.assertTrue(all(item.unavailable_reason == "probe_failed" for item in docker_telemetry))

    def test_report_marks_failed_image_inventory_partial(self) -> None:
        command_runner = _CommandRunner()

        def fail_image_inventory(
            command: Sequence[str], timeout_seconds: int
        ) -> RemoteCommandResult:
            if tuple(command) == (
                "docker",
                "image",
                "ls",
                "--all",
                "--no-trunc",
                "--format",
                "{{json .}}",
            ):
                return RemoteCommandResult(returncode=1, stderr="inventory unavailable")
            return command_runner(command, timeout_seconds)

        report = collect_runner_host_hygiene_report(
            request=_request(mutate=False),
            remote_runner=fail_image_inventory,
        )
        image_telemetry = next(
            item for item in report.cache_class_telemetry if item.cache_class == "docker_images"
        )

        self.assertEqual(image_telemetry.availability, "partial")
        self.assertEqual(image_telemetry.unavailable_reason, "inventory_probe_failed")

    def test_report_merges_partial_image_and_inventory_reasons(self) -> None:
        command_runner = _CommandRunner(
            docker_disk_usage=_docker_disk_usage_json(
                images=[{"Containers": 0, "SharedSize": -1, "Size": 100}],
                layers_size=100,
            )
        )

        def fail_image_inventory(
            command: Sequence[str], timeout_seconds: int
        ) -> RemoteCommandResult:
            if tuple(command) == (
                "docker",
                "image",
                "ls",
                "--all",
                "--no-trunc",
                "--format",
                "{{json .}}",
            ):
                return RemoteCommandResult(returncode=1, stderr="inventory unavailable")
            return command_runner(command, timeout_seconds)

        report = collect_runner_host_hygiene_report(
            request=_request(mutate=False),
            remote_runner=fail_image_inventory,
        )
        image_telemetry = next(
            item for item in report.cache_class_telemetry if item.cache_class == "docker_images"
        )

        self.assertEqual(image_telemetry.availability, "partial")
        self.assertEqual(
            image_telemetry.unavailable_reason,
            "measurement_partial,inventory_probe_failed",
        )

    def test_executor_blocks_before_prune_without_mutate_intent(self) -> None:
        command_runner = _CommandRunner()
        audit_poster = _AuditPoster()

        result = execute_runner_host_hygiene_executor(
            request=_request(mutate=False),
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "blocked")
        self.assertEqual([record[0] for record in audit_poster.records], ["planned"])
        self.assertFalse(
            any(command[:5] == _ENGINE_PRUNE_PREFIX for command in command_runner.commands)
        )

    def test_executor_blocks_prune_when_active_build_processes_are_present(self) -> None:
        command_runner = _CommandRunner(active_build_processes="123 docker buildx build .\n")
        audit_poster = _AuditPoster()

        result = execute_runner_host_hygiene_executor(
            request=_request(mutate=True),
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "blocked")
        self.assertEqual([record[0] for record in audit_poster.records], ["planned"])
        self.assertIn(
            "idle_evidence_active",
            {finding.code for finding in audit_poster.audits[0].pre_apply_report.findings},
        )
        self.assertFalse(
            any(command[:5] == _ENGINE_PRUNE_PREFIX for command in command_runner.commands)
        )

    def test_executor_blocks_when_runner_worker_is_active(self) -> None:
        command_runner = _CommandRunner(
            active_build_processes="456 /opt/actions-runner/bin/Runner.Worker spawnclient 123\n"
        )
        audit_poster = _AuditPoster()

        result = execute_runner_host_hygiene_executor(
            request=_request(mutate=True),
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "blocked")
        self.assertIn(
            "idle_evidence_active",
            {finding.code for finding in audit_poster.audits[0].pre_apply_report.findings},
        )
        self.assertFalse(
            any(command[:5] == _ENGINE_PRUNE_PREFIX for command in command_runner.commands)
        )

    def test_executor_excludes_current_job_runner_worker_ancestor(self) -> None:
        command_runner = _CommandRunner(
            active_build_processes=(
                "ancestor_pids=456 123 1\n"
                "456 /opt/actions-runner/bin/Runner.Worker spawnclient 123\n"
            )
        )

        result = execute_runner_host_hygiene_executor(
            request=_request(mutate=True),
            remote_runner=command_runner,
            audit_poster=_AuditPoster(),
            audit_delivery_sleeper=lambda _seconds: None,
        )

        self.assertEqual(result.status, "completed")

    def test_executor_blocks_other_runner_worker_beside_current_job(self) -> None:
        command_runner = _CommandRunner(
            active_build_processes=(
                "ancestor_pids=456 123 1\n"
                "456 /opt/actions-runner/bin/Runner.Worker spawnclient 123\n"
                "789 /opt/actions-runner/bin/Runner.Worker spawnclient 456\n"
            )
        )

        result = execute_runner_host_hygiene_executor(
            request=_request(mutate=True),
            remote_runner=command_runner,
            audit_poster=_AuditPoster(),
            audit_delivery_sleeper=lambda _seconds: None,
        )

        self.assertEqual(result.status, "blocked")

    def test_executor_requires_two_consecutive_idle_samples(self) -> None:
        command_runner = _CommandRunner()

        result = execute_runner_host_hygiene_executor(
            request=_request(mutate=True),
            remote_runner=command_runner,
            audit_poster=_AuditPoster(),
            audit_delivery_sleeper=lambda _seconds: None,
        )

        self.assertEqual(result.status, "completed")
        idle_commands = [
            command
            for command in command_runner.commands
            if command[:2] == ("bash", "-lc") and "pgrep -af" in command[2]
        ]
        self.assertEqual(len(idle_commands), 6)

    def test_executor_spools_terminal_audit_before_failed_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            spool = RunnerHostHygieneAuditSpool(
                root=temporary_path / "spool",
                artifact_file=temporary_path / "artifact.json",
            )
            current_service_unit = "launchplane-runner@ops-gate.service"
            sibling_runner_name = "sibling-runner"
            sibling_service_unit = "launchplane-runner@secondary.service"
            request = _request(
                mutate=True,
                github_idle_bindings=(
                    RunnerHostGitHubBinding(
                        repository_scope="cbusillo/launchplane",
                        execution_lane="chris-testing-ops-gate",
                        runner_name="test-runner",
                        service_unit=current_service_unit,
                    ),
                    RunnerHostGitHubBinding(
                        repository_scope="cbusillo/launchplane",
                        execution_lane="chris-testing-ops-gate",
                        runner_name=sibling_runner_name,
                        service_unit=sibling_service_unit,
                    ),
                ),
            )

            result = execute_runner_host_hygiene_executor(
                request=request,
                remote_runner=_CommandRunner(
                    runner_service_output=(
                        f"{current_service_unit} active running\n"
                        f"{sibling_service_unit} active running\n"
                    )
                ),
                audit_poster=_TerminalFailingAuditPoster(),
                audit_spool=spool,
                audit_delivery_sleeper=lambda _seconds: None,
            )

            self.assertEqual(result.status, "audit_delivery_pending")
            self.assertTrue(result.audit_delivery_pending)
            envelope = spool.read(
                host_name=request.host_name,
                action=request.action,
                audit_record_key=request.audit_record_key,
            )
            assert envelope is not None
            self.assertEqual(envelope.execution_state, "terminal_recorded")
            self.assertEqual(envelope.planned_delivery_state, "delivered")
            self.assertEqual(envelope.terminal_delivery_state, "pending")
            terminal_audit = envelope.terminal_audit
            assert terminal_audit is not None
            self.assertEqual(terminal_audit.status, "completed")
            artifact_text = (temporary_path / "artifact.json").read_text(encoding="utf-8")
            spool_paths = tuple((temporary_path / "spool").rglob("*.json"))
            self.assertEqual(len(spool_paths), 1)
            spool_text = spool_paths[0].read_text(encoding="utf-8")
            for persisted_text in (artifact_text, spool_text):
                self.assertNotIn("Bearer", persisted_text)
                self.assertNotIn("temporary-token", persisted_text)
                self.assertNotIn(request.execution_lane, persisted_text)
                self.assertNotIn(request.service_user, persisted_text)
                self.assertNotIn(request.repository_scope, persisted_text)
                self.assertNotIn(request.current_runner_name, persisted_text)
                self.assertNotIn(current_service_unit, persisted_text)
                self.assertNotIn(sibling_runner_name, persisted_text)
                self.assertNotIn(sibling_service_unit, persisted_text)

            reconciliation_runner = _CommandRunner()
            reconciled = execute_runner_host_hygiene_executor(
                request=request,
                remote_runner=reconciliation_runner,
                audit_poster=_AuditPoster(),
                audit_spool=spool,
                audit_delivery_sleeper=lambda _seconds: None,
            )

            self.assertEqual(reconciled.status, "completed")
            self.assertTrue(reconciled.reconciled)
            self.assertEqual(reconciliation_runner.commands, [])

    def test_executor_persists_action_started_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool = RunnerHostHygieneAuditSpool(root=Path(temporary_directory) / "spool")
            request = _request(mutate=True)

            result = execute_runner_host_hygiene_executor(
                request=request,
                remote_runner=_SpoolAwareCommandRunner(spool=spool, request=request),
                audit_poster=_AuditPoster(),
                audit_spool=spool,
                audit_delivery_sleeper=lambda _seconds: None,
            )

            self.assertEqual(result.status, "completed")
            envelope = spool.read(
                host_name=request.host_name,
                action=request.action,
                audit_record_key=request.audit_record_key,
            )
            assert envelope is not None
            self.assertEqual(envelope.execution_state, "terminal_recorded")
            self.assertEqual(envelope.terminal_delivery_state, "delivered")

    def test_executor_records_fresh_pre_action_block_without_mutation(self) -> None:
        class _ActivityAppearsCommandRunner(_CommandRunner):
            def __init__(self) -> None:
                super().__init__()
                self._idle_probe_count = 0

            def __call__(self, command: Sequence[str], timeout_seconds: int) -> RemoteCommandResult:
                command_tuple = tuple(command)
                if command_tuple[:2] == ("bash", "-lc") and "pgrep -af" in command_tuple[2]:
                    self.commands.append(command_tuple)
                    self._idle_probe_count += 1
                    return RemoteCommandResult(
                        returncode=0,
                        stdout=(
                            ""
                            if self._idle_probe_count <= 2
                            else "789 /opt/actions-runner/bin/Runner.Worker spawnclient 456\n"
                        ),
                    )
                return super().__call__(command_tuple, timeout_seconds)

        with tempfile.TemporaryDirectory() as temporary_directory:
            spool = RunnerHostHygieneAuditSpool(root=Path(temporary_directory) / "spool")
            request = _request(mutate=True)
            command_runner = _ActivityAppearsCommandRunner()

            result = execute_runner_host_hygiene_executor(
                request=request,
                remote_runner=command_runner,
                audit_poster=_AuditPoster(),
                audit_spool=spool,
                audit_delivery_sleeper=lambda _seconds: None,
            )

            self.assertEqual(result.status, "failed")
            self.assertFalse(
                any(command[:5] == _ENGINE_PRUNE_PREFIX for command in command_runner.commands)
            )
            envelope = spool.read(
                host_name=request.host_name,
                action=request.action,
                audit_record_key=request.audit_record_key,
            )
            assert envelope is not None
            self.assertEqual(envelope.execution_state, "terminal_recorded")
            terminal_audit = envelope.terminal_audit
            assert terminal_audit is not None
            self.assertEqual(terminal_audit.status, "failed")
            self.assertEqual(terminal_audit.plan.status, "blocked")

            reconciled = execute_runner_host_hygiene_executor(
                request=request,
                remote_runner=_CommandRunner(),
                audit_poster=_AuditPoster(),
                audit_spool=spool,
                audit_delivery_sleeper=lambda _seconds: None,
            )

            self.assertEqual(reconciled.status, "failed")
            self.assertTrue(reconciled.reconciled)

    def test_executor_retries_terminal_audit_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool = RunnerHostHygieneAuditSpool(root=Path(temporary_directory) / "spool")
            request = _request(mutate=True)
            poster = _FlakyTerminalAuditPoster(failures=2)

            result = execute_runner_host_hygiene_executor(
                request=request,
                remote_runner=_CommandRunner(),
                audit_poster=poster,
                audit_spool=spool,
                audit_delivery_sleeper=lambda _seconds: None,
            )

            self.assertEqual(result.status, "completed")
            self.assertFalse(result.audit_delivery_pending)
            envelope = spool.read(
                host_name=request.host_name,
                action=request.action,
                audit_record_key=request.audit_record_key,
            )
            assert envelope is not None
            self.assertEqual(envelope.terminal_delivery_state, "delivered")
            self.assertEqual(envelope.delivery_attempts, 4)

    def test_executor_blocks_new_action_while_terminal_delivery_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool = RunnerHostHygieneAuditSpool(root=Path(temporary_directory) / "spool")
            first_request = _request(mutate=True)
            execute_runner_host_hygiene_executor(
                request=first_request,
                remote_runner=_CommandRunner(),
                audit_poster=_TerminalFailingAuditPoster(),
                audit_spool=spool,
                audit_delivery_sleeper=lambda _seconds: None,
            )
            second_runner = _CommandRunner()

            blocked = execute_runner_host_hygiene_executor(
                request=_request(
                    mutate=True,
                    audit_record_key="runner-host-hygiene/2026-05-24/chris-testing",
                ),
                remote_runner=second_runner,
                audit_poster=_TerminalFailingAuditPoster(),
                audit_spool=spool,
                audit_delivery_sleeper=lambda _seconds: None,
            )

            self.assertEqual(blocked.status, "blocked")
            self.assertTrue(blocked.audit_delivery_pending)
            self.assertEqual(second_runner.commands, [])

    def test_executor_blocks_permanently_rejected_planned_audit_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool = RunnerHostHygieneAuditSpool(root=Path(temporary_directory) / "spool")
            command_runner = _CommandRunner()

            result = execute_runner_host_hygiene_executor(
                request=_request(mutate=True),
                remote_runner=command_runner,
                audit_poster=_PermanentlyFailingAuditPoster(),
                audit_spool=spool,
                audit_delivery_sleeper=lambda _seconds: None,
            )

            self.assertEqual(result.status, "blocked")
            self.assertTrue(result.audit_delivery_pending)
            self.assertFalse(
                any(
                    command[:5]
                    == (
                        "flock",
                        "-n",
                        "/tmp/launchplane-runner-host-hygiene.lock",
                        "docker",
                        "builder",
                    )
                    for command in command_runner.commands
                )
            )

    def test_executor_reports_pending_planned_delivery_for_blocked_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool = RunnerHostHygieneAuditSpool(root=Path(temporary_directory) / "spool")

            result = execute_runner_host_hygiene_executor(
                request=_request(mutate=False),
                remote_runner=_CommandRunner(),
                audit_poster=_PermanentlyFailingAuditPoster(),
                audit_spool=spool,
                audit_delivery_sleeper=lambda _seconds: None,
            )

            self.assertEqual(result.status, "blocked")
            self.assertTrue(result.audit_delivery_pending)
            self.assertIn("planned audit delivery remains pending", result.message)

    def test_executor_resolves_action_started_without_repeating_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool = RunnerHostHygieneAuditSpool(root=Path(temporary_directory) / "spool")
            request = _request(mutate=True)
            crashing_runner = _CrashingCommandRunner()
            disk_observations = iter((900, 800))

            def varying_evidence_runner(
                command: Sequence[str], timeout_seconds: int
            ) -> RemoteCommandResult:
                if tuple(command) == ("df", "-B1", "-P", "/"):
                    available_bytes = next(disk_observations)
                    return RemoteCommandResult(
                        returncode=0,
                        stdout=(
                            "Filesystem 1B-blocks Used Available Use% Mounted on\n"
                            f"/dev/disk 1000 100 {available_bytes} 10% /\n"
                        ),
                    )
                return crashing_runner(command, timeout_seconds)

            with self.assertRaisesRegex(RuntimeError, "simulated runner termination"):
                execute_runner_host_hygiene_executor(
                    request=request,
                    remote_runner=varying_evidence_runner,
                    audit_poster=_AuditPoster(),
                    audit_spool=spool,
                    audit_delivery_sleeper=lambda _seconds: None,
                )
            envelope = spool.read(
                host_name=request.host_name,
                action=request.action,
                audit_record_key=request.audit_record_key,
            )
            assert envelope is not None
            self.assertEqual(envelope.execution_state, "action_started")
            self.assertIsNotNone(envelope.action_authorization)
            assert envelope.action_authorization is not None
            self.assertEqual(
                envelope.planned_audit.pre_apply_report.free_disk_bytes,
                900,
            )
            self.assertEqual(
                envelope.action_authorization.pre_apply_report.free_disk_bytes,
                800,
            )
            resolution_runner = _CommandRunner()
            resolution_poster = _AuditPoster()

            resolved = execute_runner_host_hygiene_executor(
                request=_request(mutate=True, resolve_action_started=True),
                remote_runner=resolution_runner,
                audit_poster=resolution_poster,
                audit_spool=spool,
                audit_delivery_sleeper=lambda _seconds: None,
            )

            self.assertEqual(resolved.status, "failed")
            self.assertTrue(resolved.reconciled)
            self.assertEqual(
                resolution_poster.audits[-1].pre_apply_report.free_disk_bytes,
                800,
            )
            self.assertFalse(
                any(
                    command[:5]
                    == (
                        "flock",
                        "-n",
                        "/tmp/launchplane-runner-host-hygiene.lock",
                        "docker",
                        "builder",
                    )
                    for command in resolution_runner.commands
                )
            )

    def test_executor_terminalizes_interrupted_planned_state_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool = RunnerHostHygieneAuditSpool(root=Path(temporary_directory) / "spool")
            request = _request(mutate=True)
            with self.assertRaisesRegex(RuntimeError, "pre-action termination"):
                execute_runner_host_hygiene_executor(
                    request=request,
                    remote_runner=_PreActionCrashingCommandRunner(),
                    audit_poster=_AuditPoster(),
                    audit_spool=spool,
                )
            envelope = spool.read(
                host_name=request.host_name,
                action=request.action,
                audit_record_key=request.audit_record_key,
            )
            assert envelope is not None
            self.assertEqual(envelope.execution_state, "planned")
            reconciliation_runner = _CommandRunner()

            result = execute_runner_host_hygiene_executor(
                request=request,
                remote_runner=reconciliation_runner,
                audit_poster=_AuditPoster(),
                audit_spool=spool,
            )

            self.assertEqual(result.status, "failed")
            self.assertTrue(result.reconciled)
            self.assertEqual(reconciliation_runner.commands, [])

    def test_action_started_recovery_rejects_changed_executor_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool = RunnerHostHygieneAuditSpool(root=Path(temporary_directory) / "spool")
            request = _request(mutate=True)
            with self.assertRaisesRegex(RuntimeError, "simulated runner termination"):
                execute_runner_host_hygiene_executor(
                    request=request,
                    remote_runner=_CrashingCommandRunner(),
                    audit_poster=_AuditPoster(),
                    audit_spool=spool,
                )
            resolution_runner = _CommandRunner()

            result = execute_runner_host_hygiene_executor(
                request=_request(
                    mutate=True,
                    resolve_action_started=True,
                    idle_observation_interval_seconds=2,
                ),
                remote_runner=resolution_runner,
                audit_poster=_AuditPoster(),
                audit_spool=spool,
            )

            self.assertEqual(result.status, "blocked")
            self.assertIn("do not match", result.message)
            self.assertEqual(resolution_runner.commands, [])

    def test_executor_redacts_action_failure_before_audit_and_artifact(self) -> None:
        secret = "ghp_aaaaaaaaaaaa"
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            spool = RunnerHostHygieneAuditSpool(
                root=temporary_path / "spool",
                artifact_file=temporary_path / "artifact.json",
            )
            poster = _AuditPoster()

            result = execute_runner_host_hygiene_executor(
                request=_request(mutate=True),
                remote_runner=_CommandRunner(
                    prune_returncode=1,
                    prune_stderr=f"token={secret} Bearer raw-jwt-value",
                ),
                audit_poster=poster,
                audit_spool=spool,
                audit_delivery_sleeper=lambda _seconds: None,
            )

            self.assertEqual(result.status, "failed")
            self.assertNotIn(secret, result.message)
            self.assertNotIn("raw-jwt-value", result.message)
            terminal_audit = poster.audits[-1]
            self.assertNotIn(secret, terminal_audit.message)
            artifact_text = (temporary_path / "artifact.json").read_text(encoding="utf-8")
            self.assertNotIn(secret, artifact_text)
            self.assertNotIn("raw-jwt-value", artifact_text)

    def test_executor_removes_allowlisted_zero_link_buildkit_state_volume(
        self,
    ) -> None:
        target_volume = "buildx_buildkit_launchplane-ci0_state"
        _summary_row, inventory_row = _buildkit_volume_evidence(target_volume)
        command_runner = _CommandRunner(
            volume_inventory=json.dumps([inventory_row]),
        )
        audit_poster = _AuditPoster()

        result = execute_runner_host_hygiene_executor(
            request=_request(
                mutate=True,
                action="remove_buildkit_state_volumes",
                target_buildkit_state_volumes=(target_volume,),
                allowed_buildkit_state_volumes=(target_volume,),
            ),
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(command_runner.removed_volumes, [target_volume])
        self.assertIn(
            (
                "flock",
                "-n",
                "/tmp/launchplane-runner-host-hygiene.lock",
                "docker",
                "volume",
                "rm",
                target_volume,
            ),
            command_runner.commands,
        )
        terminal_audit = audit_poster.audits[-1]
        post_apply_report = terminal_audit.post_apply_report
        self.assertIsNotNone(post_apply_report)
        assert post_apply_report is not None
        self.assertNotIn(
            target_volume,
            [volume.name for volume in post_apply_report.volume_inventory],
        )

    def test_executor_blocks_multiple_buildkit_state_volume_targets_without_mutating(
        self,
    ) -> None:
        first_volume = "buildx_buildkit_launchplane-ci0_state"
        second_volume = "buildx_buildkit_verireel-ci0_state"
        _first_summary_row, first_inventory_row = _buildkit_volume_evidence(first_volume)
        _second_summary_row, second_inventory_row = _buildkit_volume_evidence(
            second_volume,
            size_bytes=12_480_000_000,
        )
        command_runner = _CommandRunner(
            volume_remove_partial_failure=True,
            volume_inventory=json.dumps([first_inventory_row, second_inventory_row]),
        )
        audit_poster = _AuditPoster()

        result = execute_runner_host_hygiene_executor(
            request=_request(
                mutate=True,
                action="remove_buildkit_state_volumes",
                target_buildkit_state_volumes=(first_volume, second_volume),
                allowed_buildkit_state_volumes=(first_volume, second_volume),
            ),
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(command_runner.removed_volumes, [])
        self.assertFalse(
            any(command[:6] == _VOLUME_RM_PREFIX for command in command_runner.commands)
        )
        planned_audit = audit_poster.audits[0]
        self.assertIn(
            "target_volume_multiple_requested",
            [blocker.code for blocker in planned_audit.plan.blockers],
        )

    def test_executor_blocks_active_buildkit_state_volume_removal(self) -> None:
        target_volume = "buildx_buildkit_odoo-docker-chris-testing0_state"
        command_runner = _CommandRunner(
            volume_inventory=json.dumps(
                [
                    {
                        "Driver": "local",
                        "Labels": {},
                        "Mountpoint": f"/var/lib/docker/volumes/{target_volume}/_data",
                        "Name": target_volume,
                        "UsageData": {"RefCount": 1, "Size": 32_480_000_000},
                    }
                ]
            ),
        )
        audit_poster = _AuditPoster()

        result = execute_runner_host_hygiene_executor(
            request=_request(
                mutate=True,
                action="remove_buildkit_state_volumes",
                target_buildkit_state_volumes=(target_volume,),
                allowed_buildkit_state_volumes=(target_volume,),
            ),
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(command_runner.removed_volumes, [])
        planned_audit = audit_poster.audits[0]
        self.assertIn(
            "target_volume_active",
            [blocker.code for blocker in planned_audit.plan.blockers],
        )

    def test_executor_blocks_target_volume_without_independent_allowlist(
        self,
    ) -> None:
        target_volume = "buildx_buildkit_launchplane-ci0_state"
        _summary_row, inventory_row = _buildkit_volume_evidence(target_volume)
        command_runner = _CommandRunner(
            volume_inventory=json.dumps([inventory_row]),
        )
        audit_poster = _AuditPoster()

        result = execute_runner_host_hygiene_executor(
            request=_request(
                mutate=True,
                action="remove_buildkit_state_volumes",
                target_buildkit_state_volumes=(target_volume,),
            ),
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(command_runner.removed_volumes, [])
        planned_audit = audit_poster.audits[0]
        self.assertEqual(
            [blocker.code for blocker in planned_audit.plan.blockers],
            ["target_volume_not_allowlisted"],
        )

    def test_executor_posts_failed_when_warm_builder_missing_after_prune(self) -> None:
        command_runner = _CommandRunner(image_present_after=False)
        audit_poster = _AuditPoster()

        result = execute_runner_host_hygiene_executor(
            request=_request(mutate=True),
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "failed")
        self.assertIn(
            ("failed", "runner-host-hygiene:runner-host-hygiene/2026-05-23/chris-testing:failed"),
            audit_poster.records,
        )
        self.assertIn("post evidence failed", result.message)

    def test_executor_environment_requires_runner_user_to_match_service_user(
        self,
    ) -> None:
        with self.assertRaisesRegex(Exception, "must match the approved service user"):
            validate_local_executor_environment(
                request=_request(mutate=True),
                current_user="root",
                current_host="chris-testing",
            )

    def test_executor_environment_requires_host_to_match_approved_host(self) -> None:
        with self.assertRaisesRegex(Exception, "host must match the approved host"):
            validate_local_executor_environment(
                request=_request(mutate=True),
                current_user="launchplane-runner-hygiene",
                current_host="other-host",
            )

    def test_executor_environment_requires_repository_scope_to_match_github_repository(
        self,
    ) -> None:
        with self.assertRaisesRegex(Exception, "must match GITHUB_REPOSITORY"):
            validate_local_executor_environment(
                request=_request(mutate=True),
                env={"GITHUB_REPOSITORY": "cbusillo/other"},
                current_user="launchplane-runner-hygiene",
                current_host="chris-testing",
            )

    @staticmethod
    def test_executor_environment_honors_explicit_empty_env_mapping() -> None:
        validate_local_executor_environment(
            request=_request(mutate=True),
            env={},
            current_user="launchplane-runner-hygiene",
            current_host="chris-testing.example.test",
        )

    def test_local_command_runner_returns_structured_timeout_result(self) -> None:
        from control_plane.workflows.runner_host_hygiene_executor import build_local_command_runner

        with patch(
            "control_plane.workflows.runner_host_hygiene_executor.subprocess.run",
            side_effect=subprocess.TimeoutExpired(
                cmd=("docker", "system", "df"),
                timeout=1,
                output=b"partial stdout",
                stderr=b"partial stderr",
            ),
        ):
            result = build_local_command_runner()(("docker", "system", "df"), 1)

        self.assertEqual(result.returncode, 124)
        self.assertEqual(result.stdout, "partial stdout")
        self.assertEqual(result.stderr, "partial stderr")

    def test_service_audit_poster_refreshes_token_per_post(self) -> None:
        class _Response:
            def __init__(self, payload: Mapping[str, object]) -> None:
                self._payload = payload

            def __enter__(self) -> "_Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(self._payload).encode()

        observed_authorization_headers: list[str | None] = []
        tokens = iter(("first-token", "second-token"))
        audit = _planned_audit()
        response_payload = {
            "status": "accepted",
            "records": {
                "runner_host_hygiene_audit_record_key": audit.audit_record_key,
            },
            "result": {
                "runner_host_hygiene_audit_record_key": audit.audit_record_key,
                "audit_status": audit.status,
                "mutate": audit.request.mutate,
            },
        }

        def fake_urlopen(request: Request, timeout: int) -> _Response:
            self.assertEqual(timeout, 30)
            observed_authorization_headers.append(request.get_header("Authorization"))
            return _Response(response_payload)

        poster = build_refreshing_service_audit_poster(
            service_url="https://launchplane.example",
            bearer_token_provider=lambda: next(tokens),
        )
        with patch(
            "control_plane.workflows.runner_host_hygiene_executor.urlopen",
            side_effect=fake_urlopen,
        ):
            poster(audit, "idempotency-key-1")
            poster(audit, "idempotency-key-2")

        self.assertEqual(
            observed_authorization_headers,
            ["Bearer first-token", "Bearer second-token"],
        )

    def test_service_audit_poster_rejects_mismatched_acceptance(self) -> None:
        class _Response:
            def __enter__(self) -> "_Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            @staticmethod
            def read() -> bytes:
                return b'{"status":"accepted","records":{},"result":{}}'

        poster = build_refreshing_service_audit_poster(
            service_url="https://launchplane.example",
            bearer_token_provider=lambda: "token",
        )
        with (
            patch(
                "control_plane.workflows.runner_host_hygiene_executor.urlopen",
                return_value=_Response(),
            ),
            self.assertRaisesRegex(AuditDeliveryError, "mismatched audit acceptance"),
        ):
            poster(_planned_audit(), "idempotency-key")

    def test_github_run_state_reader_supports_public_repository_without_token(self) -> None:
        class _Response:
            def __enter__(self) -> "_Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            @staticmethod
            def read() -> bytes:
                return b'{"status":"completed"}'

        observed_authorization: list[str | None] = []

        def fake_urlopen(request: Request, timeout: int) -> _Response:
            self.assertEqual(timeout, 30)
            observed_authorization.append(request.get_header("Authorization"))
            self.assertIn("/repos/cbusillo/codex-lab/actions/runs/101", request.full_url)
            return _Response()

        reader = build_github_run_state_reader(bearer_token="")
        with patch(
            "control_plane.workflows.runner_host_hygiene_executor.urlopen",
            side_effect=fake_urlopen,
        ):
            states = reader("cbusillo/codex-lab", (101,))

        self.assertEqual(states, {101: "completed"})
        self.assertEqual(observed_authorization, [None])

    def test_github_run_state_reader_preserves_unauthorized_response(self) -> None:
        reader = build_github_run_state_reader(bearer_token="token")

        with patch(
            "control_plane.workflows.runner_host_hygiene_executor.urlopen",
            side_effect=HTTPError(
                url="https://api.github.test/run",
                code=403,
                msg="forbidden",
                hdrs=Message(),
                fp=BytesIO(b"forbidden"),
            ),
        ):
            states = reader("cbusillo/codex-lab", (101,))

        self.assertEqual(states, {101: "unauthorized"})

    def test_github_idle_reader_attributes_job_and_runner_activity(self) -> None:
        responses = {
            "status=queued": {
                "total_count": 1,
                "workflow_runs": [{"id": 101, "status": "queued"}],
            },
            "status=in_progress": {
                "total_count": 2,
                "workflow_runs": [
                    {"id": 102, "status": "in_progress"},
                    {"id": 999, "status": "in_progress"},
                ],
            },
            "status=waiting": {
                "total_count": 1,
                "workflow_runs": [{"id": 103, "status": "waiting"}],
            },
            "status=pending": {"total_count": 0, "workflow_runs": []},
            "status=requested": {"total_count": 0, "workflow_runs": []},
            "/actions/runs/101/jobs": {
                "total_count": 1,
                "jobs": [
                    {
                        "status": "queued",
                        "labels": ["self-hosted", "ops-gate"],
                    }
                ],
            },
            "/actions/runs/102/jobs": {
                "total_count": 1,
                "jobs": [
                    {
                        "status": "in_progress",
                        "labels": ["self-hosted", "other-lane"],
                    }
                ],
            },
            "/actions/runs/103/jobs": {
                "total_count": 1,
                "jobs": [
                    {
                        "status": "waiting",
                        "labels": ["self-hosted", "ops-gate"],
                    }
                ],
            },
            "/actions/runs/999/jobs": {
                "total_count": 1,
                "jobs": [
                    {
                        "status": "in_progress",
                        "labels": ["self-hosted", "ops-gate"],
                        "runner_name": "current-runner",
                    }
                ],
            },
            "/actions/runners": {
                "total_count": 2,
                "runners": [
                    {
                        "name": "current-runner",
                        "status": "online",
                        "busy": True,
                        "labels": [{"name": "ops-gate"}],
                    },
                    {
                        "name": "peer-runner",
                        "status": "online",
                        "busy": False,
                        "labels": [{"name": "ops-gate"}],
                    },
                ],
            },
        }

        class _Response:
            def __init__(self, payload: Mapping[str, object]) -> None:
                self._payload = payload

            def __enter__(self) -> "_Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(self._payload).encode()

        def fake_urlopen(request: Request, timeout: int) -> _Response:
            self.assertEqual(timeout, 30)
            self.assertEqual(request.get_header("Authorization"), "Bearer token")
            for fragment, payload in responses.items():
                if fragment in request.full_url:
                    return _Response(payload)
            raise AssertionError(f"unexpected GitHub request: {request.full_url}")

        reader = build_github_idle_evidence_reader(
            bearer_token="token",
            current_run_id=999,
            current_runner_name="current-runner",
        )
        with patch(
            "control_plane.workflows.runner_host_hygiene_executor.urlopen",
            side_effect=fake_urlopen,
        ):
            observations = reader(
                "cbusillo/launchplane",
                "ops-gate",
                "current-runner",
                True,
            )

        by_source = {observation.source: observation for observation in observations}
        self.assertEqual(by_source["github_jobs"].state, "active")
        self.assertEqual(by_source["github_jobs"].active_count, 2)
        self.assertEqual(by_source["github_runners"].state, "idle")
        self.assertEqual(by_source["github_runners"].active_count, 0)
        self.assertNotEqual(by_source["github_jobs"].subject_key, "ops-gate")
        self.assertTrue(by_source["github_jobs"].subject_key.startswith("runner-"))

    def test_github_idle_reader_excludes_only_current_runner_for_sibling_binding(self) -> None:
        responses = {
            "status=queued": {"total_count": 0, "workflow_runs": []},
            "status=in_progress": {
                "total_count": 1,
                "workflow_runs": [{"id": 999, "status": "in_progress"}],
            },
            "status=waiting": {"total_count": 0, "workflow_runs": []},
            "status=pending": {"total_count": 0, "workflow_runs": []},
            "status=requested": {"total_count": 0, "workflow_runs": []},
            "/actions/runs/999/jobs": {
                "total_count": 2,
                "jobs": [
                    {
                        "status": "in_progress",
                        "labels": ["self-hosted", "ops-gate"],
                        "runner_name": "current-runner",
                    },
                    {
                        "status": "in_progress",
                        "labels": ["self-hosted", "ops-gate"],
                        "runner_name": "sibling-runner",
                    },
                ],
            },
            "/actions/runners": {
                "total_count": 2,
                "runners": [
                    {
                        "name": "current-runner",
                        "status": "online",
                        "busy": True,
                        "labels": [{"name": "ops-gate"}],
                    },
                    {
                        "name": "sibling-runner",
                        "status": "online",
                        "busy": True,
                        "labels": [{"name": "ops-gate"}],
                    },
                ],
            },
        }

        class _Response:
            def __init__(self, payload: Mapping[str, object]) -> None:
                self._payload = payload

            def __enter__(self) -> "_Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(self._payload).encode()

        def fake_urlopen(request: Request, timeout: int) -> _Response:
            self.assertEqual(timeout, 30)
            for fragment, payload in responses.items():
                if fragment in request.full_url:
                    return _Response(payload)
            raise AssertionError(f"unexpected GitHub request: {request.full_url}")

        reader = build_github_idle_evidence_reader(
            bearer_token="token",
            current_run_id=999,
            current_runner_name="current-runner",
        )
        with patch(
            "control_plane.workflows.runner_host_hygiene_executor.urlopen",
            side_effect=fake_urlopen,
        ):
            current = reader(
                "cbusillo/launchplane",
                "ops-gate",
                "current-runner",
                True,
            )
            sibling = reader(
                "cbusillo/launchplane",
                "ops-gate",
                "sibling-runner",
                True,
            )

        current_by_source = {item.source: item for item in current}
        sibling_by_source = {item.source: item for item in sibling}
        self.assertEqual(current_by_source["github_jobs"].state, "active")
        self.assertEqual(current_by_source["github_jobs"].active_count, 1)
        self.assertEqual(current_by_source["github_runners"].state, "idle")
        self.assertEqual(sibling_by_source["github_jobs"].state, "active")
        self.assertEqual(sibling_by_source["github_jobs"].active_count, 1)
        self.assertEqual(sibling_by_source["github_runners"].state, "active")

    def test_github_idle_reader_preserves_unauthorized_runner_source(self) -> None:
        class _Response:
            def __init__(self, payload: Mapping[str, object]) -> None:
                self._payload = payload

            def __enter__(self) -> "_Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(self._payload).encode()

        def fake_urlopen(request: Request, timeout: int) -> _Response:
            self.assertEqual(timeout, 30)
            if "/actions/runners" in request.full_url:
                raise HTTPError(
                    url=request.full_url,
                    code=403,
                    msg="forbidden",
                    hdrs=Message(),
                    fp=BytesIO(b"forbidden"),
                )
            if "/actions/runs/999/jobs" in request.full_url:
                return _Response(
                    {
                        "total_count": 1,
                        "jobs": [
                            {
                                "status": "in_progress",
                                "labels": ["self-hosted", "ops-gate"],
                                "runner_name": "current-runner",
                            }
                        ],
                    }
                )
            return _Response(
                {
                    "total_count": 1 if "status=in_progress" in request.full_url else 0,
                    "workflow_runs": (
                        [{"id": 999, "status": "in_progress"}]
                        if "status=in_progress" in request.full_url
                        else []
                    ),
                }
            )

        reader = build_github_idle_evidence_reader(
            bearer_token="token",
            current_run_id=999,
            current_runner_name="current-runner",
        )
        with patch(
            "control_plane.workflows.runner_host_hygiene_executor.urlopen",
            side_effect=fake_urlopen,
        ):
            observations = reader(
                "cbusillo/launchplane",
                "ops-gate",
                "current-runner",
                True,
            )

        by_source = {observation.source: observation for observation in observations}
        self.assertEqual(by_source["github_jobs"].state, "idle")
        self.assertEqual(by_source["github_runners"].availability, "unavailable")
        self.assertEqual(by_source["github_runners"].reason_code, "source_unauthorized")

    def test_github_idle_reader_fails_closed_without_current_job_identity(self) -> None:
        reader = build_github_idle_evidence_reader(bearer_token="token")

        observations = reader("cbusillo/launchplane", "ops-gate", "", True)

        self.assertEqual(
            {observation.reason_code for observation in observations},
            {"source_missing"},
        )

    def test_github_idle_reader_fails_closed_on_missing_or_malformed_current_job(self) -> None:
        def read_jobs_observation(
            *,
            run_status: str,
            jobs: list[dict[str, object]],
        ) -> RunnerHostHygieneIdleObservation:
            responses = {
                "status=queued": {"total_count": 0, "workflow_runs": []},
                "status=in_progress": {
                    "total_count": 1,
                    "workflow_runs": [{"id": 999, "status": run_status}],
                },
                "status=waiting": {"total_count": 0, "workflow_runs": []},
                "status=pending": {"total_count": 0, "workflow_runs": []},
                "status=requested": {"total_count": 0, "workflow_runs": []},
                "/actions/runs/999/jobs": {
                    "total_count": len(jobs),
                    "jobs": jobs,
                },
                "/actions/runners": {
                    "total_count": 1,
                    "runners": [
                        {
                            "name": "current-runner",
                            "status": "online",
                            "busy": True,
                            "labels": [{"name": "ops-gate"}],
                        }
                    ],
                },
            }

            class _Response:
                def __init__(self, payload: Mapping[str, object]) -> None:
                    self._payload = payload

                def __enter__(self) -> "_Response":
                    return self

                def __exit__(self, *_args: object) -> None:
                    return None

                def read(self) -> bytes:
                    return json.dumps(self._payload).encode()

            def fake_urlopen(request: Request, timeout: int) -> _Response:
                self.assertEqual(timeout, 30)
                for fragment, payload in responses.items():
                    if fragment in request.full_url:
                        return _Response(payload)
                raise AssertionError(f"unexpected GitHub request: {request.full_url}")

            reader = build_github_idle_evidence_reader(
                bearer_token="token",
                current_run_id=999,
                current_runner_name="current-runner",
            )
            with patch(
                "control_plane.workflows.runner_host_hygiene_executor.urlopen",
                side_effect=fake_urlopen,
            ):
                observations = reader(
                    "cbusillo/launchplane",
                    "ops-gate",
                    "current-runner",
                    True,
                )
            return next(item for item in observations if item.source == "github_jobs")

        missing_current_job = read_jobs_observation(
            run_status="in_progress",
            jobs=[
                {
                    "status": "completed",
                    "labels": ["self-hosted", "ops-gate"],
                    "runner_name": "sibling-runner",
                }
            ],
        )
        malformed_run_status = read_jobs_observation(
            run_status="mystery",
            jobs=[],
        )
        malformed_job_status = read_jobs_observation(
            run_status="in_progress",
            jobs=[
                {
                    "status": "mystery",
                    "labels": ["self-hosted", "ops-gate"],
                    "runner_name": "current-runner",
                }
            ],
        )

        self.assertEqual(missing_current_job.availability, "unavailable")
        self.assertEqual(missing_current_job.reason_code, "source_missing")
        for malformed in (malformed_run_status, malformed_job_status):
            self.assertEqual(malformed.availability, "unavailable")
            self.assertEqual(malformed.reason_code, "source_contradictory")

    def test_github_idle_aggregation_preserves_source_timestamp(self) -> None:
        samples = tuple(
            tuple(
                RunnerHostHygieneIdleObservation(
                    source=source,
                    scope="full_host",
                    scope_key="host",
                    subject_key="lane-key",
                    observed_at="2026-07-30T12:00:00Z",
                    availability="available",
                    state="idle",
                    active_count=0,
                    total_count=1,
                )
                for source in ("github_jobs", "github_runners")
            )
            for _ in range(2)
        )

        observations = _aggregate_github_idle_samples(
            samples=samples,
            observed_at="2026-07-30T13:00:00Z",
            observation_window_seconds=5,
        )

        self.assertEqual(
            {observation.observed_at for observation in observations},
            {"2026-07-30T12:00:00Z"},
        )

    def test_service_audit_poster_marks_route_not_found_non_retryable(self) -> None:
        poster = build_refreshing_service_audit_poster(
            service_url="https://launchplane.example",
            bearer_token_provider=lambda: "token",
        )
        error = HTTPError(
            url="https://launchplane.example/v1/evidence/runner-host-hygiene/audits",
            code=404,
            msg="not found",
            hdrs=Message(),
            fp=BytesIO(b"404 page not found"),
        )

        with patch(
            "control_plane.workflows.runner_host_hygiene_executor.urlopen",
            side_effect=error,
        ):
            with self.assertRaises(AuditDeliveryError) as raised:
                poster(_planned_audit(), "idempotency-key")

        self.assertFalse(raised.exception.retryable)
        self.assertIn("404 page not found", raised.exception.message)


def _request(
    *,
    mutate: bool,
    action: Literal[
        "prune_docker_cache",
        "prune_dangling_images",
        "prune_generated_run_cache",
        "remove_buildkit_state_volumes",
        "prune_runner_workdir",
        "restart_runner_service",
    ] = "prune_docker_cache",
    target_buildkit_state_volumes: tuple[str, ...] = (),
    allowed_buildkit_state_volumes: tuple[str, ...] = (),
    target_buildkit_builder: str = "",
    allowed_buildkit_builders: tuple[str, ...] = (),
    max_used_space_bytes: int = 0,
    prune_until: str = "168h",
    audit_record_key: str = "runner-host-hygiene/2026-05-23/chris-testing",
    runner_workdir_roots: tuple[RunnerWorkdirRoot, ...] = (
        RunnerWorkdirRoot(key="legacy", path="/opt/actions-runners"),
    ),
    generated_run_cache_roots: tuple[GeneratedRunCacheRoot, ...] = (),
    target_generated_cache_key: str = "",
    idle_observation_interval_seconds: int = 1,
    resolve_action_started: bool = False,
    execution_lane: str = "chris-testing-ops-gate",
    repository_scope: str = "cbusillo/launchplane",
    current_runner_name: str = "test-runner",
    github_idle_bindings: tuple[RunnerHostGitHubBinding, ...] = (
        RunnerHostGitHubBinding(
            repository_scope="cbusillo/launchplane",
            execution_lane="chris-testing-ops-gate",
            runner_name="test-runner",
            service_unit="launchplane-runner@ops-gate.service",
        ),
    ),
) -> RunnerHostHygieneExecutorRequest:
    return RunnerHostHygieneExecutorRequest(
        action=action,
        host_name="chris-testing",
        execution_lane=execution_lane,
        service_user="launchplane-runner-hygiene",
        repository_scope=repository_scope,
        current_runner_name=current_runner_name,
        github_idle_bindings=github_idle_bindings,
        audit_record_key=audit_record_key,
        retained_warm_builders=("odoo-docker-chris-testing",),
        target_buildkit_builder=target_buildkit_builder,
        allowed_buildkit_builders=allowed_buildkit_builders,
        max_used_space_bytes=max_used_space_bytes,
        target_buildkit_state_volumes=target_buildkit_state_volumes,
        allowed_buildkit_state_volumes=allowed_buildkit_state_volumes,
        mutate=mutate,
        prune_until=prune_until,
        runner_workdir_roots=runner_workdir_roots,
        generated_run_cache_roots=generated_run_cache_roots,
        target_generated_cache_key=target_generated_cache_key,
        idle_observation_interval_seconds=idle_observation_interval_seconds,
        resolve_action_started=resolve_action_started,
    )


def _planned_audit() -> RunnerHostHygieneApplyAuditRecord:
    command_runner = _CommandRunner()
    audit_poster = _AuditPoster()
    execute_runner_host_hygiene_executor(
        request=_request(mutate=False),
        remote_runner=command_runner,
        audit_poster=audit_poster,
    )
    return audit_poster.audits[0]


def _json_lines(*rows: dict[str, object]) -> str:
    return "".join(f"{json.dumps(row)}\n" for row in rows)


def _generated_cache_evidence(
    *,
    entries: tuple[tuple[int, int, int, int, tuple[int, ...]], ...],
    worker_observations: tuple[int, ...],
    observed_epoch_seconds: int | None = None,
    last_cleanup_epoch_seconds: int = 0,
    last_reclaimed_bytes: int = 0,
) -> str:
    allocated_bytes = sum(entry[2] for entry in entries)
    apparent_bytes = sum(entry[1] for entry in entries)
    rows: list[dict[str, object]] = [
        {
            "record_type": "summary",
            "cache_key": "codex-lab-runs",
            "observed_epoch_seconds": (
                int(time.time()) if observed_epoch_seconds is None else observed_epoch_seconds
            ),
            "apparent_bytes": apparent_bytes,
            "allocated_bytes": allocated_bytes,
            "entry_count": len(entries),
            "measurement_status": "complete",
            "owner_valid": True,
            "mode_valid": True,
            "symlink_safe": True,
            "owner_worker_observations": list(worker_observations),
            "entries_truncated": False,
            "minimum_age_hours": 1,
            "high_water_bytes": 100,
            "low_water_bytes": 10,
            "cooldown_hours": 1,
            "last_cleanup_epoch_seconds": last_cleanup_epoch_seconds,
            "last_reclaimed_bytes": last_reclaimed_bytes,
            "last_post_cleanup_allocated_bytes": allocated_bytes,
            "last_removed_run_ids": [],
        }
    ]
    rows.extend(
        {
            "record_type": "entry",
            "cache_key": "codex-lab-runs",
            "run_id": run_id,
            "apparent_bytes": apparent,
            "allocated_bytes": allocated,
            "age_seconds": age_seconds,
            "open_handle_observations": list(handle_observations),
        }
        for run_id, apparent, allocated, age_seconds, handle_observations in entries
    )
    return _json_lines(*rows)


def _docker_disk_usage_json(
    *,
    images: list[dict[str, object]] | None = None,
    containers: list[dict[str, object]] | None = None,
    volumes: list[dict[str, object]] | None = None,
    build_cache: list[dict[str, object]] | None = None,
    layers_size: int | None = None,
) -> str:
    image_rows = images or []
    build_cache_rows = build_cache or []
    return json.dumps(
        {
            "BuildCache": build_cache_rows,
            "Containers": containers or [],
            "Images": image_rows,
            "LayersSize": (
                layers_size
                if layers_size is not None
                else sum(size for row in image_rows if isinstance((size := row.get("Size")), int))
            ),
            "Volumes": volumes or [],
        }
    )


def _without_removed_disk_usage_volumes(output: str, removed_volumes: list[str]) -> str:
    if not removed_volumes or not output.strip():
        return output
    payload = json.loads(output)
    if not isinstance(payload, dict) or not isinstance(payload.get("Volumes"), list):
        return output
    removed_volume_set = set(removed_volumes)
    payload["Volumes"] = [
        row
        for row in payload["Volumes"]
        if not isinstance(row, dict) or row.get("Name") not in removed_volume_set
    ]
    return json.dumps(payload)
