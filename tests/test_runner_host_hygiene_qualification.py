from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from typing import Literal

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.workflows.runner_host_hygiene_qualification import (
    BuilderMeasurement,
    HygieneQualificationEvidence,
    HygieneQualification,
    qualify_runner_host_hygiene,
)
from control_plane.workflows.runner_host_hygiene_executor import RunnerHostGitHubBinding
from tests.test_runner_host_hygiene_executor import (
    _AuditPoster,
    _CommandRunner,
    _request,
    execute_runner_host_hygiene_executor,
)
from control_plane.workflows.runner_host_hygiene_audit_spool import RunnerHostHygieneAuditSpool


class HygieneQualificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc)
        self.request = _request(
            mutate=False,
            target_buildkit_builder="builder-a",
            allowed_buildkit_builders=("builder-a",),
            max_used_space_bytes=8 * 1024**3,
        )
        self.evidence = HygieneQualificationEvidence(
            host_name=self.request.host_name,
            observed_at=self.now,
            services_complete=True,
            registrations_complete=True,
            images_complete=True,
            builders_complete=True,
            service_units=tuple(item.service_unit for item in self.request.github_idle_bindings),
            registrations=self.request.github_idle_bindings,
            image_references=self.request.retained_warm_builders,
            builders=(
                BuilderMeasurement(builder="builder-a", availability="complete", logical_bytes=0),
            ),
        )

    def qualify(self, **updates: object) -> HygieneQualification:
        evidence = HygieneQualificationEvidence.model_validate(
            {
                **self.evidence.model_dump(),
                **updates,
            }
        )
        return qualify_runner_host_hygiene(
            requests=(self.request,), evidence=evidence, now=self.now
        )

    def test_complete_fleet_and_measured_zero_qualify(self) -> None:
        result = self.qualify()
        self.assertTrue(result.configuration_qualified)
        self.assertTrue(result.retention_accounted_for)
        self.assertFalse(result.builders[0].accepted_terminal_completion)

    def test_added_service_cannot_be_omitted_even_when_every_configured_service_exists(
        self,
    ) -> None:
        added = RunnerHostGitHubBinding(
            "example/project", "general", "runner-b", "launchplane-runner@b.service"
        )
        evidence = self.evidence.model_copy(
            update={
                "service_units": (*self.evidence.service_units, added.service_unit),
                "registrations": (*self.evidence.registrations, added),
            }
        )
        stale = qualify_runner_host_hygiene(
            requests=(self.request,), evidence=evidence, now=self.now
        )
        self.assertIn("fleet_binding_mismatch", stale.blockers)
        corrected = self.request.model_copy(update={"github_idle_bindings": evidence.registrations})
        result = qualify_runner_host_hygiene(requests=(corrected,), evidence=evidence, now=self.now)
        self.assertTrue(result.configuration_qualified)

    def test_run_specific_image_requires_exact_reviewed_replacement(self) -> None:
        produced = "example-image:verify-123-1-runtime"
        self.assertIn("retained_image_missing", self.qualify(image_references=(produced,)).blockers)
        corrected = self.request.model_copy(update={"retained_warm_builders": (produced,)})
        result = qualify_runner_host_hygiene(
            requests=(corrected,),
            evidence=self.evidence.model_copy(update={"image_references": (produced,)}),
            now=self.now,
        )
        self.assertTrue(result.configuration_qualified)
        self.assertFalse(self.qualify(images_complete=False).configuration_qualified)

    def test_unavailable_partial_and_missing_builder_are_never_zero(self) -> None:
        samples: list[tuple[Literal["complete", "partial", "unavailable"], int | None]] = [
            ("unavailable", None),
            ("partial", 0),
            ("complete", None),
        ]
        for availability, logical in samples:
            with self.subTest(availability=availability, logical=logical):
                result = self.qualify(
                    builders=(
                        BuilderMeasurement(
                            builder="builder-a", availability=availability, logical_bytes=logical
                        ),
                    )
                )
                self.assertFalse(result.retention_accounted_for)
                self.assertEqual(result.builders[0].status, "unknown")
        self.assertFalse(self.qualify(builders=()).retention_accounted_for)
        self.assertFalse(self.qualify(builders_complete=False).retention_accounted_for)

    def test_excess_requires_explicit_constraints_and_recovery_owner(self) -> None:
        measurement = BuilderMeasurement(
            builder="builder-a",
            availability="complete",
            logical_bytes=self.request.max_used_space_bytes + 1,
        )
        result = self.qualify(builders=(measurement,))
        self.assertFalse(result.retention_accounted_for)
        for constraint in ("protected", "recent_cache", "in_use", "maintenance_blocked"):
            with self.subTest(constraint=constraint):
                owned = measurement.model_copy(
                    update={
                        "constraint": constraint,
                        "evidence": "fresh named-builder evidence",
                        "recovery_owner": "infra issue",
                    }
                )
                result = self.qualify(builders=(owned,))
                self.assertEqual(
                    result.retention_accounted_for, constraint != "maintenance_blocked"
                )
                self.assertFalse(
                    self.qualify(
                        builders=(owned.model_copy(update={"recovery_owner": ""}),)
                    ).retention_accounted_for
                )

    def test_unbudgeted_builder_stays_unqualified_without_borrowing_budget(self) -> None:
        extra = BuilderMeasurement(
            builder="other-product", availability="complete", logical_bytes=0
        )
        result = self.qualify(builders=(*self.evidence.builders, extra))
        self.assertIn("builder_ownership_unqualified", result.blockers)
        self.assertFalse(result.retention_accounted_for)

    def test_stale_unknown_duplicate_and_wrong_host_sources_fail_closed(self) -> None:
        updates: list[dict[str, object]] = [
            {"observed_at": self.now - timedelta(seconds=self.request.idle_max_age_seconds + 1)},
            {"observed_at": self.now + timedelta(seconds=1)},
            {"services_complete": False},
            {"registrations_complete": False},
            {"host_name": "another-host"},
            {"registrations": (*self.evidence.registrations, *self.evidence.registrations)},
            {"builders": (*self.evidence.builders, *self.evidence.builders)},
        ]
        for update in updates:
            with self.subTest(update=update):
                self.assertFalse(self.qualify(**update).configuration_qualified)

    def test_delivered_blocked_dry_run_does_not_prove_terminal_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            spool = RunnerHostHygieneAuditSpool(root=Path(root))
            result = execute_runner_host_hygiene_executor(
                request=self.request,
                remote_runner=_CommandRunner(),
                audit_poster=_AuditPoster(),
                audit_spool=spool,
            )
            self.assertEqual(result.status, "blocked")
            self.assertFalse(result.model_dump()["maintenance_completed"])
            self.assertFalse(result.audit_delivery_pending)
            envelope = spool.read(
                host_name=self.request.host_name,
                action=self.request.action,
                audit_record_key=self.request.audit_record_key,
            )
            self.assertIsNotNone(envelope)
            qualified = self.qualify(audits=(envelope,))
            self.assertFalse(qualified.builders[0].accepted_terminal_completion)

    def test_terminal_completion_requires_matching_target_and_accepted_delivery(self) -> None:
        request = _request(
            mutate=True,
            target_buildkit_builder="odoo-docker-chris-testing",
            allowed_buildkit_builders=("odoo-docker-chris-testing",),
            max_used_space_bytes=8 * 1024**3,
        )
        with tempfile.TemporaryDirectory() as root:
            spool = RunnerHostHygieneAuditSpool(root=Path(root))
            result = execute_runner_host_hygiene_executor(
                request=request,
                remote_runner=_CommandRunner(),
                audit_poster=_AuditPoster(),
                audit_spool=spool,
            )
            self.assertEqual(result.status, "completed")
            self.assertTrue(result.model_dump()["maintenance_completed"])
            replay_runner = _CommandRunner()
            replay = execute_runner_host_hygiene_executor(
                request=request,
                remote_runner=replay_runner,
                audit_poster=_AuditPoster(),
                audit_spool=spool,
            )
            self.assertTrue(replay.maintenance_completed)
            self.assertFalse(replay_runner.commands)
            envelope = spool.read(
                host_name=request.host_name,
                action=request.action,
                audit_record_key=request.audit_record_key,
            )
            assert envelope is not None
            self.request = request
            self.now = datetime.now(timezone.utc)
            self.evidence = self.evidence.model_copy(
                update={
                    "builders": (
                        BuilderMeasurement(
                            builder=request.target_buildkit_builder,
                            availability="complete",
                            logical_bytes=0,
                        ),
                    )
                }
            )
            self.assertTrue(
                self.qualify(audits=(envelope,)).builders[0].accepted_terminal_completion
            )
            for update in (
                {"terminal_delivery_state": "pending"},
                {"planned_delivery_state": "pending"},
            ):
                self.assertFalse(
                    self.qualify(audits=(envelope.model_copy(update=update),))
                    .builders[0]
                    .accepted_terminal_completion
                )
            self.request = request.model_copy(
                update={"max_used_space_bytes": request.max_used_space_bytes + 1}
            )
            self.assertFalse(
                self.qualify(audits=(envelope,)).builders[0].accepted_terminal_completion
            )
            self.request = request
            self.now += timedelta(seconds=request.idle_max_age_seconds + 1)
            self.assertFalse(
                self.qualify(audits=(envelope,)).builders[0].accepted_terminal_completion
            )

    def test_cli_rejects_drift_and_accepts_qualified_review_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            request_file, evidence_file = Path(root) / "requests.json", Path(root) / "evidence.json"
            request_file.write_text(json.dumps([self.request.model_dump(mode="json")]))
            evidence_file.write_text(self.evidence.model_dump_json())
            args = [
                "work-graph",
                "runner-host-hygiene-qualify",
                "--requests-file",
                str(request_file),
                "--evidence-file",
                str(evidence_file),
            ]
            result = CliRunner().invoke(main, args)
            self.assertEqual(result.exit_code, 0, result.output)
            evidence_file.write_text(
                self.evidence.model_copy(update={"service_units": ()}).model_dump_json()
            )
            result = CliRunner().invoke(main, args)
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("fleet_binding_mismatch", result.output)
