from __future__ import annotations

import json
from email.message import Message
from io import BytesIO
import tempfile
import unittest

from collections.abc import Sequence
from collections.abc import Mapping
import subprocess
from pathlib import Path
from typing import Literal
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.workflows.runner_host_hygiene_executor import RemoteCommandResult
from control_plane.workflows.runner_host_hygiene_executor import RunnerHostHygieneExecutorRequest
from control_plane.workflows.runner_host_hygiene_executor import RunnerWorkdirRoot
from control_plane.workflows.runner_host_hygiene_executor import AuditDeliveryError
from control_plane.workflows.runner_host_hygiene_executor import (
    build_refreshing_service_audit_poster,
)
from control_plane.workflows.runner_host_hygiene_executor import (
    execute_runner_host_hygiene_executor,
)
from control_plane.workflows.runner_host_hygiene_executor import _parse_volume_usage
from control_plane.workflows.runner_host_hygiene_executor import validate_local_executor_environment
from control_plane.workflows.runner_host_hygiene_audit_spool import (
    RunnerHostHygieneAuditSpool,
)


_VOLUME_RM_PREFIX = (
    "flock",
    "-n",
    "/tmp/launchplane-runner-host-hygiene.lock",
    "docker",
    "volume",
    "rm",
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
        docker_summary: str | None = None,
        docker_verbose_summary: str | None = None,
        image_inventory: str | None = None,
        container_inventory: str | None = None,
        volume_inventory: str | None = None,
        runner_workdir_bytes: int = 0,
        runner_workdir_usage: Mapping[str, tuple[int, int]] | None = None,
    ) -> None:
        self.commands: list[tuple[str, ...]] = []
        self._prune_returncode = prune_returncode
        self._prune_stderr = prune_stderr
        self._volume_remove_returncode = volume_remove_returncode
        self._volume_remove_partial_failure = volume_remove_partial_failure
        self._image_present_after = image_present_after
        self._active_build_processes = active_build_processes
        self._docker_summary = docker_summary or "Images 1 1GB 500MB\n"
        self._docker_verbose_summary = docker_verbose_summary or (
            "Local Volumes space usage:\n\n"
            "VOLUME NAME     LINKS     SIZE\n"
            "runner-cache    0         45.5GB\n\n"
            "Build cache usage: 1GB\n"
        )
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
                    "UsageData": {"RefCount": 0, "Size": 1234},
                }
            ]
        )
        self._runner_workdir_bytes = runner_workdir_bytes
        self._runner_workdir_usage = dict(runner_workdir_usage or {})
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
        if command_tuple == (
            "docker",
            "system",
            "df",
            "--format",
            "{{.Type}} {{.TotalCount}} {{.Size}} {{.Reclaimable}}",
        ):
            return RemoteCommandResult(returncode=0, stdout=self._docker_summary)
        if command_tuple == ("docker", "system", "df", "-v"):
            return RemoteCommandResult(
                returncode=0,
                stdout=_without_removed_volume_rows(
                    self._docker_verbose_summary,
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
        if command_tuple == (
            "bash",
            "-lc",
            "docker volume ls -q | xargs -r docker volume inspect",
        ):
            return RemoteCommandResult(
                returncode=0,
                stdout=_without_removed_volume_inventory(
                    self._volume_inventory,
                    self.removed_volumes,
                ),
            )
        if (
            command_tuple[:2] == ("bash", "-lc")
            and "-name _work" in command_tuple[2]
            and "du -sb" in command_tuple[2]
        ):
            apparent_bytes = self._runner_workdir_bytes
            allocated_bytes = self._runner_workdir_bytes
            for root_path, root_usage in self._runner_workdir_usage.items():
                if root_path in command_tuple[2]:
                    apparent_bytes, allocated_bytes = root_usage
                    break
            return RemoteCommandResult(
                returncode=0,
                stdout=f"{apparent_bytes}\n{allocated_bytes}\n",
            )
        if command_tuple[:3] == ("docker", "image", "inspect"):
            if not self._pruned or self._image_present_after:
                return RemoteCommandResult(returncode=0, stdout="[]\n")
            return RemoteCommandResult(returncode=1, stderr="image missing")
        if command_tuple[:2] == ("bash", "-lc") and "pgrep -af" in command_tuple[2]:
            return RemoteCommandResult(returncode=0, stdout=self._active_build_processes)
        if command_tuple == (
            "flock",
            "-n",
            "/tmp/launchplane-runner-host-hygiene.lock",
            "docker",
            "builder",
            "prune",
            "--force",
            "--filter",
            "until=168h",
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
        if command_tuple[:5] == (
            "flock",
            "-n",
            "/tmp/launchplane-runner-host-hygiene.lock",
            "docker",
            "builder",
        ):
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
        if command_tuple[:5] == (
            "flock",
            "-n",
            "/tmp/launchplane-runner-host-hygiene.lock",
            "docker",
            "builder",
        ):
            raise RuntimeError("simulated runner termination")
        return super().__call__(command_tuple, timeout_seconds)


class RunnerHostHygieneWorkflowTests(unittest.TestCase):
    def test_workflow_runs_on_ops_lane_without_xargs_dependency(self) -> None:
        workflow_text = Path(".github/workflows/runner-host-hygiene.yml").read_text(
            encoding="utf-8"
        )

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
        self.assertIn("      - name: Upload executor result\n        if: always()\n", workflow_text)


class RunnerHostHygieneExecutorTests(unittest.TestCase):
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
                "docker",
                "builder",
                "prune",
                "--force",
                "--filter",
                "until=168h",
            ),
            command_runner.commands,
        )
        self.assertNotIn(
            ("docker", "builder", "prune", "--all", "--force"), command_runner.commands
        )

    def test_executor_records_typed_docker_reclaimable_bytes(self) -> None:
        command_runner = _CommandRunner(
            docker_summary=(
                "Images 109 23.35GB 23.12GB (99%)\n"
                "Containers 2 0B 0B\n"
                "Local Volumes 38 161.5GB 97.47GB (60%)\n"
                "Build Cache 1556 27.88GB 27.88GB\n"
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
            image
            for image in post_apply_report.image_inventory
            if image.repository == "odoo-docker-chris-testing"
        )
        self.assertEqual(warm_image.tag, "latest")
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
        self.assertEqual(volume.labels, ("launchplane.scope=test",))
        self.assertIn(("docker", "system", "df", "-v"), command_runner.commands)
        self.assertIn(
            (
                "bash",
                "-lc",
                "docker volume ls -q | xargs -r docker volume inspect",
            ),
            command_runner.commands,
        )

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
            docker_verbose_summary=(
                "Local Volumes space usage:\n\n"
                "VOLUME NAME     LINKS     SIZE\n"
                "buildx_buildkit_old0_state    0         12GB\n"
                "Build cache usage: 1GB\n"
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

    def test_volume_usage_parser_accepts_documented_local_volume_header(
        self,
    ) -> None:
        volume_usage = _parse_volume_usage(
            "Local Volumes:\n\n"
            "VOLUME NAME     LINKS     SIZE\n"
            "runner-cache    0         45.5GB\n\n"
            "Build cache usage: 1GB\n"
        )

        self.assertEqual(volume_usage["runner-cache"].links, 0)
        self.assertEqual(volume_usage["runner-cache"].size_bytes, 45_500_000_000)

    def test_executor_fails_closed_when_docker_summary_is_unparseable(self) -> None:
        command_runner = _CommandRunner(docker_summary="TYPE TOTAL ACTIVE SIZE RECLAIMABLE\n")
        audit_poster = _AuditPoster()

        with self.assertRaisesRegex(Exception, "did not include reclaimable bytes"):
            execute_runner_host_hygiene_executor(
                request=_request(mutate=True),
                remote_runner=command_runner,
                audit_poster=audit_poster,
            )

        self.assertEqual(audit_poster.records, [])

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
        self.assertNotIn(
            ("docker", "builder", "prune", "--all", "--force"), command_runner.commands
        )

    def test_executor_blocks_prune_when_active_build_processes_are_present(self) -> None:
        command_runner = _CommandRunner(active_build_processes="123 docker buildx build .\n")
        audit_poster = _AuditPoster()

        result = execute_runner_host_hygiene_executor(
            request=_request(mutate=True),
            remote_runner=command_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("active runner or build processes", result.message)
        self.assertIn(
            ("failed", "runner-host-hygiene:runner-host-hygiene/2026-05-23/chris-testing:failed"),
            audit_poster.records,
        )
        self.assertNotIn(
            ("docker", "builder", "prune", "--all", "--force"), command_runner.commands
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

        self.assertEqual(result.status, "failed")
        self.assertIn("Runner.Worker", result.message)
        self.assertFalse(
            any(
                command[:5]
                == ("flock", "-n", "/tmp/launchplane-runner-host-hygiene.lock", "docker", "builder")
                for command in command_runner.commands
            )
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

        self.assertEqual(result.status, "failed")
        self.assertIn("789", result.message)

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
        self.assertEqual(len(idle_commands), 2)

    def test_executor_spools_terminal_audit_before_failed_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            spool = RunnerHostHygieneAuditSpool(
                root=temporary_path / "spool",
                artifact_file=temporary_path / "artifact.json",
            )
            request = _request(mutate=True)

            result = execute_runner_host_hygiene_executor(
                request=request,
                remote_runner=_CommandRunner(),
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
            self.assertNotIn("Bearer", artifact_text)
            self.assertNotIn("temporary-token", artifact_text)

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

    def test_executor_resolves_action_started_without_repeating_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            spool = RunnerHostHygieneAuditSpool(root=Path(temporary_directory) / "spool")
            request = _request(mutate=True)
            with self.assertRaisesRegex(RuntimeError, "simulated runner termination"):
                execute_runner_host_hygiene_executor(
                    request=request,
                    remote_runner=_CrashingCommandRunner(),
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
            resolution_runner = _CommandRunner()

            resolved = execute_runner_host_hygiene_executor(
                request=_request(mutate=True, resolve_action_started=True),
                remote_runner=resolution_runner,
                audit_poster=_AuditPoster(),
                audit_spool=spool,
                audit_delivery_sleeper=lambda _seconds: None,
            )

            self.assertEqual(resolved.status, "failed")
            self.assertTrue(resolved.reconciled)
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
        summary_row, inventory_row = _buildkit_volume_evidence(target_volume)
        command_runner = _CommandRunner(
            docker_verbose_summary=(
                "Local Volumes space usage:\n\n"
                "VOLUME NAME     LINKS     SIZE\n"
                f"{summary_row}"
                "Build cache usage: 1GB\n"
            ),
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
        first_summary_row, first_inventory_row = _buildkit_volume_evidence(first_volume)
        second_summary_row, second_inventory_row = _buildkit_volume_evidence(
            second_volume,
            size_bytes=12_480_000_000,
        )
        command_runner = _CommandRunner(
            volume_remove_partial_failure=True,
            docker_verbose_summary=(
                "Local Volumes space usage:\n\n"
                "VOLUME NAME     LINKS     SIZE\n"
                f"{first_summary_row}"
                f"{second_summary_row}"
                "Build cache usage: 1GB\n"
            ),
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
            docker_verbose_summary=(
                "Local Volumes space usage:\n\n"
                "VOLUME NAME     LINKS     SIZE\n"
                f"{target_volume}    1         32.48GB\n"
                "Build cache usage: 1GB\n"
            ),
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
        summary_row, inventory_row = _buildkit_volume_evidence(target_volume)
        command_runner = _CommandRunner(
            docker_verbose_summary=(
                "Local Volumes space usage:\n\n"
                "VOLUME NAME     LINKS     SIZE\n"
                f"{summary_row}"
                "Build cache usage: 1GB\n"
            ),
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
        self.assertIn("post evidence is not healthy", result.message)

    def test_executor_environment_requires_runner_user_to_match_service_user(
        self,
    ) -> None:
        with self.assertRaisesRegex(Exception, "must match the approved service user"):
            validate_local_executor_environment(
                request=_request(mutate=True),
                current_user="root",
            )

    def test_executor_environment_requires_repository_scope_to_match_github_repository(
        self,
    ) -> None:
        with self.assertRaisesRegex(Exception, "must match GITHUB_REPOSITORY"):
            validate_local_executor_environment(
                request=_request(mutate=True),
                env={"GITHUB_REPOSITORY": "cbusillo/other"},
                current_user="launchplane-runner-hygiene",
            )

    @staticmethod
    def test_executor_environment_honors_explicit_empty_env_mapping() -> None:
        validate_local_executor_environment(
            request=_request(mutate=True),
            env={},
            current_user="launchplane-runner-hygiene",
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
            def __enter__(self) -> "_Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            @staticmethod
            def read() -> bytes:
                return b'{"status":"accepted"}'

        observed_authorization_headers: list[str | None] = []
        tokens = iter(("first-token", "second-token"))

        def fake_urlopen(request: Request, timeout: int) -> _Response:
            self.assertEqual(timeout, 30)
            observed_authorization_headers.append(request.get_header("Authorization"))
            return _Response()

        poster = build_refreshing_service_audit_poster(
            service_url="https://launchplane.example",
            bearer_token_provider=lambda: next(tokens),
        )
        audit = _planned_audit()
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
        "remove_buildkit_state_volumes",
        "prune_runner_workdir",
        "restart_runner_service",
    ] = "prune_docker_cache",
    target_buildkit_state_volumes: tuple[str, ...] = (),
    allowed_buildkit_state_volumes: tuple[str, ...] = (),
    audit_record_key: str = "runner-host-hygiene/2026-05-23/chris-testing",
    runner_workdir_roots: tuple[RunnerWorkdirRoot, ...] = (
        RunnerWorkdirRoot(key="legacy", path="/opt/actions-runners"),
    ),
    resolve_action_started: bool = False,
) -> RunnerHostHygieneExecutorRequest:
    return RunnerHostHygieneExecutorRequest(
        action=action,
        host_name="chris-testing",
        execution_lane="chris-testing-ops-gate",
        service_user="launchplane-runner-hygiene",
        repository_scope="cbusillo/launchplane",
        audit_record_key=audit_record_key,
        retained_warm_builders=("odoo-docker-chris-testing",),
        target_buildkit_state_volumes=target_buildkit_state_volumes,
        allowed_buildkit_state_volumes=allowed_buildkit_state_volumes,
        mutate=mutate,
        runner_workdir_roots=runner_workdir_roots,
        idle_observation_interval_seconds=0,
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


def _without_removed_volume_inventory(output: str, removed_volumes: list[str]) -> str:
    if not removed_volumes or not output.strip():
        return output
    payload = json.loads(output)
    if not isinstance(payload, list):
        return output
    removed_volume_set = set(removed_volumes)
    return json.dumps(
        [
            row
            for row in payload
            if not isinstance(row, dict) or row.get("Name") not in removed_volume_set
        ]
    )


def _without_removed_volume_rows(output: str, removed_volumes: list[str]) -> str:
    if not removed_volumes:
        return output
    removed_volume_set = set(removed_volumes)
    return "\n".join(
        line
        for line in output.splitlines()
        if not line.split() or line.split()[0] not in removed_volume_set
    )
