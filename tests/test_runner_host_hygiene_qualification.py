from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

import click
from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneReport
from control_plane.workflows.runner_host_hygiene_audit_spool import RunnerHostHygieneAuditSpool
from control_plane.workflows.runner_host_hygiene_executor import (
    RemoteCommandResult,
    RunnerHostHygieneExecutorRequest,
    GeneratedRunCacheRoot,
    collect_runner_host_hygiene_report,
    execute_runner_host_hygiene_executor,
    _parse_builder_cache_usage,
    _executor_intent_sha256,
    _BUILDCTL_STORAGE_MB_BYTES,
)
from control_plane.workflows.runner_host_hygiene_qualification import qualify_runner_host_hygiene
from tests.test_runner_host_hygiene_executor import (
    _AuditPoster,
    _CommandRunner,
    _CrashingCommandRunner,
    _docker_disk_usage_json,
    _generated_cache_evidence,
    _github_idle_evidence_reader,
    _request,
)


class ReportRunner(_CommandRunner):
    def __init__(
        self,
        *,
        usage: str = "[]",
        generated_cache_before: str = "",
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
            generated_cache_before=generated_cache_before,
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
            "buildkit_cache_budgets": {
                "default": 8 * 1024**3,
                "builder-a": 100 * _BUILDCTL_STORAGE_MB_BYTES,
            },
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

        report = report_for(
            ReportRunner(daemon_records=[{"ID": "partial", "Size": 1, "Shared": False}])
        )
        default = next(item for item in report.builder_cache_usage if item.cache_key == "default")
        self.assertIsNone(default.logical_bytes)
        self.assertIsNone(default.budgeted_bytes)
        self.assertEqual(default.qualification, "unknown")

    def test_protected_cache_explains_excess_without_estimating_reclaim(self) -> None:
        now = datetime.now(timezone.utc)
        record = {
            "id": "cache",
            "size": 200 * _BUILDCTL_STORAGE_MB_BYTES,
            "inUse": False,
            "mutable": False,
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
                    (
                        "maintenance_required"
                        if cause == "none"
                        else "within_target"
                        if cause == "shared"
                        else "protected_cache"
                    ),
                )
                self.assertEqual(bool(report.findings), cause == "none")
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
            "mutable": False,
            "shared": False,
            "createdAt": now.isoformat(),
            "lastUsedAt": now.isoformat(),
        }
        for records in (
            [record, record],
            [{**record, "size": -1}],
            [{key: value for key, value in record.items() if key != "lastUsedAt"}],
            [{**record, "lastUsedAt": now.replace(tzinfo=None).isoformat()}],
            [{**record, "lastUsedAt": (now + timedelta(seconds=1)).isoformat()}],
            [{key: value for key, value in record.items() if key != "mutable"}],
            [{**record, "mutable": "false"}],
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
            missing_label = "builder-missing"
            missing = Path(root) / f"runner-host-hygiene-audit-evidence-{missing_label}.json"
            response = CliRunner().invoke(main, [*args, "--audit-file", str(missing)])
            self.assertNotEqual(response.exit_code, 0)
            self.assertIn(missing_label, summary.read_text())
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
                "max_used_space_bytes": request_with_budgets().buildkit_cache_budgets["builder-a"],
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
                "size": 200 * _BUILDCTL_STORAGE_MB_BYTES,
                "inUse": False,
                "mutable": False,
                "shared": True,
                "createdAt": (now - timedelta(days=10)).isoformat(),
                "lastUsedAt": (now - timedelta(days=10)).isoformat(),
            },
            {
                "id": "active",
                "size": 40,
                "inUse": True,
                "mutable": False,
                "shared": True,
                "createdAt": now.isoformat(),
                "lastUsedAt": now.isoformat(),
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
        self.assertEqual(builder.protected_bytes, 0)
        self.assertEqual(default.budgeted_bytes, builder.budgeted_bytes)
        self.assertEqual(builder.shared_bytes, builder.logical_bytes)
        self.assertEqual(builder.qualification, "within_target")

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

    def test_unresolved_and_pending_audits_cannot_qualify_from_healthy_pre_report(self) -> None:
        request = request_with_budgets(mutate=True)
        runner = ReportRunner()

        def crash(command: Sequence[str], timeout: int) -> RemoteCommandResult:
            if command[0] == "flock":
                raise RuntimeError("interrupted action")
            return runner(command, timeout)

        with tempfile.TemporaryDirectory() as root:
            spool = RunnerHostHygieneAuditSpool(root=Path(root))
            with self.assertRaises(RuntimeError):
                execute_runner_host_hygiene_executor(
                    request=request,
                    remote_runner=crash,
                    audit_poster=_AuditPoster(),
                    audit_spool=spool,
                    github_idle_evidence_reader=_github_idle_evidence_reader,
                    audit_delivery_sleeper=lambda _: None,
                )
            envelope = spool.read(
                host_name=request.host_name,
                action=request.action,
                audit_record_key=request.audit_record_key,
            )
            assert envelope is not None
            for state in (envelope.execution_state, "planned"):
                result = qualify_runner_host_hygiene(
                    envelope.model_copy(update={"execution_state": state}),
                    as_of=datetime.now(timezone.utc),
                    maximum_age_seconds=3600,
                )
                self.assertTrue(result["retention_accounted_for"])
                self.assertTrue(result["recovery_required"])
                self.assertFalse(result["qualified"])

            complete_spool = RunnerHostHygieneAuditSpool(root=Path(root) / "complete")
            execute_runner_host_hygiene_executor(
                request=request,
                remote_runner=ReportRunner(),
                audit_poster=_AuditPoster(),
                audit_spool=complete_spool,
                github_idle_evidence_reader=_github_idle_evidence_reader,
                audit_delivery_sleeper=lambda _: None,
            )
            complete = complete_spool.read(
                host_name=request.host_name,
                action=request.action,
                audit_record_key=request.audit_record_key,
            )
            assert complete is not None and complete.terminal_audit is not None
            for update in (
                {"terminal_delivery_state": "pending"},
                {"terminal_audit": complete.terminal_audit.model_copy(update={"status": "failed"})},
            ):
                result = qualify_runner_host_hygiene(
                    complete.model_copy(update=update),
                    as_of=datetime.now(timezone.utc),
                    maximum_age_seconds=3600,
                )
                self.assertTrue(result["retention_accounted_for"])
                self.assertFalse(result["qualified"])

    def test_unbudgeted_probe_failure_is_report_only_unknown_without_a_new_grant(self) -> None:
        runner = ReportRunner(extra_builder=True)

        def unavailable(command: Sequence[str], timeout: int) -> RemoteCommandResult:
            if tuple(command)[:3] == ("docker", "exec", "buildx_buildkit_other-product0"):
                return RemoteCommandResult(1)
            return runner(command, timeout)

        report = collect_runner_host_hygiene_report(
            request=request_with_budgets(),
            remote_runner=unavailable,
            github_idle_evidence_reader=_github_idle_evidence_reader,
            idle_observation_sleeper=lambda _: None,
        )
        row = next(item for item in report.builder_cache_usage if item.target_bytes is None)
        self.assertEqual(row.qualification, "unbudgeted_unmeasured_report_only")
        self.assertIsNone(row.logical_bytes)
        self.assertNotIn("builder_cache_unqualified", {item.code for item in report.findings})

    def test_target_comparison_uses_adapter_granularity_and_actual_lru_age(self) -> None:
        now = datetime.now(timezone.utc)
        target = 100 * _BUILDCTL_STORAGE_MB_BYTES + 1
        request = RunnerHostHygieneExecutorRequest.model_validate(
            {**request_with_budgets().model_dump(), "buildkit_cache_budgets": {"builder-a": target}}
        )
        record = {
            "id": "old",
            "size": target + 1,
            "inUse": False,
            "mutable": False,
            "shared": False,
            "lastUsedAt": None,
            "createdAt": now.isoformat(),
        }
        report = collect_runner_host_hygiene_report(
            request=request,
            remote_runner=ReportRunner(usage=json.dumps([record])),
            github_idle_evidence_reader=_github_idle_evidence_reader,
            idle_observation_sleeper=lambda _: None,
        )
        row = report.builder_cache_usage[0]
        assert row.budgeted_bytes is not None and row.target_bytes is not None
        self.assertGreater(row.budgeted_bytes, row.target_bytes)
        self.assertEqual(row.qualification, "within_target")
        self.assertEqual(row.protected_bytes, 0)
        # A mutable active record reports a synthetic size of zero in BuildKit;
        # it is unavailable measurement, not proof that cache is empty.
        with self.assertRaises(ValueError):
            _parse_builder_cache_usage(
                json.dumps([{**record, "mutable": True, "inUse": True, "size": 0}]),
                observed_at=now.isoformat(),
                minimum_age_hours=168,
            )

    def test_engine_active_zero_is_unknown_but_empty_and_inactive_zero_are_measured(self) -> None:
        for records, known in (
            ([{"ID": "active", "Size": 0, "InUse": True, "Shared": False}], False),
            ([{"ID": "inactive", "Size": 0, "InUse": False, "Shared": False}], True),
            ([], True),
        ):
            with self.subTest(records=records):
                report = report_for(ReportRunner(daemon_records=records))
                row = next(
                    item for item in report.builder_cache_usage if item.cache_key == "default"
                )
                aggregate = next(
                    item
                    for item in report.cache_class_telemetry
                    if item.cache_class == "docker_build_cache"
                )
                self.assertEqual(row.accounted_for, known)
                if known:
                    self.assertEqual(row.logical_bytes, 0)
                    self.assertEqual(aggregate.logical_bytes, 0)
                else:
                    self.assertIsNone(row.logical_bytes)
                    # Legacy Engine telemetry is a reported-size lower bound.
                    # Qualification must not add a new maintenance admission gate.
                    self.assertEqual(aggregate.logical_bytes, 0)
                    self.assertEqual(aggregate.availability, "available")
                    self.assertIn(
                        "builder_cache_unqualified", {item.code for item in report.findings}
                    )
        now = datetime.now(timezone.utc).isoformat()
        for mutable, active in ((True, False), (False, True)):
            measured = _parse_builder_cache_usage(
                json.dumps(
                    [
                        {
                            "id": "zero",
                            "size": 0,
                            "inUse": active,
                            "shared": False,
                            "mutable": mutable,
                            "lastUsedAt": None,
                        }
                    ]
                ),
                observed_at=now,
                minimum_age_hours=168,
            )
            self.assertEqual(measured["logical_bytes"], 0)

    def test_active_zero_daemon_retains_reclaimable_bytes_and_audited_maintenance_admission(
        self,
    ) -> None:
        for target in ("", "builder-a"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as root:
                original = request_with_budgets(mutate=True)
                request = RunnerHostHygieneExecutorRequest.model_validate(
                    {
                        **original.model_dump(),
                        "target_buildkit_builder": target,
                        "max_used_space_bytes": original.buildkit_cache_budgets[
                            target or "default"
                        ],
                        "allowed_buildkit_builders": (target,) if target else (),
                    }
                )
                runner = ReportRunner(
                    daemon_records=[
                        {"ID": "active", "Size": 0, "InUse": True, "Shared": False},
                        {"ID": "reclaimable", "Size": 100, "InUse": False, "Shared": False},
                    ]
                )

                def busy(command: Sequence[str], timeout: int) -> RemoteCommandResult:
                    if tuple(command)[:2] == ("bash", "-lc") and "pgrep -af" in command[2]:
                        return RemoteCommandResult(
                            0, "789 /opt/actions-runner/bin/Runner.Worker spawnclient 456\n"
                        )
                    return runner(command, timeout)

                spool = RunnerHostHygieneAuditSpool(root=Path(root))
                result = execute_runner_host_hygiene_executor(
                    request=request,
                    remote_runner=busy,
                    audit_poster=_AuditPoster(),
                    audit_spool=spool,
                    github_idle_evidence_reader=_github_idle_evidence_reader,
                    audit_delivery_sleeper=lambda _: None,
                )
                self.assertEqual(result.status, "blocked")
                self.assertFalse(any(command[0] == "flock" for command in runner.commands))
                envelope = spool.read(
                    host_name=request.host_name,
                    action=request.action,
                    audit_record_key=request.audit_record_key,
                )
                assert envelope is not None
                self.assertEqual(envelope.planned_delivery_state, "delivered")
                self.assertIsNone(envelope.terminal_audit)
                report = envelope.planned_audit.pre_apply_report
                self.assertEqual(report.docker_reclaimable_breakdown.build_cache_bytes, 100)
                default = next(
                    item for item in report.builder_cache_usage if item.cache_key == "default"
                )
                self.assertFalse(default.accounted_for)
                self.assertIsNone(default.logical_bytes)

    def test_failed_terminal_uses_latest_evidence_and_reports_action_failure(self) -> None:
        for cause in ("idle", "collection", "command"):
            with self.subTest(cause=cause), tempfile.TemporaryDirectory() as root:
                request = request_with_budgets(mutate=True)
                runner = ReportRunner()
                probes = 0

                def fail(command: Sequence[str], timeout: int) -> RemoteCommandResult:
                    nonlocal probes
                    if tuple(command)[:2] == ("bash", "-lc") and "pgrep -af" in command[2]:
                        probes += 1
                        if probes > 2 and cause == "collection":
                            raise click.ClickException("measurement unavailable")
                        if probes > 2 and cause == "idle":
                            return RemoteCommandResult(
                                0, "789 /opt/actions-runner/bin/Runner.Worker spawnclient 456\n"
                            )
                    if command[0] == "flock" and cause == "command":
                        return RemoteCommandResult(1)
                    return runner(command, timeout)

                artifact, summary = Path(root) / "audit.json", Path(root) / "summary.md"
                spool = RunnerHostHygieneAuditSpool(
                    root=Path(root) / "spool", artifact_file=artifact
                )
                execute_runner_host_hygiene_executor(
                    request=request,
                    remote_runner=fail,
                    audit_poster=_AuditPoster(),
                    audit_spool=spool,
                    github_idle_evidence_reader=_github_idle_evidence_reader,
                    audit_delivery_sleeper=lambda _: None,
                )
                envelope = spool.read(
                    host_name=request.host_name,
                    action=request.action,
                    audit_record_key=request.audit_record_key,
                )
                assert envelope is not None and envelope.terminal_audit is not None
                terminal = envelope.terminal_audit
                self.assertEqual(terminal.status, "failed")
                result = qualify_runner_host_hygiene(
                    envelope, as_of=datetime.now(timezone.utc), maximum_age_seconds=3600
                )
                self.assertEqual(
                    result["blockers"],
                    tuple(
                        item.code
                        for item in terminal.plan.blockers
                        if item.code != "mutate_not_requested"
                    ),
                )
                current_report = terminal.post_apply_report or terminal.pre_apply_report
                self.assertEqual(
                    result["findings"], tuple(item.code for item in current_report.findings)
                )
                self.assertEqual(result["terminal_status"], terminal.status)
                self.assertFalse(result["maintenance_completed"])
                self.assertFalse(result["qualified"])
                if cause == "idle":
                    self.assertTrue(result["blockers"])
                    self.assertTrue(result["findings"])
                if cause == "command":
                    self.assertEqual(result["report_status"], "healthy")
                response = CliRunner().invoke(
                    main,
                    [
                        "work-graph",
                        "runner-host-hygiene-qualify",
                        "--audit-file",
                        str(artifact),
                        "--summary-file",
                        str(summary),
                    ],
                )
                self.assertEqual(response.exit_code, 0, response.output)
                self.assertIn(f"terminal_status: `{terminal.status}`", summary.read_text())

    def test_idle_blocks_action_qualification_without_erasing_retention_accounting(self) -> None:
        from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyBlocker

        request = request_with_budgets()
        with tempfile.TemporaryDirectory() as root:
            spool = RunnerHostHygieneAuditSpool(root=Path(root))
            execute_runner_host_hygiene_executor(
                request=request,
                remote_runner=ReportRunner(),
                audit_poster=_AuditPoster(),
                audit_spool=spool,
                github_idle_evidence_reader=_github_idle_evidence_reader,
                audit_delivery_sleeper=lambda _: None,
            )
            envelope = spool.read(
                host_name=request.host_name,
                action=request.action,
                audit_record_key=request.audit_record_key,
            )
            assert envelope is not None
            plan = envelope.planned_audit.plan.model_copy(
                update={
                    "blockers": (
                        RunnerHostHygieneApplyBlocker(
                            code="idle_evidence_stale", message="stale idle evidence"
                        ),
                    )
                }
            )
            updated = envelope.model_copy(
                update={"planned_audit": envelope.planned_audit.model_copy(update={"plan": plan})}
            )
            result = qualify_runner_host_hygiene(
                updated, as_of=datetime.now(timezone.utc), maximum_age_seconds=3600
            )
            self.assertTrue(result["retention_accounted_for"])
            self.assertFalse(result["qualified"])

    def test_existing_below_high_water_outcome_is_not_a_retention_failure(self) -> None:
        root = GeneratedRunCacheRoot(
            key="codex-lab-runs",
            path="/home/gha/.cache/codex-ci",
            source_repository="cbusillo/codex-lab",
            high_water_bytes=100,
            low_water_bytes=10,
            minimum_age_hours=1,
            cooldown_hours=1,
        )
        request = _request(
            mutate=True,
            action="prune_generated_run_cache",
            generated_run_cache_roots=(root,),
            target_generated_cache_key=root.key,
        )
        request = RunnerHostHygieneExecutorRequest.model_validate(
            {
                **request.model_dump(),
                "buildkit_cache_budgets": request_with_budgets().buildkit_cache_budgets,
            }
        )
        runner = ReportRunner(
            generated_cache_before=_generated_cache_evidence(
                entries=((101, 80, 80, 7_200, (0, 0)),), worker_observations=(0, 0)
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            spool = RunnerHostHygieneAuditSpool(root=Path(directory))
            result = execute_runner_host_hygiene_executor(
                request=request,
                remote_runner=runner,
                audit_poster=_AuditPoster(),
                audit_spool=spool,
                github_run_state_reader=lambda _repo, run_ids: {
                    run_id: "completed" for run_id in run_ids
                },
                audit_delivery_sleeper=lambda _: None,
            )
            self.assertEqual(result.status, "not_needed")
            envelope = spool.read(
                host_name=request.host_name,
                action=request.action,
                audit_record_key=request.audit_record_key,
            )
            assert envelope is not None
            qualified = qualify_runner_host_hygiene(
                envelope, as_of=datetime.now(timezone.utc), maximum_age_seconds=3600
            )
            self.assertTrue(qualified["action_not_needed"])
            self.assertTrue(qualified["qualified"])
            self.assertFalse(qualified["maintenance_completed"])
