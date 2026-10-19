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
)
from control_plane.workflows.runner_host_hygiene_qualification import qualify_runner_host_hygiene
from tests.test_runner_host_hygiene_executor import (
    _AuditPoster,
    _CommandRunner,
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
    ) -> None:
        containers = [
            {"Names": "buildx_buildkit_builder-a0", "State": "running", "Image": "buildkit"}
        ]
        if extra_builder:
            containers.append(
                {"Names": "buildx_buildkit_other-product0", "State": "running", "Image": "buildkit"}
            )
        super().__init__(
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
            self.assertFalse(result.maintenance_completed)
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
        # A renamed tag is accepted through the same exact image-inspect path,
        # including Docker's normal implicit latest/digest resolution semantics.
        changed = request_with_budgets().model_copy(
            update={"retained_warm_builders": ("example:verify-123-1-runtime",)}
        )
        report = collect_runner_host_hygiene_report(
            request=changed,
            remote_runner=ReportRunner(),
            github_idle_evidence_reader=_github_idle_evidence_reader,
            idle_observation_sleeper=lambda _: None,
        )
        self.assertNotIn("required_warm_builder_missing", {item.code for item in report.findings})

    def test_unavailable_and_truncated_builder_measurements_are_not_zero(self) -> None:
        for usage in ("", '[{"id":"partial"}', '[{"id":"partial"}]'):
            with self.subTest(usage=usage):
                report = report_for(ReportRunner(usage=usage))
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
                    "maintenance_required" if cause == "none" else "protected_cache",
                )
                self.assertEqual(bool(report.findings), cause == "none")
                self.assertTrue(measured.recovery_owner)

    def test_unbudgeted_builder_is_report_only_without_adding_cleanup_authority(self) -> None:
        report = report_for(ReportRunner(extra_builder=True))
        extra = next(
            item
            for item in report.builder_cache_usage
            if item.cache_key == "buildx_buildkit_other-product0"
        )
        self.assertIsNone(extra.target_bytes)
        self.assertEqual(extra.qualification, "unbudgeted_report_only")
        self.assertTrue(extra.recovery_owner)
        self.assertFalse(request_with_budgets().allowed_buildkit_builders)

    def test_report_budget_must_agree_with_the_target_and_not_change_legacy_recovery_intent(
        self,
    ) -> None:
        request = request_with_budgets()
        with self.assertRaisesRegex(ValueError, "agree"):
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
            self.assertFalse(result.maintenance_completed)
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
            self.assertIn("maintenance_completed", summary.read_text())
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
                self.assertTrue(result.maintenance_completed)
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
            pending = envelope.model_copy(update={"terminal_delivery_state": "pending"})
            self.assertFalse(
                qualify_runner_host_hygiene(
                    pending, as_of=datetime.now(timezone.utc), maximum_age_seconds=3600
                )["maintenance_completed"]
            )
