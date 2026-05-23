from __future__ import annotations

import unittest

from collections.abc import Sequence

from control_plane.workflows.runner_host_hygiene_executor import RemoteCommandResult
from control_plane.workflows.runner_host_hygiene_executor import RunnerHostHygieneSshExecutorRequest
from control_plane.workflows.runner_host_hygiene_executor import (
    execute_runner_host_hygiene_ssh_executor,
)
from control_plane.workflows.runner_host_hygiene_executor import validate_ssh_executor_environment


class _RemoteRunner:
    def __init__(self, *, prune_returncode: int = 0, image_present_after: bool = True) -> None:
        self.commands: list[tuple[str, ...]] = []
        self._prune_returncode = prune_returncode
        self._image_present_after = image_present_after
        self._pruned = False

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
        if command_tuple[:3] == ("docker", "image", "inspect"):
            if not self._pruned or self._image_present_after:
                return RemoteCommandResult(returncode=0, stdout="[]\n")
            return RemoteCommandResult(returncode=1, stderr="image missing")
        if command_tuple == ("docker", "builder", "prune", "--all", "--force"):
            self._pruned = True
            return RemoteCommandResult(returncode=self._prune_returncode, stderr="prune failed")
        return RemoteCommandResult(returncode=127, stderr="unexpected command")


class _AuditPoster:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def __call__(self, audit: object, idempotency_key: str) -> dict[str, object]:
        status = getattr(audit, "status")
        audit_record_key = getattr(audit, "audit_record_key")
        self.records.append((str(status), idempotency_key))
        return {
            "status": "accepted",
            "records": {"runner_host_hygiene_audit_record_key": audit_record_key},
        }


class RunnerHostHygieneExecutorTests(unittest.TestCase):
    def test_executor_posts_planned_and_completed_audits(self) -> None:
        remote_runner = _RemoteRunner()
        audit_poster = _AuditPoster()

        result = execute_runner_host_hygiene_ssh_executor(
            request=_request(mutate=True),
            remote_runner=remote_runner,
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
        self.assertIn(("docker", "builder", "prune", "--all", "--force"), remote_runner.commands)

    def test_executor_blocks_before_prune_without_mutate_intent(self) -> None:
        remote_runner = _RemoteRunner()
        audit_poster = _AuditPoster()

        result = execute_runner_host_hygiene_ssh_executor(
            request=_request(mutate=False),
            remote_runner=remote_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "blocked")
        self.assertEqual([record[0] for record in audit_poster.records], ["planned"])
        self.assertNotIn(("docker", "builder", "prune", "--all", "--force"), remote_runner.commands)

    def test_executor_posts_failed_when_warm_builder_missing_after_prune(self) -> None:
        remote_runner = _RemoteRunner(image_present_after=False)
        audit_poster = _AuditPoster()

        result = execute_runner_host_hygiene_ssh_executor(
            request=_request(mutate=True),
            remote_runner=remote_runner,
            audit_poster=audit_poster,
        )

        self.assertEqual(result.status, "failed")
        self.assertIn(
            ("failed", "runner-host-hygiene:runner-host-hygiene/2026-05-23/chris-testing:failed"),
            audit_poster.records,
        )
        self.assertIn("post evidence is not healthy", result.message)

    def test_executor_environment_requires_configured_ssh_user_to_match_service_user(
        self,
    ) -> None:
        with self.assertRaisesRegex(Exception, "must match the approved service user"):
            validate_ssh_executor_environment(
                request=_request(mutate=True),
                env={
                    "LAUNCHPLANE_RUNNER_HOST_HYGIENE_SSH_HOST": "runner.example.com",
                    "LAUNCHPLANE_RUNNER_HOST_HYGIENE_SSH_USER": "root",
                    "LAUNCHPLANE_RUNNER_HOST_HYGIENE_SSH_PRIVATE_KEY": "private-key",
                    "LAUNCHPLANE_RUNNER_HOST_HYGIENE_SSH_KNOWN_HOSTS": "known-hosts",
                },
            )


def _request(*, mutate: bool) -> RunnerHostHygieneSshExecutorRequest:
    return RunnerHostHygieneSshExecutorRequest(
        action="prune_docker_cache",
        host_name="chris-testing",
        execution_lane="chris-testing-ops-gate",
        service_user="launchplane-runner-hygiene",
        repository_scope="cbusillo/launchplane",
        audit_record_key="runner-host-hygiene/2026-05-23/chris-testing",
        retained_warm_builders=("odoo-docker-chris-testing",),
        mutate=mutate,
    )
