import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest

from click import Command
from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneObservation
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyPolicy
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyRequest
from control_plane.contracts.runner_host_hygiene import RunnerHostHygienePolicy
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneReport
from control_plane.contracts.runner_host_hygiene import evaluate_runner_host_hygiene
from control_plane.contracts.runner_host_hygiene import plan_runner_host_hygiene_apply


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

    def test_apply_audit_record_requires_terminal_post_apply_report(self) -> None:
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


def _healthy_report() -> RunnerHostHygieneReport:
    return evaluate_runner_host_hygiene(
        policy=RunnerHostHygienePolicy(required_warm_builders=("odoo-docker-chris-testing",)),
        observation=RunnerHostHygieneObservation(
            host_name="chris-testing",
            observed_at="2026-05-23T13:00:00Z",
            free_disk_bytes=500,
            warm_builders=("odoo-docker-chris-testing",),
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


if __name__ == "__main__":
    unittest.main()
