import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from collections.abc import Sequence
from typing import cast
import unittest
from unittest.mock import patch
from urllib.request import Request

from click import Command
from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneObservation
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyPolicy
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyPlan
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyRequest
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneAdapterPolicy
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneAdapterProposal
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneImageInventoryItem
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneGeneratedCacheBudget
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneGeneratedCacheEntry
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneGeneratedCacheUsage
from control_plane.contracts.runner_host_hygiene import RunnerHostHygienePolicy
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneReport
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneRunnerWorkdirUsage
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneVolumeInventoryItem
from control_plane.contracts.runner_host_hygiene import evaluate_runner_host_hygiene
from control_plane.contracts.runner_host_hygiene import plan_runner_host_hygiene_apply
from control_plane.contracts.runner_host_hygiene import plan_runner_host_hygiene_adapter_boundary
from control_plane.cli_runner_lanes import _runner_host_hygiene_bearer_token
from control_plane.workflows.runner_host_hygiene_executor import DOCKER_BUILDX_PLUGIN_PATH_COMMAND
from control_plane.workflows.runner_host_hygiene_executor import RemoteCommandResult
from control_plane.workflows.runner_host_hygiene_executor import RunnerHostHygieneExecutorRequest
from control_plane.workflows.runner_host_hygiene_executor import RunnerWorkdirRoot
from control_plane.workflows.runner_host_hygiene_executor import _DOCKER_DISK_USAGE_COMMAND
from control_plane.workflows.runner_host_hygiene_executor import _RUNNER_WORKDIR_USAGE_HELPER
from control_plane.workflows.runner_host_hygiene_executor import collect_runner_host_hygiene_report


CLI_MAIN = cast(Command, main)


class RunnerHostHygieneTests(unittest.TestCase):
    def test_report_is_healthy_when_observation_satisfies_policy(self) -> None:
        report = evaluate_runner_host_hygiene(
            policy=RunnerHostHygienePolicy(
                minimum_free_disk_bytes=100,
                maximum_docker_reclaimable_bytes=50,
                maximum_runner_workdir_bytes=75,
                required_warm_builders=("odoo-docker-chris-testing",),
            ),
            observation=RunnerHostHygieneObservation(
                host_name="chris-testing",
                observed_at="2026-05-23T13:00:00Z",
                free_disk_bytes=200,
                docker_reclaimable_bytes=25,
                runner_workdir_bytes=50,
                warm_builders=(" Odoo-Docker-Chris-Testing ",),
            ),
        )

        self.assertEqual(report.status, "healthy")
        self.assertEqual(report.host_name, "chris-testing")
        self.assertEqual(report.warm_builders, ("odoo-docker-chris-testing",))
        self.assertEqual(report.findings, ())
        self.assertIn("report-only", report.summary)

    def test_report_marks_partial_runner_workdir_measurement_for_attention(self) -> None:
        report = evaluate_runner_host_hygiene(
            policy=RunnerHostHygienePolicy(),
            observation=RunnerHostHygieneObservation(
                host_name="chris-testing",
                observed_at="2026-07-25T23:00:00Z",
                free_disk_bytes=200,
                runner_workdir_usage=(
                    RunnerHostHygieneRunnerWorkdirUsage(
                        root_key="legacy",
                        apparent_bytes=100,
                        allocated_bytes=90,
                        measurement_status="partial",
                    ),
                ),
            ),
        )

        self.assertEqual(report.status, "attention")
        self.assertEqual(
            [finding.code for finding in report.findings],
            ["runner_workdir_measurement_partial"],
        )

    def test_report_marks_generated_cache_budget_and_safety_findings(self) -> None:
        report = evaluate_runner_host_hygiene(
            policy=RunnerHostHygienePolicy(
                generated_cache_budgets=(
                    RunnerHostHygieneGeneratedCacheBudget(
                        cache_key="codex-lab-runs",
                        high_water_bytes=100,
                    ),
                )
            ),
            observation=RunnerHostHygieneObservation(
                host_name="chris-testing",
                observed_at="2026-07-27T00:00:00Z",
                free_disk_bytes=200,
                generated_cache_apparent_bytes=120,
                generated_cache_allocated_bytes=120,
                generated_cache_usage=(
                    RunnerHostHygieneGeneratedCacheUsage(
                        cache_key="codex-lab-runs",
                        apparent_bytes=120,
                        allocated_bytes=120,
                        entry_count=1,
                        measurement_status="partial",
                    ),
                ),
            ),
        )

        self.assertEqual(report.status, "attention")
        self.assertEqual(
            [finding.code for finding in report.findings],
            [
                "generated_cache_above_limit",
                "generated_cache_measurement_partial",
                "generated_cache_safety_failed",
            ],
        )

    def test_report_preserves_sorted_resource_inventory(self) -> None:
        report = evaluate_runner_host_hygiene(
            policy=RunnerHostHygienePolicy(),
            observation=RunnerHostHygieneObservation(
                host_name="chris-testing",
                observed_at="2026-05-23T13:00:00Z",
                free_disk_bytes=200,
                image_inventory=(
                    RunnerHostHygieneImageInventoryItem(
                        image_id="image-b",
                        repository="z-image",
                        tag="latest",
                    ),
                    RunnerHostHygieneImageInventoryItem(
                        image_id="image-a",
                        repository="a-image",
                        tag="latest",
                        in_use=True,
                    ),
                ),
                volume_inventory=(
                    RunnerHostHygieneVolumeInventoryItem(name="z-volume", driver="local"),
                    RunnerHostHygieneVolumeInventoryItem(
                        name="a-volume",
                        driver="local",
                        labels=(" role=cache ", ""),
                    ),
                ),
            ),
        )

        self.assertEqual([item.image_id for item in report.image_inventory], ["image-a", "image-b"])
        self.assertTrue(report.image_inventory[0].in_use)
        self.assertEqual([item.name for item in report.volume_inventory], ["a-volume", "z-volume"])
        self.assertEqual(report.volume_inventory[0].labels, ("role=cache",))

    def test_report_finds_missing_builder_and_low_free_disk(self) -> None:
        report = evaluate_runner_host_hygiene(
            policy=RunnerHostHygienePolicy(
                minimum_free_disk_bytes=500,
                required_warm_builders=("odoo-docker-chris-testing",),
            ),
            observation=RunnerHostHygieneObservation(
                host_name="chris-testing",
                observed_at="2026-05-23T13:00:00Z",
                free_disk_bytes=100,
                warm_builders=(),
            ),
        )

        self.assertEqual(report.status, "attention")
        self.assertEqual(
            [finding.code for finding in report.findings],
            ["free_disk_below_minimum", "required_warm_builder_missing"],
        )

    def test_report_flags_orphan_buildkit_by_default(self) -> None:
        report = evaluate_runner_host_hygiene(
            policy=RunnerHostHygienePolicy(),
            observation=RunnerHostHygieneObservation(
                host_name="chris-testing",
                observed_at="2026-05-23T13:00:00Z",
                free_disk_bytes=100,
                orphan_buildkit_containers=1,
                orphan_buildkit_volumes=2,
            ),
        )

        self.assertEqual(report.status, "attention")
        self.assertEqual(
            [finding.code for finding in report.findings],
            ["orphan_buildkit_present"],
        )

    def test_report_can_allow_orphan_buildkit_for_observation_only(self) -> None:
        report = evaluate_runner_host_hygiene(
            policy=RunnerHostHygienePolicy(allow_orphan_buildkit=True),
            observation=RunnerHostHygieneObservation(
                host_name="chris-testing",
                observed_at="2026-05-23T13:00:00Z",
                free_disk_bytes=100,
                orphan_buildkit_containers=1,
            ),
        )

        self.assertEqual(report.status, "healthy")

    def test_executor_report_preserves_docker_toolchain_evidence(self) -> None:
        outputs: dict[tuple[str, ...], str] = {
            (
                "df",
                "-B1",
                "-P",
                "/",
            ): "Filesystem 1B-blocks Used Available Use% Mounted on\n/dev/root 1000 100 900 10% /\n",
            (
                "docker",
                "system",
                "df",
                "--format",
                "{{.Type}} {{.TotalCount}} {{.Size}} {{.Reclaimable}}",
            ): "Images 1 10MB 0B\nBuild Cache 1 10MB 0B\n",
            ("docker", "system", "df", "-v"): "",
            ("docker", "image", "inspect", "odoo-docker-chris-testing"): "{}",
            (
                "docker",
                "image",
                "ls",
                "--all",
                "--no-trunc",
                "--format",
                "{{json .}}",
            ): "",
            ("docker", "ps", "--all", "--no-trunc", "--format", "{{json .}}"): "",
            (
                "bash",
                "-lc",
                "docker volume ls -q | xargs -r docker volume inspect",
            ): "",
            (
                "/usr/bin/sudo",
                "-n",
                _RUNNER_WORKDIR_USAGE_HELPER,
                "legacy=/opt/actions-runners",
            ): "0\n0\n",
            ("docker", "version", "--format", "{{.Server.Version}}"): "26.1.5+dfsg1\n",
            ("docker", "version", "--format", "{{.Client.Version}}"): "26.1.5+dfsg1\n",
            ("docker", "buildx", "version"): "github.com/docker/buildx 0.13.1+ds1 0.13.1+ds1-3\n",
            ("docker", "buildx", "inspect"): "Driver: docker-container\nBuildKit: v0.30.0\n",
            (
                "sh",
                "-c",
                DOCKER_BUILDX_PLUGIN_PATH_COMMAND,
            ): "/usr/libexec/docker/cli-plugins/docker-buildx\n",
            (
                "sh",
                "-c",
                "dpkg-query -W -f='${Package} ${Version}' docker-buildx 2>/dev/null",
            ): "docker-buildx 0.13.1+ds1-3",
            ("sh", "-c", "rpm -q docker-buildx 2>/dev/null"): "",
        }

        def runner(command: Sequence[str], _timeout: int) -> RemoteCommandResult:
            command_tuple = tuple(command)
            if command_tuple == _DOCKER_DISK_USAGE_COMMAND:
                return RemoteCommandResult(returncode=0, stdout=_empty_docker_disk_usage())
            if command_tuple[:3] == ("docker", "volume", "inspect"):
                return RemoteCommandResult(returncode=1)
            if command_tuple[:3] == (
                "/usr/bin/sudo",
                "-n",
                _RUNNER_WORKDIR_USAGE_HELPER,
            ):
                return RemoteCommandResult(returncode=0, stdout="0\n0\n")
            return RemoteCommandResult(returncode=0, stdout=outputs.get(command_tuple, ""))

        report = collect_runner_host_hygiene_report(
            request=RunnerHostHygieneExecutorRequest(
                action="prune_docker_cache",
                host_name="chris-testing",
                execution_lane="chris-testing-ops-gate",
                service_user="gha",
                repository_scope="cbusillo/launchplane",
                audit_record_key="runner-host-hygiene/2026-06-04/chris-testing",
                retained_warm_builders=("odoo-docker-chris-testing",),
                runner_workdir_roots=(
                    RunnerWorkdirRoot(key="legacy", path="/opt/actions-runners"),
                ),
            ),
            remote_runner=runner,
        )

        self.assertIsNotNone(report.docker_toolchain)
        assert report.docker_toolchain is not None
        self.assertEqual(report.docker_toolchain.docker_buildx_version, "0.13.1+ds1")
        self.assertEqual(
            report.docker_toolchain.docker_buildx_plugin_path,
            "/usr/libexec/docker/cli-plugins/docker-buildx",
        )
        self.assertEqual(
            report.docker_toolchain.docker_buildx_package, "docker-buildx 0.13.1+ds1-3"
        )
        self.assertEqual(report.docker_toolchain.docker_buildx_source, "system package")
        self.assertEqual(report.docker_toolchain.buildkit_version, "0.30.0")

    def test_executor_report_uses_active_managed_buildx_plugin_path(self) -> None:
        outputs: dict[tuple[str, ...], str] = {
            ("hostname",): "chris-testing\n",
            (
                "df",
                "-B1",
                "-P",
                "/",
            ): "Filesystem 1B-blocks Used Available Use% Mounted on\n/dev/root 1000 100 900 10% /\n",
            (
                "docker",
                "system",
                "df",
                "--format",
                "{{.Type}} {{.TotalCount}} {{.Size}} {{.Reclaimable}}",
            ): "Images 1 10MB 0B\nBuild Cache 1 10MB 0B\n",
            ("docker", "system", "df", "-v"): "",
            ("docker", "builder", "ls"): "",
            ("docker", "volume", "ls", "-q"): "",
            (
                "docker",
                "image",
                "ls",
                "--all",
                "--no-trunc",
                "--format",
                "{{json .}}",
            ): "",
            ("docker", "ps", "--all", "--no-trunc", "--format", "{{json .}}"): "",
            (
                "bash",
                "-lc",
                "docker volume ls -q | xargs -r docker volume inspect",
            ): "",
            (
                "/usr/bin/sudo",
                "-n",
                _RUNNER_WORKDIR_USAGE_HELPER,
                "legacy=/opt/actions-runners",
            ): "0\n0\n",
            ("docker", "version", "--format", "{{.Server.Version}}"): "26.1.5+dfsg1\n",
            ("docker", "version", "--format", "{{.Client.Version}}"): "26.1.5+dfsg1\n",
            ("docker", "buildx", "version"): "github.com/docker/buildx v0.23.0 abcdef\n",
            ("docker", "buildx", "inspect"): "Driver: docker-container\nBuildKit: v0.30.0\n",
            (
                "sh",
                "-c",
                DOCKER_BUILDX_PLUGIN_PATH_COMMAND,
            ): "/usr/local/lib/docker/cli-plugins/docker-buildx\n",
            (
                "sh",
                "-c",
                "dpkg-query -W -f='${Package} ${Version}' docker-buildx 2>/dev/null",
            ): "docker-buildx 0.13.1+ds1-3",
            ("sh", "-c", "rpm -q docker-buildx 2>/dev/null"): "",
        }

        def runner(command: Sequence[str], _timeout: int) -> RemoteCommandResult:
            command_tuple = tuple(command)
            if command_tuple == _DOCKER_DISK_USAGE_COMMAND:
                return RemoteCommandResult(returncode=0, stdout=_empty_docker_disk_usage())
            if command_tuple[:3] == ("docker", "volume", "inspect"):
                return RemoteCommandResult(returncode=1)
            if command_tuple[:3] == (
                "/usr/bin/sudo",
                "-n",
                _RUNNER_WORKDIR_USAGE_HELPER,
            ):
                return RemoteCommandResult(returncode=0, stdout="0\n0\n")
            return RemoteCommandResult(returncode=0, stdout=outputs.get(command_tuple, ""))

        report = collect_runner_host_hygiene_report(
            request=RunnerHostHygieneExecutorRequest(
                action="prune_docker_cache",
                host_name="chris-testing",
                execution_lane="chris-testing-ops-gate",
                service_user="gha",
                repository_scope="cbusillo/launchplane",
                audit_record_key="runner-host-hygiene/2026-06-04/chris-testing",
                retained_warm_builders=("odoo-docker-chris-testing",),
                runner_workdir_roots=(
                    RunnerWorkdirRoot(key="legacy", path="/opt/actions-runners"),
                ),
            ),
            remote_runner=runner,
        )

        self.assertIsNotNone(report.docker_toolchain)
        assert report.docker_toolchain is not None
        self.assertEqual(
            report.docker_toolchain.docker_buildx_plugin_path,
            "/usr/local/lib/docker/cli-plugins/docker-buildx",
        )
        self.assertEqual(report.docker_toolchain.docker_buildx_source, "managed plugin")
        self.assertEqual(
            report.docker_toolchain.docker_buildx_package, "docker-buildx 0.13.1+ds1-3"
        )

    def test_buildx_plugin_path_probe_falls_back_when_docker_info_has_no_path(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            bin_dir = temp_path / "bin"
            bin_dir.mkdir()
            docker_path = bin_dir / "docker"
            docker_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            docker_path.chmod(0o755)
            plugin_path = Path(temp_dir) / "docker-buildx"
            plugin_path.write_text("#!/bin/sh\n", encoding="utf-8")
            plugin_path.chmod(0o755)
            missing_path = str(Path(temp_dir) / "missing-docker-buildx")
            probe = DOCKER_BUILDX_PLUGIN_PATH_COMMAND
            for fallback_path in (
                "$HOME/.docker/cli-plugins/docker-buildx",
                "/usr/local/lib/docker/cli-plugins/docker-buildx",
                "/usr/local/libexec/docker/cli-plugins/docker-buildx",
                "/usr/lib/docker/cli-plugins/docker-buildx",
                "/usr/libexec/docker/cli-plugins/docker-buildx",
            ):
                probe = probe.replace(
                    fallback_path,
                    str(plugin_path) if fallback_path.startswith("$HOME") else missing_path,
                )
            result = subprocess.run(
                ["sh", "-c", probe],
                check=False,
                capture_output=True,
                text=True,
                env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": temp_dir},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(plugin_path))

    def test_executor_report_omits_empty_docker_toolchain_evidence(self) -> None:
        outputs: dict[tuple[str, ...], str] = {
            (
                "df",
                "-B1",
                "-P",
                "/",
            ): "Filesystem 1B-blocks Used Available Use% Mounted on\n/dev/root 1000 100 900 10% /\n",
            (
                "docker",
                "system",
                "df",
                "--format",
                "{{.Type}} {{.TotalCount}} {{.Size}} {{.Reclaimable}}",
            ): "Images 1 10MB 0B\nBuild Cache 1 10MB 0B\n",
            ("docker", "system", "df", "-v"): "",
            ("docker", "image", "inspect", "odoo-docker-chris-testing"): "{}",
            (
                "docker",
                "image",
                "ls",
                "--all",
                "--no-trunc",
                "--format",
                "{{json .}}",
            ): "",
            ("docker", "ps", "--all", "--no-trunc", "--format", "{{json .}}"): "",
            (
                "bash",
                "-lc",
                "docker volume ls -q | xargs -r docker volume inspect",
            ): "",
            (
                "/usr/bin/sudo",
                "-n",
                _RUNNER_WORKDIR_USAGE_HELPER,
                "legacy=/opt/actions-runners",
            ): "0\n0\n",
        }

        def runner(command: Sequence[str], _timeout: int) -> RemoteCommandResult:
            command_tuple = tuple(command)
            if command_tuple == _DOCKER_DISK_USAGE_COMMAND:
                return RemoteCommandResult(returncode=0, stdout=_empty_docker_disk_usage())
            if command_tuple[:3] == ("docker", "volume", "inspect"):
                return RemoteCommandResult(returncode=1)
            if command_tuple[:3] == (
                "/usr/bin/sudo",
                "-n",
                _RUNNER_WORKDIR_USAGE_HELPER,
            ):
                return RemoteCommandResult(returncode=0, stdout="0\n0\n")
            if command_tuple in outputs:
                return RemoteCommandResult(returncode=0, stdout=outputs[command_tuple])
            return RemoteCommandResult(returncode=1, stderr="not installed")

        report = collect_runner_host_hygiene_report(
            request=RunnerHostHygieneExecutorRequest(
                action="prune_docker_cache",
                host_name="chris-testing",
                execution_lane="chris-testing-ops-gate",
                service_user="gha",
                repository_scope="cbusillo/launchplane",
                audit_record_key="runner-host-hygiene/2026-06-04/chris-testing",
                retained_warm_builders=("odoo-docker-chris-testing",),
                runner_workdir_roots=(
                    RunnerWorkdirRoot(key="legacy", path="/opt/actions-runners"),
                ),
            ),
            remote_runner=runner,
        )

        self.assertIsNone(report.docker_toolchain)


class RunnerHostHygieneCliTests(unittest.TestCase):
    def test_cli_builds_report_from_observation_and_flags(self) -> None:
        with TemporaryDirectory() as temp_dir:
            observation_file = Path(temp_dir) / "observation.json"
            observation_file.write_text(
                json.dumps(
                    RunnerHostHygieneObservation(
                        host_name="chris-testing",
                        observed_at="2026-05-23T13:00:00Z",
                        free_disk_bytes=100,
                        docker_reclaimable_bytes=80,
                    ).model_dump(mode="json")
                ),
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "runner-host-hygiene-report",
                    "--observation-file",
                    observation_file.as_posix(),
                    "--minimum-free-disk-bytes",
                    "200",
                    "--maximum-docker-reclaimable-bytes",
                    "50",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["mode"], "report-only")
        self.assertEqual(payload["report"]["status"], "attention")
        self.assertEqual(
            [finding["code"] for finding in payload["report"]["findings"]],
            ["docker_reclaimable_above_limit", "free_disk_below_minimum"],
        )

    def test_cli_accepts_policy_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            observation_file = root / "observation.json"
            policy_file = root / "policy.json"
            observation_file.write_text(
                json.dumps(
                    RunnerHostHygieneObservation(
                        host_name="chris-testing",
                        observed_at="2026-05-23T13:00:00Z",
                        free_disk_bytes=500,
                        warm_builders=("odoo-enterprise-chris-testing",),
                    ).model_dump(mode="json")
                ),
                encoding="utf-8",
            )
            policy_file.write_text(
                json.dumps(
                    RunnerHostHygienePolicy(
                        required_warm_builders=("odoo-enterprise-chris-testing",)
                    ).model_dump(mode="json")
                ),
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "runner-host-hygiene-report",
                    "--observation-file",
                    observation_file.as_posix(),
                    "--policy-file",
                    policy_file.as_posix(),
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["report"]["status"], "healthy")

    def test_executor_bearer_token_prefers_configured_env_token(self) -> None:
        with patch.dict(os.environ, {"LAUNCHPLANE_SERVICE_TOKEN": " env-token "}, clear=True):
            token = _runner_host_hygiene_bearer_token(
                service_url="https://launchplane.example",
                token_env="LAUNCHPLANE_SERVICE_TOKEN",
            )

        self.assertEqual(token, "env-token")

    def test_executor_bearer_token_mints_actions_oidc_token_on_demand(self) -> None:
        class _Response:
            def __enter__(self) -> "_Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            @staticmethod
            def read() -> bytes:
                return b'{"value": "fresh-oidc-token"}'

        captured_urls: list[str] = []
        captured_authorization: list[str | None] = []

        def fake_urlopen(request: Request, timeout: int) -> _Response:
            self.assertEqual(timeout, 30)
            captured_urls.append(getattr(request, "full_url"))
            captured_authorization.append(request.get_header("Authorization"))
            return _Response()

        with (
            patch.dict(
                os.environ,
                {
                    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
                    "ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.actions.example/request?api-version=2",
                },
                clear=True,
            ),
            patch("control_plane.cli_runner_lanes.urlopen", side_effect=fake_urlopen),
        ):
            token = _runner_host_hygiene_bearer_token(
                service_url="https://launchplane.example/base",
                token_env="LAUNCHPLANE_SERVICE_TOKEN",
            )

        self.assertEqual(token, "fresh-oidc-token")
        self.assertEqual(captured_authorization, ["Bearer request-token"])
        self.assertEqual(
            captured_urls,
            ["https://token.actions.example/request?api-version=2&audience=launchplane.example"],
        )


class RunnerHostHygieneApplyPlanTests(unittest.TestCase):
    def test_apply_plan_requires_mutate_audit_healthy_report_and_retained_builders(
        self,
    ) -> None:
        plan = plan_runner_host_hygiene_apply(
            policy=RunnerHostHygieneApplyPolicy(
                approved_hosts=("chris-testing",),
                required_retained_warm_builders=("odoo-docker-chris-testing",),
                allow_docker_cache_prune=True,
            ),
            request=RunnerHostHygieneApplyRequest(
                action="prune_docker_cache",
                host_name="chris-testing",
            ),
            report=_attention_report(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(
            [blocker.code for blocker in plan.blockers],
            [
                "audit_record_required",
                "host_needs_attention",
                "mutate_not_requested",
                "warm_builder_not_retained",
            ],
        )

    def test_apply_plan_requires_approved_host_and_enabled_action(self) -> None:
        plan = plan_runner_host_hygiene_apply(
            policy=RunnerHostHygieneApplyPolicy(approved_hosts=("other-host",)),
            request=RunnerHostHygieneApplyRequest(
                action="restart_runner_service",
                host_name="chris-testing",
                mutate=True,
                audit_record_key="runner-host-hygiene/2026-05-23/chris-testing",
            ),
            report=_healthy_report(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(
            [blocker.code for blocker in plan.blockers],
            ["action_not_enabled", "approved_host_mismatch"],
        )

    def test_apply_plan_can_be_ready_without_executing_host_mutation(self) -> None:
        plan = plan_runner_host_hygiene_apply(
            policy=RunnerHostHygieneApplyPolicy(
                approved_hosts=("Chris-Testing",),
                required_retained_warm_builders=("odoo-docker-chris-testing",),
                allow_docker_cache_prune=True,
            ),
            request=RunnerHostHygieneApplyRequest(
                action="prune_docker_cache",
                host_name=" chris-testing ",
                mutate=True,
                retained_warm_builders=("Odoo-Docker-Chris-Testing",),
                audit_record_key="runner-host-hygiene/2026-05-23/chris-testing",
            ),
            report=_healthy_report(),
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.blockers, ())
        self.assertIn("pre-apply", plan.next_steps[0])

    def test_apply_plan_can_bound_one_allowlisted_buildkit_builder(self) -> None:
        plan = plan_runner_host_hygiene_apply(
            policy=RunnerHostHygieneApplyPolicy(
                approved_hosts=("chris-testing",),
                allow_docker_cache_prune=True,
                allowed_buildkit_builders=("odoo-docker-chris-testing",),
            ),
            request=RunnerHostHygieneApplyRequest(
                action="prune_docker_cache",
                host_name="chris-testing",
                mutate=True,
                target_buildkit_builder="odoo-docker-chris-testing",
                max_used_space_bytes=64_424_509_440,
                audit_record_key="runner-host-hygiene/2026-07-25/chris-testing-builder",
            ),
            report=_healthy_report(),
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.blockers, ())

    def test_apply_plan_can_prune_completed_idle_generated_run_cache(self) -> None:
        plan = plan_runner_host_hygiene_apply(
            policy=RunnerHostHygieneApplyPolicy(
                approved_hosts=("chris-testing",),
                allow_generated_run_cache_prune=True,
                allowed_generated_cache_keys=("codex-lab-runs",),
            ),
            request=RunnerHostHygieneApplyRequest(
                action="prune_generated_run_cache",
                host_name="chris-testing",
                mutate=True,
                target_generated_cache_key="codex-lab-runs",
                target_generated_cache_run_ids=(101,),
                generated_cache_minimum_age_hours=1,
                generated_cache_high_water_bytes=100,
                generated_cache_low_water_bytes=10,
                generated_cache_cooldown_hours=1,
                audit_record_key="runner-host-hygiene/2026-07-27/generated-cache",
            ),
            report=_healthy_report_with_generated_cache(
                RunnerHostHygieneGeneratedCacheUsage(
                    cache_key="codex-lab-runs",
                    apparent_bytes=120,
                    allocated_bytes=120,
                    entry_count=1,
                    owner_valid=True,
                    mode_valid=True,
                    symlink_safe=True,
                    owner_worker_observations=(0, 0),
                    entries=(
                        RunnerHostHygieneGeneratedCacheEntry(
                            run_id=101,
                            apparent_bytes=120,
                            allocated_bytes=120,
                            age_seconds=7_200,
                            open_handle_observations=(0, 0),
                            github_run_state="completed",
                        ),
                    ),
                )
            ),
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.blockers, ())

    def test_apply_plan_blocks_generated_cache_when_owner_is_busy(self) -> None:
        plan = plan_runner_host_hygiene_apply(
            policy=RunnerHostHygieneApplyPolicy(
                approved_hosts=("chris-testing",),
                allow_generated_run_cache_prune=True,
                allowed_generated_cache_keys=("codex-lab-runs",),
            ),
            request=RunnerHostHygieneApplyRequest(
                action="prune_generated_run_cache",
                host_name="chris-testing",
                mutate=True,
                target_generated_cache_key="codex-lab-runs",
                target_generated_cache_run_ids=(101,),
                generated_cache_minimum_age_hours=1,
                generated_cache_high_water_bytes=100,
                generated_cache_low_water_bytes=10,
                generated_cache_cooldown_hours=1,
                audit_record_key="runner-host-hygiene/2026-07-27/generated-cache",
            ),
            report=_healthy_report_with_generated_cache(
                RunnerHostHygieneGeneratedCacheUsage(
                    cache_key="codex-lab-runs",
                    apparent_bytes=120,
                    allocated_bytes=120,
                    entry_count=1,
                    owner_valid=True,
                    mode_valid=True,
                    symlink_safe=True,
                    owner_worker_observations=(1, 1),
                    entries=(
                        RunnerHostHygieneGeneratedCacheEntry(
                            run_id=101,
                            apparent_bytes=120,
                            allocated_bytes=120,
                            age_seconds=7_200,
                            open_handle_observations=(0, 0),
                            github_run_state="completed",
                        ),
                    ),
                )
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(
            [blocker.code for blocker in plan.blockers],
            ["target_generated_cache_owner_busy"],
        )

    def test_apply_plan_blocks_unallowlisted_builder_without_budget(self) -> None:
        plan = plan_runner_host_hygiene_apply(
            policy=RunnerHostHygieneApplyPolicy(
                approved_hosts=("chris-testing",),
                allow_docker_cache_prune=True,
                allowed_buildkit_builders=("odoo-docker-chris-testing",),
            ),
            request=RunnerHostHygieneApplyRequest(
                action="prune_docker_cache",
                host_name="chris-testing",
                mutate=True,
                target_buildkit_builder="unapproved-builder",
                audit_record_key="runner-host-hygiene/2026-07-25/chris-testing-builder",
            ),
            report=_healthy_report(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(
            [blocker.code for blocker in plan.blockers],
            ["target_builder_budget_missing", "target_builder_not_allowlisted"],
        )

    def test_apply_plan_allows_dangling_image_prune_under_explicit_policy(self) -> None:
        plan = plan_runner_host_hygiene_apply(
            policy=RunnerHostHygieneApplyPolicy(
                approved_hosts=("chris-testing",),
                allow_dangling_image_prune=True,
            ),
            request=RunnerHostHygieneApplyRequest(
                action="prune_dangling_images",
                host_name="chris-testing",
                mutate=True,
                audit_record_key="runner-host-hygiene/2026-07-25/chris-testing-images",
            ),
            report=_healthy_report(),
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.blockers, ())

    def test_cache_prune_permission_does_not_enable_image_prune(self) -> None:
        plan = plan_runner_host_hygiene_apply(
            policy=RunnerHostHygieneApplyPolicy(
                approved_hosts=("chris-testing",),
                allow_docker_cache_prune=True,
            ),
            request=RunnerHostHygieneApplyRequest(
                action="prune_dangling_images",
                host_name="chris-testing",
                mutate=True,
                audit_record_key="runner-host-hygiene/2026-07-25/chris-testing-images",
            ),
            report=_healthy_report(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual([blocker.code for blocker in plan.blockers], ["action_not_enabled"])

    def test_apply_plan_can_target_allowlisted_zero_link_buildkit_state_volume(
        self,
    ) -> None:
        target_volume = "buildx_buildkit_launchplane-ci0_state"

        plan = plan_runner_host_hygiene_apply(
            policy=RunnerHostHygieneApplyPolicy(
                approved_hosts=("chris-testing",),
                required_retained_warm_builders=("odoo-docker-chris-testing",),
                allow_buildkit_state_volume_remove=True,
                allowed_buildkit_state_volumes=(target_volume,),
            ),
            request=RunnerHostHygieneApplyRequest(
                action="remove_buildkit_state_volumes",
                host_name="chris-testing",
                mutate=True,
                retained_warm_builders=("odoo-docker-chris-testing",),
                target_buildkit_state_volumes=(target_volume,),
                audit_record_key="runner-host-hygiene/2026-05-24/chris-testing",
            ),
            report=_healthy_report_with_volumes(
                RunnerHostHygieneVolumeInventoryItem(
                    name=target_volume,
                    driver="local",
                    size_bytes=50_490_000_000,
                    dangling=True,
                )
            ),
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.blockers, ())
        self.assertIn("zero-link BuildKit state volumes", plan.next_steps[1])

    def test_apply_plan_blocks_multiple_buildkit_state_volume_targets(self) -> None:
        first_volume = "buildx_buildkit_launchplane-ci0_state"
        second_volume = "buildx_buildkit_verireel-ci0_state"

        plan = plan_runner_host_hygiene_apply(
            policy=RunnerHostHygieneApplyPolicy(
                approved_hosts=("chris-testing",),
                required_retained_warm_builders=("odoo-docker-chris-testing",),
                allow_buildkit_state_volume_remove=True,
                allowed_buildkit_state_volumes=(first_volume, second_volume),
            ),
            request=RunnerHostHygieneApplyRequest(
                action="remove_buildkit_state_volumes",
                host_name="chris-testing",
                mutate=True,
                retained_warm_builders=("odoo-docker-chris-testing",),
                target_buildkit_state_volumes=(first_volume, second_volume),
                audit_record_key="runner-host-hygiene/2026-05-24/chris-testing",
            ),
            report=_healthy_report_with_volumes(
                RunnerHostHygieneVolumeInventoryItem(
                    name=first_volume,
                    driver="local",
                    size_bytes=1,
                    dangling=True,
                ),
                RunnerHostHygieneVolumeInventoryItem(
                    name=second_volume,
                    driver="local",
                    size_bytes=1,
                    dangling=True,
                ),
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertIn(
            "target_volume_multiple_requested",
            [blocker.code for blocker in plan.blockers],
        )

    def test_apply_plan_blocks_repeated_buildkit_state_volume_targets(self) -> None:
        target_volume = "buildx_buildkit_launchplane-ci0_state"

        plan = plan_runner_host_hygiene_apply(
            policy=RunnerHostHygieneApplyPolicy(
                approved_hosts=("chris-testing",),
                required_retained_warm_builders=("odoo-docker-chris-testing",),
                allow_buildkit_state_volume_remove=True,
                allowed_buildkit_state_volumes=(target_volume,),
            ),
            request=RunnerHostHygieneApplyRequest(
                action="remove_buildkit_state_volumes",
                host_name="chris-testing",
                mutate=True,
                retained_warm_builders=("odoo-docker-chris-testing",),
                target_buildkit_state_volumes=(target_volume, target_volume),
                audit_record_key="runner-host-hygiene/2026-05-24/chris-testing",
            ),
            report=_healthy_report_with_volumes(
                RunnerHostHygieneVolumeInventoryItem(
                    name=target_volume,
                    driver="local",
                    size_bytes=1,
                    dangling=True,
                )
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertIn(
            "target_volume_multiple_requested",
            [blocker.code for blocker in plan.blockers],
        )

    def test_apply_request_preserves_repeated_buildkit_state_volume_targets(self) -> None:
        target_volume = "buildx_buildkit_launchplane-ci0_state"

        request = RunnerHostHygieneApplyRequest(
            action="remove_buildkit_state_volumes",
            host_name="chris-testing",
            mutate=True,
            retained_warm_builders=("odoo-docker-chris-testing",),
            target_buildkit_state_volumes=(target_volume, target_volume),
            audit_record_key="runner-host-hygiene/2026-05-24/chris-testing",
        )

        self.assertEqual(request.target_buildkit_state_volumes, (target_volume, target_volume))

    def test_apply_plan_blocks_active_or_unapproved_buildkit_state_volume(
        self,
    ) -> None:
        target_volume = "buildx_buildkit_odoo-docker-chris-testing0_state"

        plan = plan_runner_host_hygiene_apply(
            policy=RunnerHostHygieneApplyPolicy(
                approved_hosts=("chris-testing",),
                required_retained_warm_builders=("odoo-docker-chris-testing",),
                allow_buildkit_state_volume_remove=True,
                allowed_buildkit_state_volumes=("buildx_buildkit_other_state",),
            ),
            request=RunnerHostHygieneApplyRequest(
                action="remove_buildkit_state_volumes",
                host_name="chris-testing",
                mutate=True,
                retained_warm_builders=("odoo-docker-chris-testing",),
                target_buildkit_state_volumes=(target_volume,),
                audit_record_key="runner-host-hygiene/2026-05-24/chris-testing",
            ),
            report=_healthy_report_with_volumes(
                RunnerHostHygieneVolumeInventoryItem(
                    name=target_volume,
                    driver="local",
                    size_bytes=32_480_000_000,
                    referenced_by_containers=1,
                )
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(
            [blocker.code for blocker in plan.blockers],
            ["target_volume_active", "target_volume_not_allowlisted"],
        )

    def test_apply_plan_requires_buildkit_state_volume_target_shape(self) -> None:
        plan = plan_runner_host_hygiene_apply(
            policy=RunnerHostHygieneApplyPolicy(
                approved_hosts=("chris-testing",),
                required_retained_warm_builders=("odoo-docker-chris-testing",),
                allow_buildkit_state_volume_remove=True,
                allowed_buildkit_state_volumes=("runner-cache",),
            ),
            request=RunnerHostHygieneApplyRequest(
                action="remove_buildkit_state_volumes",
                host_name="chris-testing",
                mutate=True,
                retained_warm_builders=("odoo-docker-chris-testing",),
                target_buildkit_state_volumes=("runner-cache",),
                audit_record_key="runner-host-hygiene/2026-05-24/chris-testing",
            ),
            report=_healthy_report_with_volumes(
                RunnerHostHygieneVolumeInventoryItem(
                    name="runner-cache",
                    driver="local",
                    dangling=True,
                )
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertIn(
            "target_volume_not_buildkit_state",
            [blocker.code for blocker in plan.blockers],
        )

    def test_apply_plan_rejects_report_for_different_host(self) -> None:
        plan = plan_runner_host_hygiene_apply(
            policy=RunnerHostHygieneApplyPolicy(
                approved_hosts=("chris-testing",),
                allow_docker_cache_prune=True,
            ),
            request=RunnerHostHygieneApplyRequest(
                action="prune_docker_cache",
                host_name="chris-testing",
                mutate=True,
                audit_record_key="runner-host-hygiene/2026-05-23/chris-testing",
            ),
            report=RunnerHostHygieneReport(
                status="healthy",
                host_name="other-host",
                summary="runner host hygiene satisfies report-only policy",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertIn("report_host_mismatch", [blocker.code for blocker in plan.blockers])

    def test_apply_plan_requires_report_to_observe_retained_warm_builders(self) -> None:
        plan = plan_runner_host_hygiene_apply(
            policy=RunnerHostHygieneApplyPolicy(
                approved_hosts=("chris-testing",),
                required_retained_warm_builders=("odoo-docker-chris-testing",),
                allow_docker_cache_prune=True,
            ),
            request=RunnerHostHygieneApplyRequest(
                action="prune_docker_cache",
                host_name="chris-testing",
                mutate=True,
                retained_warm_builders=("odoo-docker-chris-testing",),
                audit_record_key="runner-host-hygiene/2026-05-23/chris-testing",
            ),
            report=RunnerHostHygieneReport(
                status="healthy",
                host_name="chris-testing",
                summary="runner host hygiene satisfies report-only policy",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertIn(
            "retained_warm_builder_missing_from_report",
            [blocker.code for blocker in plan.blockers],
        )

    def test_apply_plan_requires_report_to_observe_each_request_retained_builder(
        self,
    ) -> None:
        plan = plan_runner_host_hygiene_apply(
            policy=RunnerHostHygieneApplyPolicy(
                approved_hosts=("chris-testing",),
                required_retained_warm_builders=("odoo-docker-chris-testing",),
                allow_docker_cache_prune=True,
            ),
            request=RunnerHostHygieneApplyRequest(
                action="prune_docker_cache",
                host_name="chris-testing",
                mutate=True,
                retained_warm_builders=(
                    "odoo-docker-chris-testing",
                    "odoo-enterprise-chris-testing",
                ),
                audit_record_key="runner-host-hygiene/2026-05-23/chris-testing",
            ),
            report=_healthy_report(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertIn(
            "retained_warm_builder_missing_from_report",
            [blocker.code for blocker in plan.blockers],
        )

    def test_apply_audit_record_requires_matching_key(self) -> None:
        request = RunnerHostHygieneApplyRequest(
            action="prune_docker_cache",
            host_name="chris-testing",
            mutate=True,
            audit_record_key="runner-host-hygiene/2026-05-23/chris-testing",
        )
        plan = plan_runner_host_hygiene_apply(
            policy=RunnerHostHygieneApplyPolicy(
                approved_hosts=("chris-testing",), allow_docker_cache_prune=True
            ),
            request=request,
            report=_healthy_report(),
        )

        with self.assertRaisesRegex(ValueError, "key must match request"):
            RunnerHostHygieneApplyAuditRecord(
                audit_record_key="runner-host-hygiene/other",
                status="planned",
                request=request,
                plan=plan,
                pre_apply_report=_healthy_report(),
            )

    def test_apply_audit_record_requires_matching_plan_audit_key(self) -> None:
        request = RunnerHostHygieneApplyRequest(
            action="prune_docker_cache",
            host_name="chris-testing",
            mutate=True,
            audit_record_key="runner-host-hygiene/2026-05-23/chris-testing",
        )
        plan = plan_runner_host_hygiene_apply(
            policy=RunnerHostHygieneApplyPolicy(
                approved_hosts=("chris-testing",), allow_docker_cache_prune=True
            ),
            request=RunnerHostHygieneApplyRequest(
                action="prune_docker_cache",
                host_name="chris-testing",
                mutate=True,
                audit_record_key="runner-host-hygiene/other",
            ),
            report=_healthy_report(),
        )

        with self.assertRaisesRegex(ValueError, "plan key must match request"):
            RunnerHostHygieneApplyAuditRecord(
                audit_record_key=request.audit_record_key,
                status="planned",
                request=request,
                plan=plan,
                pre_apply_report=_healthy_report(),
            )

    def test_apply_audit_record_requires_plan_action_and_mutate_to_match_request(
        self,
    ) -> None:
        request = RunnerHostHygieneApplyRequest(
            action="prune_docker_cache",
            host_name="chris-testing",
            mutate=True,
            audit_record_key="runner-host-hygiene/2026-05-23/chris-testing",
        )
        healthy_report = _healthy_report()

        with self.assertRaisesRegex(ValueError, "plan must match request action"):
            RunnerHostHygieneApplyAuditRecord(
                audit_record_key=request.audit_record_key,
                status="planned",
                request=request,
                plan=plan_runner_host_hygiene_apply(
                    policy=RunnerHostHygieneApplyPolicy(
                        approved_hosts=("chris-testing",), allow_runner_service_restart=True
                    ),
                    request=RunnerHostHygieneApplyRequest(
                        action="restart_runner_service",
                        host_name="chris-testing",
                        mutate=True,
                        audit_record_key=request.audit_record_key,
                    ),
                    report=healthy_report,
                ),
                pre_apply_report=healthy_report,
            )

        with self.assertRaisesRegex(ValueError, "plan must match request mutate intent"):
            RunnerHostHygieneApplyAuditRecord(
                audit_record_key=request.audit_record_key,
                status="planned",
                request=request,
                plan=plan_runner_host_hygiene_apply(
                    policy=RunnerHostHygieneApplyPolicy(
                        approved_hosts=("chris-testing",), allow_docker_cache_prune=True
                    ),
                    request=RunnerHostHygieneApplyRequest(
                        action="prune_docker_cache",
                        host_name="chris-testing",
                        audit_record_key=request.audit_record_key,
                    ),
                    report=healthy_report,
                ),
                pre_apply_report=healthy_report,
            )

    def test_apply_audit_record_requires_matching_host_for_reports(self) -> None:
        request = RunnerHostHygieneApplyRequest(
            action="prune_docker_cache",
            host_name="chris-testing",
            mutate=True,
            audit_record_key="runner-host-hygiene/2026-05-23/chris-testing",
        )
        plan = plan_runner_host_hygiene_apply(
            policy=RunnerHostHygieneApplyPolicy(
                approved_hosts=("chris-testing",), allow_docker_cache_prune=True
            ),
            request=request,
            report=_healthy_report(),
        )

        with self.assertRaisesRegex(ValueError, "pre-apply report must match request host"):
            RunnerHostHygieneApplyAuditRecord(
                audit_record_key=request.audit_record_key,
                status="planned",
                request=request,
                plan=plan,
                pre_apply_report=RunnerHostHygieneReport(
                    status="healthy",
                    host_name="other-host",
                    summary="runner host hygiene satisfies report-only policy",
                ),
            )

    def test_apply_audit_record_requires_completed_post_apply_report(self) -> None:
        request = RunnerHostHygieneApplyRequest(
            action="prune_docker_cache",
            host_name="chris-testing",
            mutate=True,
            audit_record_key="runner-host-hygiene/2026-05-23/chris-testing",
        )
        plan = plan_runner_host_hygiene_apply(
            policy=RunnerHostHygieneApplyPolicy(
                approved_hosts=("chris-testing",), allow_docker_cache_prune=True
            ),
            request=request,
            report=_healthy_report(),
        )

        with self.assertRaisesRegex(ValueError, "requires post-apply report"):
            RunnerHostHygieneApplyAuditRecord(
                audit_record_key=request.audit_record_key,
                status="completed",
                request=request,
                plan=plan,
                pre_apply_report=_healthy_report(),
            )

    def test_failed_apply_audit_record_can_record_missing_post_evidence(self) -> None:
        request = RunnerHostHygieneApplyRequest(
            action="prune_docker_cache",
            host_name="chris-testing",
            mutate=True,
            audit_record_key="runner-host-hygiene/2026-07-25/chris-testing",
        )
        plan = plan_runner_host_hygiene_apply(
            policy=RunnerHostHygieneApplyPolicy(
                approved_hosts=("chris-testing",), allow_docker_cache_prune=True
            ),
            request=request,
            report=_healthy_report(),
        )

        audit = RunnerHostHygieneApplyAuditRecord(
            audit_record_key=request.audit_record_key,
            status="failed",
            request=request,
            plan=plan,
            pre_apply_report=_healthy_report(),
            message="terminal evidence collection failed",
        )

        self.assertIsNone(audit.post_apply_report)

    def test_apply_audit_record_requires_terminal_ready_plan(self) -> None:
        request = RunnerHostHygieneApplyRequest(
            action="prune_docker_cache",
            host_name="chris-testing",
            mutate=True,
            audit_record_key="runner-host-hygiene/2026-05-23/chris-testing",
        )
        healthy_report = _healthy_report()

        with self.assertRaisesRegex(ValueError, "requires a ready plan"):
            RunnerHostHygieneApplyAuditRecord(
                audit_record_key=request.audit_record_key,
                status="completed",
                request=request,
                plan=plan_runner_host_hygiene_apply(
                    policy=RunnerHostHygieneApplyPolicy(approved_hosts=("chris-testing",)),
                    request=RunnerHostHygieneApplyRequest(
                        action="prune_docker_cache",
                        host_name="chris-testing",
                        mutate=True,
                        audit_record_key=request.audit_record_key,
                    ),
                    report=_attention_report(),
                ),
                pre_apply_report=healthy_report,
                post_apply_report=healthy_report,
            )


class RunnerHostHygieneAdapterBoundaryTests(unittest.TestCase):
    def test_adapter_boundary_can_be_ready_for_narrow_docker_prune(self) -> None:
        apply_plan = _ready_apply_plan()

        boundary = plan_runner_host_hygiene_adapter_boundary(
            policy=_adapter_policy(),
            proposal=RunnerHostHygieneAdapterProposal(
                adapter_type="github_actions_runner",
                host_name="Chris-Testing",
                execution_lane="chris-testing-ops-gate",
                service_user="gha",
                repository_scopes=("Cbusillo/Launchplane",),
                privileged_scopes=("docker_cache",),
                audit_record_key=apply_plan.audit_record_key,
                rollback_plan="Stop if retained builders are missing after pre-apply evidence.",
                pre_apply_evidence=("df", "docker_summary", "warm_builders"),
                post_apply_evidence=("df", "docker_summary", "warm_builders"),
            ),
            apply_plan=apply_plan,
        )

        self.assertEqual(boundary.status, "ready")
        self.assertEqual(boundary.blockers, ())
        self.assertEqual(boundary.execution_lane, "chris-testing-ops-gate")
        self.assertIn("approved execution lane", boundary.next_steps[1])

    def test_adapter_boundary_blocks_until_apply_plan_is_ready(self) -> None:
        blocked_apply_plan = plan_runner_host_hygiene_apply(
            policy=RunnerHostHygieneApplyPolicy(approved_hosts=("chris-testing",)),
            request=RunnerHostHygieneApplyRequest(
                action="prune_docker_cache",
                host_name="chris-testing",
                mutate=True,
                audit_record_key="runner-host-hygiene/2026-05-23/chris-testing",
            ),
            report=_healthy_report(),
        )

        boundary = plan_runner_host_hygiene_adapter_boundary(
            policy=_adapter_policy(),
            proposal=_adapter_proposal(blocked_apply_plan),
            apply_plan=blocked_apply_plan,
        )

        self.assertEqual(boundary.status, "blocked")
        self.assertIn("apply_plan_not_ready", [blocker.code for blocker in boundary.blockers])

    def test_adapter_boundary_rejects_overbroad_privileged_scope(self) -> None:
        apply_plan = _ready_apply_plan()

        boundary = plan_runner_host_hygiene_adapter_boundary(
            policy=_adapter_policy(),
            proposal=RunnerHostHygieneAdapterProposal(
                adapter_type="github_actions_runner",
                host_name="chris-testing",
                execution_lane="chris-testing-ops-gate",
                service_user="gha",
                repository_scopes=("cbusillo/launchplane",),
                privileged_scopes=("docker_cache", "runner_service"),
                audit_record_key=apply_plan.audit_record_key,
                rollback_plan="Stop if post-apply evidence cannot be collected.",
                pre_apply_evidence=("df", "docker_summary", "warm_builders"),
                post_apply_evidence=("df", "docker_summary", "warm_builders"),
            ),
            apply_plan=apply_plan,
        )

        self.assertEqual(boundary.status, "blocked")
        self.assertIn("privileged_scope_overbroad", [blocker.code for blocker in boundary.blockers])

    def test_adapter_boundary_requires_repository_scope_evidence_and_rollback(self) -> None:
        apply_plan = _ready_apply_plan()

        boundary = plan_runner_host_hygiene_adapter_boundary(
            policy=_adapter_policy(),
            proposal=RunnerHostHygieneAdapterProposal(
                adapter_type="github_actions_runner",
                host_name="chris-testing",
                execution_lane="chris-testing-ops-gate",
                service_user="gha",
                privileged_scopes=("docker_cache",),
                audit_record_key=apply_plan.audit_record_key,
                pre_apply_evidence=("df",),
                post_apply_evidence=("df",),
            ),
            apply_plan=apply_plan,
        )

        self.assertEqual(boundary.status, "blocked")
        self.assertEqual(
            [blocker.code for blocker in boundary.blockers],
            [
                "post_apply_evidence_missing",
                "pre_apply_evidence_missing",
                "repository_scope_required",
                "rollback_plan_required",
            ],
        )


class RunnerHostHygieneApplyPlanCliTests(unittest.TestCase):
    def test_cli_builds_apply_plan_from_report_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            report_file = Path(temp_dir) / "report.json"
            report_file.write_text(
                json.dumps({"report": _healthy_report().model_dump(mode="json")}),
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "runner-host-hygiene-apply-plan",
                    "--action",
                    "prune_docker_cache",
                    "--host-name",
                    "chris-testing",
                    "--mutate",
                    "--audit-record-key",
                    "runner-host-hygiene/2026-05-23/chris-testing",
                    "--approved-host",
                    "chris-testing",
                    "--allow-docker-cache-prune",
                    "--required-retained-warm-builder",
                    "odoo-docker-chris-testing",
                    "--retained-warm-builder",
                    "odoo-docker-chris-testing",
                    "--report-file",
                    report_file.as_posix(),
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["plan"]["status"], "ready")
        self.assertEqual(payload["audit_record"]["status"], "planned")
        self.assertEqual(
            payload["audit_record"]["message"],
            "planned runner host hygiene apply; no host mutation was executed",
        )

    def test_cli_builds_allowlisted_buildx_apply_plan(self) -> None:
        with TemporaryDirectory() as temp_dir:
            report_file = Path(temp_dir) / "report.json"
            report_file.write_text(
                json.dumps({"report": _healthy_report().model_dump(mode="json")}),
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "runner-host-hygiene-apply-plan",
                    "--action",
                    "prune_docker_cache",
                    "--host-name",
                    "chris-testing",
                    "--mutate",
                    "--audit-record-key",
                    "runner-host-hygiene/2026-07-26/chris-testing",
                    "--approved-host",
                    "chris-testing",
                    "--allow-docker-cache-prune",
                    "--target-buildkit-builder",
                    "odoo-docker-chris-testing",
                    "--allowed-buildkit-builder",
                    "odoo-docker-chris-testing",
                    "--max-used-space-bytes",
                    "64424509440",
                    "--retained-warm-builder",
                    "odoo-docker-chris-testing",
                    "--report-file",
                    report_file.as_posix(),
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["plan"]["status"], "ready")
        self.assertEqual(
            payload["request"]["target_buildkit_builder"],
            "odoo-docker-chris-testing",
        )
        self.assertEqual(payload["request"]["max_used_space_bytes"], 64_424_509_440)

    def test_cli_builds_generated_run_cache_apply_plan(self) -> None:
        generated_usage = RunnerHostHygieneGeneratedCacheUsage(
            cache_key="codex-lab-runs",
            apparent_bytes=120,
            allocated_bytes=120,
            entry_count=1,
            owner_valid=True,
            mode_valid=True,
            symlink_safe=True,
            owner_worker_observations=(0, 0),
            entries=(
                RunnerHostHygieneGeneratedCacheEntry(
                    run_id=101,
                    apparent_bytes=120,
                    allocated_bytes=120,
                    age_seconds=7_200,
                    open_handle_observations=(0, 0),
                    github_run_state="completed",
                ),
            ),
        )
        with TemporaryDirectory() as temp_dir:
            report_file = Path(temp_dir) / "report.json"
            report_file.write_text(
                json.dumps(
                    {
                        "report": _healthy_report_with_generated_cache(generated_usage).model_dump(
                            mode="json"
                        )
                    }
                ),
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "runner-host-hygiene-apply-plan",
                    "--action",
                    "prune_generated_run_cache",
                    "--host-name",
                    "chris-testing",
                    "--mutate",
                    "--audit-record-key",
                    "runner-host-hygiene/2026-07-27/generated-cache",
                    "--approved-host",
                    "chris-testing",
                    "--allow-generated-run-cache-prune",
                    "--allowed-generated-cache-key",
                    "codex-lab-runs",
                    "--target-generated-cache-key",
                    "codex-lab-runs",
                    "--target-generated-cache-run-id",
                    "101",
                    "--generated-cache-minimum-age-hours",
                    "1",
                    "--generated-cache-high-water-bytes",
                    "100",
                    "--generated-cache-low-water-bytes",
                    "10",
                    "--generated-cache-cooldown-hours",
                    "1",
                    "--report-file",
                    report_file.as_posix(),
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["plan"]["status"], "ready")
        self.assertEqual(payload["request"]["target_generated_cache_run_ids"], [101])

    def test_cli_builds_dangling_image_apply_plan(self) -> None:
        with TemporaryDirectory() as temp_dir:
            report_file = Path(temp_dir) / "report.json"
            report_file.write_text(
                json.dumps({"report": _healthy_report().model_dump(mode="json")}),
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "runner-host-hygiene-apply-plan",
                    "--action",
                    "prune_dangling_images",
                    "--host-name",
                    "chris-testing",
                    "--mutate",
                    "--audit-record-key",
                    "runner-host-hygiene/2026-07-26/chris-testing-images",
                    "--approved-host",
                    "chris-testing",
                    "--allow-dangling-image-prune",
                    "--retained-warm-builder",
                    "odoo-docker-chris-testing",
                    "--report-file",
                    report_file.as_posix(),
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["plan"]["status"], "ready")
        self.assertEqual(payload["request"]["action"], "prune_dangling_images")

    def test_cli_builds_adapter_boundary_from_apply_plan_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            apply_plan = _ready_apply_plan()
            apply_plan_file = Path(temp_dir) / "apply-plan.json"
            apply_plan_file.write_text(
                json.dumps({"plan": apply_plan.model_dump(mode="json")}),
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "work-graph",
                    "runner-host-hygiene-adapter-boundary-plan",
                    "--adapter-type",
                    "github_actions_runner",
                    "--host-name",
                    "chris-testing",
                    "--execution-lane",
                    "chris-testing-ops-gate",
                    "--service-user",
                    "gha",
                    "--repository-scope",
                    "cbusillo/launchplane",
                    "--privileged-scope",
                    "docker_cache",
                    "--audit-record-key",
                    apply_plan.audit_record_key,
                    "--rollback-plan",
                    "Stop before mutation if retained builders are absent.",
                    "--pre-apply-evidence",
                    "df",
                    "--pre-apply-evidence",
                    "docker_summary",
                    "--pre-apply-evidence",
                    "warm_builders",
                    "--post-apply-evidence",
                    "df",
                    "--post-apply-evidence",
                    "docker_summary",
                    "--post-apply-evidence",
                    "warm_builders",
                    "--approved-host",
                    "chris-testing",
                    "--allowed-adapter-type",
                    "github_actions_runner",
                    "--allowed-execution-lane",
                    "chris-testing-ops-gate",
                    "--allowed-service-user",
                    "gha",
                    "--allowed-repository-scope",
                    "cbusillo/launchplane",
                    "--allowed-privileged-scope",
                    "docker_cache",
                    "--required-pre-apply-evidence",
                    "df",
                    "--required-pre-apply-evidence",
                    "docker_summary",
                    "--required-pre-apply-evidence",
                    "warm_builders",
                    "--required-post-apply-evidence",
                    "df",
                    "--required-post-apply-evidence",
                    "docker_summary",
                    "--required-post-apply-evidence",
                    "warm_builders",
                    "--apply-plan-file",
                    apply_plan_file.as_posix(),
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["mode"], "adapter-boundary-plan")
        self.assertEqual(payload["boundary"]["status"], "ready")
        self.assertEqual(payload["proposal"]["privileged_scopes"], ["docker_cache"])


def _healthy_report() -> RunnerHostHygieneReport:
    return _healthy_report_with_volumes()


def _healthy_report_with_volumes(
    *volumes: RunnerHostHygieneVolumeInventoryItem,
) -> RunnerHostHygieneReport:
    return evaluate_runner_host_hygiene(
        policy=RunnerHostHygienePolicy(required_warm_builders=("odoo-docker-chris-testing",)),
        observation=RunnerHostHygieneObservation(
            host_name="chris-testing",
            observed_at="2026-05-23T13:00:00Z",
            free_disk_bytes=500,
            warm_builders=("odoo-docker-chris-testing",),
            volume_inventory=volumes,
        ),
    )


def _healthy_report_with_generated_cache(
    usage: RunnerHostHygieneGeneratedCacheUsage,
) -> RunnerHostHygieneReport:
    return evaluate_runner_host_hygiene(
        policy=RunnerHostHygienePolicy(),
        observation=RunnerHostHygieneObservation(
            host_name="chris-testing",
            observed_at="2026-07-27T00:00:00Z",
            free_disk_bytes=500,
            generated_cache_apparent_bytes=usage.apparent_bytes,
            generated_cache_allocated_bytes=usage.allocated_bytes,
            generated_cache_usage=(usage,),
        ),
    )


def _attention_report() -> RunnerHostHygieneReport:
    return evaluate_runner_host_hygiene(
        policy=RunnerHostHygienePolicy(minimum_free_disk_bytes=500),
        observation=RunnerHostHygieneObservation(
            host_name="chris-testing",
            observed_at="2026-05-23T13:00:00Z",
            free_disk_bytes=100,
        ),
    )


def _ready_apply_plan() -> RunnerHostHygieneApplyPlan:
    return plan_runner_host_hygiene_apply(
        policy=RunnerHostHygieneApplyPolicy(
            approved_hosts=("chris-testing",),
            required_retained_warm_builders=("odoo-docker-chris-testing",),
            allow_docker_cache_prune=True,
        ),
        request=RunnerHostHygieneApplyRequest(
            action="prune_docker_cache",
            host_name="chris-testing",
            mutate=True,
            retained_warm_builders=("odoo-docker-chris-testing",),
            audit_record_key="runner-host-hygiene/2026-05-23/chris-testing",
        ),
        report=_healthy_report(),
    )


def _adapter_policy() -> RunnerHostHygieneAdapterPolicy:
    return RunnerHostHygieneAdapterPolicy(
        approved_hosts=("chris-testing",),
        allowed_adapter_types=("github_actions_runner",),
        allowed_execution_lanes=("chris-testing-ops-gate",),
        allowed_service_users=("gha",),
        allowed_repository_scopes=("cbusillo/launchplane",),
        allowed_privileged_scopes=("docker_cache",),
        required_pre_apply_evidence=("df", "docker_summary", "warm_builders"),
        required_post_apply_evidence=("df", "docker_summary", "warm_builders"),
    )


def _adapter_proposal(apply_plan: RunnerHostHygieneApplyPlan) -> RunnerHostHygieneAdapterProposal:
    return RunnerHostHygieneAdapterProposal(
        adapter_type="github_actions_runner",
        host_name="chris-testing",
        execution_lane="chris-testing-ops-gate",
        service_user="gha",
        repository_scopes=("cbusillo/launchplane",),
        privileged_scopes=("docker_cache",),
        audit_record_key=apply_plan.audit_record_key,
        rollback_plan="Stop if retained builders are missing after pre-apply evidence.",
        pre_apply_evidence=("df", "docker_summary", "warm_builders"),
        post_apply_evidence=("df", "docker_summary", "warm_builders"),
    )


def _empty_docker_disk_usage() -> str:
    return json.dumps(
        {
            "BuildCache": [],
            "Containers": [],
            "Images": [],
            "LayersSize": 0,
            "Volumes": [],
        }
    )


if __name__ == "__main__":
    unittest.main()
