from __future__ import annotations

import json
import unittest

from collections.abc import Sequence
import subprocess
from typing import Literal
from unittest.mock import patch
from urllib.request import Request

from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.workflows.runner_host_hygiene_executor import RemoteCommandResult
from control_plane.workflows.runner_host_hygiene_executor import RunnerHostHygieneExecutorRequest
from control_plane.workflows.runner_host_hygiene_executor import (
    build_refreshing_service_audit_poster,
)
from control_plane.workflows.runner_host_hygiene_executor import (
    execute_runner_host_hygiene_executor,
)
from control_plane.workflows.runner_host_hygiene_executor import _parse_volume_usage
from control_plane.workflows.runner_host_hygiene_executor import validate_local_executor_environment


class _CommandRunner:
    def __init__(
        self,
        *,
        prune_returncode: int = 0,
        image_present_after: bool = True,
        active_build_processes: str = "",
        volume_remove_returncode: int = 0,
        docker_summary: str | None = None,
        docker_verbose_summary: str | None = None,
        image_inventory: str | None = None,
        container_inventory: str | None = None,
        volume_inventory: str | None = None,
    ) -> None:
        self.commands: list[tuple[str, ...]] = []
        self._prune_returncode = prune_returncode
        self._volume_remove_returncode = volume_remove_returncode
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
        if command_tuple[:3] == ("docker", "image", "inspect"):
            if not self._pruned or self._image_present_after:
                return RemoteCommandResult(returncode=0, stdout="[]\n")
            return RemoteCommandResult(returncode=1, stderr="image missing")
        if command_tuple == (
            "bash",
            "-lc",
            "pgrep -af '[d]ocker buildx|[d]ocker build|[b]uildctl' || true",
        ):
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
            return RemoteCommandResult(returncode=self._prune_returncode, stderr="prune failed")
        if command_tuple[:6] == (
            "flock",
            "-n",
            "/tmp/launchplane-runner-host-hygiene.lock",
            "docker",
            "volume",
            "rm",
        ):
            self.removed_volumes.extend(command_tuple[6:])
            return RemoteCommandResult(
                returncode=self._volume_remove_returncode,
                stderr="volume rm failed",
            )
        return RemoteCommandResult(returncode=127, stderr="unexpected command")


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
        self.assertIn("active build processes", result.message)
        self.assertIn(
            ("failed", "runner-host-hygiene:runner-host-hygiene/2026-05-23/chris-testing:failed"),
            audit_poster.records,
        )
        self.assertNotIn(
            ("docker", "builder", "prune", "--all", "--force"), command_runner.commands
        )

    def test_executor_removes_allowlisted_zero_link_buildkit_state_volume(
        self,
    ) -> None:
        target_volume = "buildx_buildkit_launchplane-ci0_state"
        command_runner = _CommandRunner(
            docker_verbose_summary=(
                "Local Volumes space usage:\n\n"
                "VOLUME NAME     LINKS     SIZE\n"
                f"{target_volume}    0         50.49GB\n"
                "Build cache usage: 1GB\n"
            ),
            volume_inventory=json.dumps(
                [
                    {
                        "Driver": "local",
                        "Labels": {},
                        "Mountpoint": f"/var/lib/docker/volumes/{target_volume}/_data",
                        "Name": target_volume,
                        "UsageData": {"RefCount": 0, "Size": 50_490_000_000},
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
        command_runner = _CommandRunner(
            docker_verbose_summary=(
                "Local Volumes space usage:\n\n"
                "VOLUME NAME     LINKS     SIZE\n"
                f"{target_volume}    0         50.49GB\n"
                "Build cache usage: 1GB\n"
            ),
            volume_inventory=json.dumps(
                [
                    {
                        "Driver": "local",
                        "Labels": {},
                        "Mountpoint": f"/var/lib/docker/volumes/{target_volume}/_data",
                        "Name": target_volume,
                        "UsageData": {"RefCount": 0, "Size": 50_490_000_000},
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
) -> RunnerHostHygieneExecutorRequest:
    return RunnerHostHygieneExecutorRequest(
        action=action,
        host_name="chris-testing",
        execution_lane="chris-testing-ops-gate",
        service_user="launchplane-runner-hygiene",
        repository_scope="cbusillo/launchplane",
        audit_record_key="runner-host-hygiene/2026-05-23/chris-testing",
        retained_warm_builders=("odoo-docker-chris-testing",),
        target_buildkit_state_volumes=target_buildkit_state_volumes,
        allowed_buildkit_state_volumes=allowed_buildkit_state_volumes,
        mutate=mutate,
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
