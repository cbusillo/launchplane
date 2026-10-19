from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneReport
from control_plane.workflows.runner_host_hygiene_audit_spool import RunnerHostHygieneAuditSpool
from control_plane.workflows.runner_host_hygiene_executor import (
    RemoteCommandResult,
    RunnerHostHygieneExecutorRequest,
    collect_runner_host_hygiene_report,
    execute_runner_host_hygiene_executor,
    _parse_builder_cache_usage,
    _executor_intent_sha256,
)
from control_plane.workflows.runner_host_hygiene_qualification import qualify_runner_host_hygiene
from tests.test_runner_host_hygiene_executor import (
    _AuditPoster,
    _CommandRunner,
    _CrashingCommandRunner,
    _docker_disk_usage_json,
    _github_idle_evidence_reader,
    _request,
)


class ReportRunner(_CommandRunner):
    def __init__(
        self,
        *,
        usage: str = "[]",
        extra_builder: bool = False,
        runner_service_output: str = "launchplane-runner@ops-gate.service active running\n",
        container_rows: list[dict[str, str]] | None = None,
        daemon_records: list[dict[str, object]] | None = None,
    ) -> None:
        containers = [
            {"Names": "buildx_buildkit_builder-a0", "State": "running", "Image": "buildkit"}
        ]
        if extra_builder:
            containers.append(
                {"Names": "buildx_buildkit_other-product0", "State": "running", "Image": "buildkit"}
            )
        if container_rows is not None:
            containers = container_rows
        super().__init__(
            docker_disk_usage=_docker_disk_usage_json(
                build_cache=daemon_records
                if daemon_records is not None
                else [{"ID": "daemon", "Size": 10, "InUse": False, "Shared": False}]
            ),
            container_inventory="\n".join(json.dumps(row) for row in containers),
            runner_service_output=runner_service_output,
        )
        self.usage = usage

    def __call__(self, command: Sequence[str], timeout: int) -> RemoteCommandResult:
        if tuple(command)[3:] == ("buildctl", "du", "--format", "{{json .}}"):
            self.commands.append(tuple(command))
            return RemoteCommandResult(0, self.usage)
        return super().__call__(command, timeout)


def request_with_budgets(*, mutate: bool = False) -> RunnerHostHygieneExecutorRequest:
    request = _request(mutate=mutate)
    return RunnerHostHygieneExecutorRequest.model_validate(
        {
            **request.model_dump(),
            "buildkit_cache_budgets": {"default": 8 * 1024**3, "builder-a": 100},
        }
    )


def report_for(runner: ReportRunner) -> RunnerHostHygieneReport:
    return collect_runner_host_hygiene_report(
        request=request_with_budgets(),
        remote_runner=runner,
        github_idle_evidence_reader=_github_idle_evidence_reader,
        idle_observation_sleeper=lambda _: None,
    )


class HygieneQualificationTests(unittest.TestCase):
    def test_complete_fleet_and_measured_builder_zero_qualify(self) -> None:
        report = report_for(ReportRunner())
        self.assertEqual(report.status, "healthy")
        named = next(item for item in report.builder_cache_usage if item.cache_key == "builder-a")
        self.assertEqual(named.logical_bytes, 0)
        self.assertEqual(named.qualification, "within_target")

    def test_added_omitted_service_blocks_real_sunday_action_after_accepted_plan(self) -> None:
        request = request_with_budgets(mutate=True)
        runner = ReportRunner(
            runner_service_output=(
                "launchplane-runner@ops-gate.service active running\n"
                "launchplane-runner@added.service active running\n"
            )
        )
        poster = _AuditPoster()
        with tempfile.TemporaryDirectory() as root:
            spool = RunnerHostHygieneAuditSpool(root=Path(root))
            result = execute_runner_host_hygiene_executor(
                request=request,
                remote_runner=runner,
                audit_poster=poster,
                audit_spool=spool,
                github_idle_evidence_reader=_github_idle_evidence_reader,
                audit_delivery_sleeper=lambda _: None,
            )
            self.assertEqual(result.status, "blocked")
            self.assertFalse(result.audit_delivery_pending)
            self.assertEqual([audit.status for audit in poster.audits], ["planned"])
            self.assertFalse(any(command[0] == "flock" for command in runner.commands))
            envelope = spool.read(
                host_name=request.host_name,
                action=request.action,
                audit_record_key=request.audit_record_key,
            )
            assert envelope is not None
            qualification = qualify_runner_host_hygiene(
                envelope, as_of=datetime.now(timezone.utc), maximum_age_seconds=3600
            )
            self.assertFalse(qualification["qualified"])
            self.assertIn("idle_evidence_conflicting", qualification["findings"])

    def test_changed_retained_image_naming_cannot_be_replaced_by_live_builder(self) -> None:
        runner = ReportRunner()
        original = runner.__call__

        def command(command: Sequence[str], timeout: int) -> RemoteCommandResult:
            if tuple(command)[:3] == ("docker", "image", "inspect"):
                return RemoteCommandResult(1)
            return original(command, timeout)

        report = collect_runner_host_hygiene_report(
            request=request_with_budgets(),
            remote_runner=command,
            github_idle_evidence_reader=_github_idle_evidence_reader,
            idle_observation_sleeper=lambda _: None,
        )
        self.assertIn("required_warm_builder_missing", {item.code for item in report.findings})
        # Accept only the exact reference emitted by the changed producer.
        changed = request_with_budgets().model_copy(
            update={"retained_warm_builders": ("example:verify-123-1-runtime",)}
        )
        report = collect_runner_host_hygiene_report(
            request=changed,
            remote_runner=lambda argv, timeout: (
                RemoteCommandResult(0 if argv[-1] == changed.retained_warm_builders[0] else 1)
                if tuple(argv)[:3] == ("docker", "image", "inspect")
                else original(argv, timeout)
            ),
            github_idle_evidence_reader=_github_idle_evidence_reader,
            idle_observation_sleeper=lambda _: None,
        )
        self.assertEqual(report.warm_builders, changed.retained_warm_builders)

    def test_unavailable_and_truncated_builder_measurements_are_not_zero(self) -> None:
        for usage in ("", '[{"id":"partial"}', '[{"id":"partial"}]'):
            with self.subTest(usage=usage):
                report = report_for(ReportRunner(usage=usage))
                self.assertEqual(report.status, "attention")
                row = next(
                    item for item in report.builder_cache_usage if item.cache_key == "builder-a"
                )
                self.assertIsNone(row.logical_bytes)
                self.assertEqual(row.qualification, "unknown")
                self.assertIn("builder_cache_unqualified", {item.code for item in report.findings})

    def test_protected_cache_explains_excess_without_estimating_reclaim(self) -> None:
        now = datetime.now(timezone.utc)
        record = {
            "id": "cache",
            "size": 200,
            "inUse": False,
            "shared": False,
            "createdAt": (now - timedelta(days=10)).isoformat(),
            "lastUsedAt": (now - timedelta(days=1)).isoformat(),
        }
        for cause in ("recent", "inUse", "shared", "none"):
            with self.subTest(cause=cause):
                row = dict(record)
                if cause != "recent":
                    row["lastUsedAt"] = row["createdAt"]
                if cause in {"inUse", "shared"}:
                    row[cause] = True
                report = report_for(ReportRunner(usage=json.dumps([row])))
                measured = next(
                    item for item in report.builder_cache_usage if item.cache_key == "builder-a"
                )
                self.assertEqual(measured.logical_bytes, record["size"])
                self.assertEqual(
                    measured.qualification,
                    "maintenance_required" if cause in {"none", "shared"} else "protected_cache",
                )
                self.assertEqual(bool(report.findings), cause in {"none", "shared"})
                self.assertTrue(measured.recovery_owner)

    def test_unbudgeted_builder_is_report_only_without_adding_cleanup_authority(self) -> None:
        report = report_for(ReportRunner(extra_builder=True))
        extra = next(item for item in report.builder_cache_usage if item.target_bytes is None)
        self.assertIsNone(extra.target_bytes)
        self.assertEqual(extra.qualification, "unbudgeted_report_only")
        self.assertTrue(extra.recovery_owner)

    def test_report_budget_must_agree_with_the_action_target(
        self,
    ) -> None:
        request = request_with_budgets()
        with self.assertRaises(ValueError):
            RunnerHostHygieneExecutorRequest.model_validate(
                {**request.model_dump(), "max_used_space_bytes": 9 * 1024**3}
            )

    def test_parser_rejects_duplicate_negative_and_missing_age_evidence(self) -> None:
        now = datetime.now(timezone.utc)
        record = {
            "id": "one",
            "size": 1,
            "inUse": False,
            "shared": False,
            "createdAt": now.isoformat(),
        }
        for records in (
            [record, record],
            [{**record, "size": -1}],
            [{**record, "createdAt": None}],
        ):
            with self.subTest(records=records), self.assertRaises(ValueError):
                _parse_builder_cache_usage(
                    json.dumps(records), observed_at=now.isoformat(), minimum_age_hours=168
                )

    def test_delivered_dry_run_is_not_completed_and_artifact_cli_replays_as_of(self) -> None:
        request = request_with_budgets()
        with tempfile.TemporaryDirectory() as root:
            artifact, summary = Path(root) / "audit.json", Path(root) / "summary.md"
            spool = RunnerHostHygieneAuditSpool(root=Path(root) / "spool", artifact_file=artifact)
            result = execute_runner_host_hygiene_executor(
                request=request,
                remote_runner=ReportRunner(),
                audit_poster=_AuditPoster(),
                audit_spool=spool,
                github_idle_evidence_reader=_github_idle_evidence_reader,
                audit_delivery_sleeper=lambda _: None,
            )
            self.assertEqual(result.status, "blocked")
            envelope = spool.read(
                host_name=request.host_name,
                action=request.action,
                audit_record_key=request.audit_record_key,
            )
            assert envelope is not None
            as_of = datetime.fromisoformat(envelope.planned_audit.pre_apply_report.observed_at)
            qualified = qualify_runner_host_hygiene(envelope, as_of=as_of, maximum_age_seconds=1)
            self.assertTrue(qualified["qualified"])
            self.assertFalse(qualified["maintenance_completed"])
            args = [
                "work-graph",
                "runner-host-hygiene-qualify",
                "--audit-file",
                str(artifact),
                "--as-of",
                as_of.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "--summary-file",
                str(summary),
                "--require-qualified",
            ]
            # Seconds precision remains after the artifact timestamp's microseconds.
            args[args.index("--as-of") + 1] = (as_of + timedelta(seconds=1)).strftime(
                "%Y-%m-%dT%H:%M:%S%z"
            )
            response = CliRunner().invoke(main, args)
            self.assertEqual(response.exit_code, 0, response.output)
            rendered = dict(
                line.removeprefix("- ").split(": ", 1)
                for line in summary.read_text().splitlines()
                if line.startswith("- ")
            )
            self.assertEqual(
                rendered["maintenance_completed"], f"`{qualified['maintenance_completed']}`"
            )
            self.assertEqual(response.output, "")
            stale = qualify_runner_host_hygiene(
                envelope, as_of=as_of + timedelta(days=1), maximum_age_seconds=1
            )
            self.assertFalse(stale["qualified"])

    def test_accepted_terminal_and_replay_remain_complete_without_claiming_retention(self) -> None:
        request = _request(mutate=True)
        with tempfile.TemporaryDirectory() as root:
            spool = RunnerHostHygieneAuditSpool(root=Path(root))
            for replay in (False, True):
                runner = _CommandRunner()
                result = execute_runner_host_hygiene_executor(
                    request=request,
                    remote_runner=runner,
                    audit_poster=_AuditPoster(),
                    audit_spool=spool,
                    github_idle_evidence_reader=_github_idle_evidence_reader,
                    audit_delivery_sleeper=lambda _: None,
                )
                self.assertEqual(result.status, "completed")
                if replay:
                    self.assertFalse(runner.commands)
            envelope = spool.read(
                host_name=request.host_name,
                action=request.action,
                audit_record_key=request.audit_record_key,
            )
            assert envelope is not None
            qualified = qualify_runner_host_hygiene(
                envelope, as_of=datetime.now(timezone.utc), maximum_age_seconds=3600
            )
            self.assertTrue(qualified["maintenance_completed"])
            self.assertFalse(qualified["retention_accounted_for"])
            for field in ("planned_delivery_state", "terminal_delivery_state"):
                pending = envelope.model_copy(update={field: "pending"})
                self.assertFalse(
                    qualify_runner_host_hygiene(
                        pending, as_of=datetime.now(timezone.utc), maximum_age_seconds=3600
                    )["maintenance_completed"]
                )

    def test_report_budgets_do_not_authorize_an_unallowlisted_mutation(self) -> None:
        request = RunnerHostHygieneExecutorRequest.model_validate(
            {
                **request_with_budgets(mutate=True).model_dump(),
                "target_buildkit_builder": "builder-a",
                "max_used_space_bytes": 100,
            }
        )
        poster, runner = _AuditPoster(), ReportRunner()
        with tempfile.TemporaryDirectory() as root:
            result = execute_runner_host_hygiene_executor(
                request=request,
                remote_runner=runner,
                audit_poster=poster,
                audit_spool=RunnerHostHygieneAuditSpool(root=Path(root)),
                github_idle_evidence_reader=_github_idle_evidence_reader,
                audit_delivery_sleeper=lambda _: None,
            )
            self.assertEqual(result.status, "blocked")
            self.assertIn(
                "target_builder_not_allowlisted",
                {item.code for item in poster.audits[0].plan.blockers},
            )
            self.assertFalse(any(command[0] == "flock" for command in runner.commands))

    def test_report_budgets_never_change_action_recovery_identity(self) -> None:
        request = _request(mutate=True, max_used_space_bytes=8 * 1024**3)
        updated = request.model_copy(
            update={
                "buildkit_cache_budgets": {"default": request.max_used_space_bytes, "unrelated": 10}
            }
        )
        self.assertEqual(_executor_intent_sha256(request), _executor_intent_sha256(updated))
        changed_report = updated.model_copy(update={"buildkit_cache_budgets": {"unrelated": 20}})
        self.assertEqual(_executor_intent_sha256(updated), _executor_intent_sha256(changed_report))
        changed_action = request.model_copy(
            update={"max_used_space_bytes": request.max_used_space_bytes * 2}
        )
        self.assertNotEqual(
            _executor_intent_sha256(request), _executor_intent_sha256(changed_action)
        )
        with tempfile.TemporaryDirectory() as root:
            spool = RunnerHostHygieneAuditSpool(root=Path(root))
            with self.assertRaises(RuntimeError):
                execute_runner_host_hygiene_executor(
                    request=request,
                    remote_runner=_CrashingCommandRunner(),
                    audit_poster=_AuditPoster(),
                    audit_spool=spool,
                    github_idle_evidence_reader=_github_idle_evidence_reader,
                    audit_delivery_sleeper=lambda _: None,
                )
            runner = ReportRunner()
            resolved = execute_runner_host_hygiene_executor(
                request=updated.model_copy(update={"resolve_action_started": True}),
                remote_runner=runner,
                audit_poster=_AuditPoster(),
                audit_spool=spool,
                github_idle_evidence_reader=_github_idle_evidence_reader,
                audit_delivery_sleeper=lambda _: None,
            )
            self.assertEqual(resolved.status, "failed")
            self.assertTrue(resolved.reconciled)
            self.assertFalse(any(command[0] == "flock" for command in runner.commands))

    def test_stopped_missing_and_unavailable_builder_evidence_stays_honest(self) -> None:
        stopped = {"Names": "buildx_buildkit_other0", "State": "exited", "Image": "buildkit"}
        runner = ReportRunner(
            container_rows=[
                {"Names": "buildx_buildkit_builder-a0", "State": "running", "Image": "buildkit"},
                stopped,
            ]
        )
        report = report_for(runner)
        extra = next(item for item in report.builder_cache_usage if item.target_bytes is None)
        self.assertEqual(extra.qualification, "stopped_unmeasured_report_only")
        self.assertIsNone(extra.logical_bytes)
        self.assertNotIn("builder_cache_unqualified", {item.code for item in report.findings})
        self.assertFalse(
            any(command[:3] == ("docker", "exec", stopped["Names"]) for command in runner.commands)
        )
        for containers in ([], [{**stopped, "Names": "buildx_buildkit_builder-a0"}]):
            with self.subTest(containers=containers):
                report = report_for(ReportRunner(container_rows=containers))
                self.assertIn("builder_cache_unqualified", {item.code for item in report.findings})
        original = ReportRunner()

        def unavailable(command: Sequence[str], timeout: int) -> RemoteCommandResult:
            return (
                RemoteCommandResult(1)
                if tuple(command)[:2] == ("docker", "ps")
                else original(command, timeout)
            )

        report = collect_runner_host_hygiene_report(
            request=request_with_budgets(),
            remote_runner=unavailable,
            github_idle_evidence_reader=_github_idle_evidence_reader,
            idle_observation_sleeper=lambda _: None,
        )
        self.assertIn("builder_cache_unqualified", {item.code for item in report.findings})

    def test_long_builder_names_are_bounded_in_both_success_and_failure_rows(self) -> None:
        name = "long-builder-" + "x" * 70
        request = RunnerHostHygieneExecutorRequest.model_validate(
            {**request_with_budgets().model_dump(), "buildkit_cache_budgets": {name: 100}}
        )
        for containers in (
            [],
            [{"Names": f"buildx_buildkit_{name}0", "State": "running", "Image": "buildkit"}],
        ):
            with self.subTest(containers=containers):
                report = collect_runner_host_hygiene_report(
                    request=request,
                    remote_runner=ReportRunner(container_rows=containers),
                    github_idle_evidence_reader=_github_idle_evidence_reader,
                    idle_observation_sleeper=lambda _: None,
                )
                self.assertTrue(report.builder_cache_usage)
                self.assertNotIn(name, report.model_dump_json())

    def test_daemon_and_named_caches_use_the_same_logical_and_protection_basis(self) -> None:
        now = datetime.now(timezone.utc)
        named = [
            {
                "id": "shared",
                "size": 200,
                "inUse": False,
                "shared": True,
                "createdAt": (now - timedelta(days=10)).isoformat(),
            },
            {
                "id": "active",
                "size": 40,
                "inUse": True,
                "shared": True,
                "createdAt": now.isoformat(),
            },
        ]
        daemon = [
            {
                "ID": item["id"],
                "Size": item["size"],
                "InUse": item["inUse"],
                "Shared": item["shared"],
            }
            for item in named
        ]
        report = report_for(ReportRunner(usage=json.dumps(named), daemon_records=daemon))
        default = next(item for item in report.builder_cache_usage if item.cache_key == "default")
        builder = next(item for item in report.builder_cache_usage if item.cache_key == "builder-a")
        self.assertEqual(default.logical_bytes, builder.logical_bytes)
        self.assertEqual(default.protected_bytes, builder.protected_bytes)
        self.assertEqual(builder.protected_bytes, named[-1]["size"])
        self.assertEqual(builder.shared_bytes, builder.logical_bytes)
        self.assertEqual(builder.qualification, "maintenance_required")

    def test_summary_handles_missing_invalid_and_multiple_private_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            summary, bad = Path(root) / "summary.md", Path(root) / "bad.json"
            bad.write_text('{"private-host": "private-secret",')
            args = ["work-graph", "runner-host-hygiene-qualify", "--summary-file", str(summary)]
            for extra in ([], ["--audit-file", str(bad)]):
                response = CliRunner().invoke(main, [*args, *extra])
                self.assertEqual(response.exit_code, 0, response.output)
                self.assertNotEqual(
                    CliRunner().invoke(main, [*args, *extra, "--require-qualified"]).exit_code, 0
                )
            self.assertNotIn("private-host", summary.read_text())
            self.assertNotIn("private-secret", summary.read_text())
            self.assertTrue(summary.read_text().strip())
