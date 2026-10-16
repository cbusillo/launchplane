from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest

from pydantic import ValidationError

from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_environment_read_model import _health_monitoring_summary
from control_plane.contracts.runtime_dependency_health import (
    RuntimeDependencyExpectations,
    RuntimeDependencyHealthPolicy,
    RuntimeDependencyOverride,
    runtime_dependency_health_evidence,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord
from control_plane.product_health_monitoring import (
    ProductHealthMonitoringApplyRequest,
    build_product_health_monitoring_plan,
    updated_product_health_monitoring_profile,
)
from control_plane.runtime_dependency_health import derive_runtime_dependencies
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.public_ingress_monitor import (
    HttpObservation,
    check_public_ingress_target,
    discover_public_ingress_monitor_targets,
)
from tests.support.profiles import product_profile_payload


STAMP = "2026-10-16T15:00:00Z"


def profile() -> LaunchplaneProductProfileRecord:
    payload = product_profile_payload("example-web")
    lanes = list(cast(tuple[dict[str, object], ...], payload["lanes"]))
    lanes[0]["health_monitoring"] = {
        "monitoring_intent": "public",
        "checks": [{"name": "public-ingress"}],
    }
    payload["lanes"] = lanes
    return LaunchplaneProductProfileRecord.model_validate(payload)


class RuntimeDependencyHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = FilesystemRecordStore(Path(temporary.name))
        self.profile = profile()
        self.lane = self.profile.lanes[0]

    def derive(self) -> RuntimeDependencyExpectations:
        return derive_runtime_dependencies(
            profile=self.profile, lane=self.lane, record_store=self.store
        )

    def runtime(self, env: dict[str, str], **kwargs: object) -> None:
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord.model_validate(
                {
                    "scope": "instance",
                    "context": self.lane.context,
                    "instance": self.lane.instance,
                    "env": env,
                    "updated_at": STAMP,
                    **kwargs,
                }
            )
        )

    def binding(
        self,
        key: str,
        *,
        instance: str = "",
        status: str = "configured",
        integration: str = "runtime_environment",
    ) -> None:
        secret_id = "test-" + key + instance
        self.store.write_secret_record(
            SecretRecord.model_validate(
                {
                    "secret_id": secret_id,
                    "name": key,
                    "integration": integration,
                    "scope": "context_instance" if instance else "context",
                    "context": self.lane.context,
                    "instance": instance,
                    "policy": "write_only",
                    "status": status,
                    "created_at": STAMP,
                    "updated_at": STAMP,
                    "current_version_id": "opaque",
                }
            )
        )
        self.store.write_secret_binding(
            SecretBinding.model_validate(
                {
                    "binding_id": secret_id,
                    "secret_id": secret_id,
                    "binding_key": key,
                    "integration": integration,
                    "context": self.lane.context,
                    "instance": instance,
                    "status": status,
                    "created_at": STAMP,
                    "updated_at": STAMP,
                }
            )
        )

    def test_derives_services_volumes_and_effective_binding_metadata_without_secret_reads(
        self,
    ) -> None:
        self.runtime({"ODOO_DB_VOLUME": "opaque", "REDIS_HOST": "opaque"})
        self.binding("SMTP_PASSWORD")
        self.assertEqual(self.derive().dependencies, ("cache", "database", "email"))
        # No secret version exists; metadata derivation must not try to decrypt it.

    def test_scope_disabled_retirement_and_integration_exclusions(self) -> None:
        self.binding("DATABASE_URL", instance="prod")
        self.binding("SMTP_PASSWORD", status="disabled")
        self.binding("RESEND_API_KEY", integration="launchplane_service")
        self.runtime(
            {"UNRELATED": "value"}, schema_version=2, retired_provider_keys=["DATABASE_URL"]
        )
        self.binding("DATABASE_URL", instance=self.lane.instance)
        self.assertEqual(self.derive().dependencies, ())

    def test_target_metadata_and_driver_defaults(self) -> None:
        self.store.write_dokploy_target_record(
            DokployTargetRecord(
                context=self.lane.context,
                instance=self.lane.instance,
                env={"POSTGRES_DB": "opaque"},
                updated_at=STAMP,
            )
        )
        self.assertEqual(self.derive().dependencies, ("database",))
        self.profile = self.profile.model_copy(update={"driver_id": "odoo"})
        self.assertEqual(
            derive_runtime_dependencies(
                profile=self.profile, lane=self.lane, record_store=object()
            ).dependencies,
            ("database",),
        )

    def test_empty_override_requires_reason_and_overrides_derived_database(self) -> None:
        self.runtime({"DATABASE_URL": "opaque"})
        policy = self.lane.health_monitoring.model_copy(
            update={
                "dependency_health": RuntimeDependencyHealthPolicy(
                    override=RuntimeDependencyOverride(
                        dependencies=(),
                        reason="No runtime database; obsolete binding is being retired.",
                    )
                )
            }
        )
        self.lane = self.lane.model_copy(update={"health_monitoring": policy})
        self.assertEqual(self.derive().dependencies, ())
        self.assertEqual(self.derive().source, "override")
        with self.assertRaises(ValidationError):
            RuntimeDependencyOverride(dependencies=(), reason=" ")

    def test_payload_missing_invalid_failure_and_public_sanitization(self) -> None:
        expected = RuntimeDependencyExpectations(dependencies=("database", "email"))
        payloads: tuple[object, ...] = (
            None,
            [],
            {},
            {"dependencies": {"database": True}},
            {"dependencies": {"database": {"status": "pass", "password": "secret"}}},
        )
        for payload in payloads:
            evidence = runtime_dependency_health_evidence(
                payload=payload, expectations=expected, policy=RuntimeDependencyHealthPolicy()
            )
            self.assertEqual(evidence.status, "not_covered")
            self.assertNotIn("secret", evidence.model_dump_json())
        evidence = runtime_dependency_health_evidence(
            payload={"dependencies": {"database": "fail", "email": "pass", "private": "secret"}},
            expectations=expected,
            policy=RuntimeDependencyHealthPolicy(),
        )
        self.assertEqual(evidence.status, "fail")
        self.assertEqual(evidence.results, {"database": "fail", "email": "pass"})

    def test_monitor_staged_adoption_and_failing_dependency(self) -> None:
        self.runtime({"DATABASE_URL": "opaque"})
        target = discover_public_ingress_monitor_targets(self.store, profiles=(self.profile,))[0]
        for mode, payload, expected_status, expected_coverage in (
            ("informational", {}, "pass", "not_covered"),
            ("required", {}, "fail", "not_covered"),
            ("required", {"dependencies": {"database": "fail"}}, "fail", "fail"),
            ("informational", {"dependencies": {"database": "fail"}}, "fail", "fail"),
            ("required", {"dependencies": {"database": "pass"}}, "pass", "pass"),
        ):
            policy = RuntimeDependencyHealthPolicy.model_validate({"mode": mode})
            result = check_public_ingress_target(
                target=replace(target, dependency_policy=policy),
                checked_at=STAMP,
                timeout_seconds=1,
                http_get=lambda url, _: HttpObservation(200, url, 0, payload),
                tls_get=lambda *_: self.fail("Unexpected TLS probe"),
            )
            self.assertEqual(result.status, expected_status)
            health = result.targets[-1].dependency_health
            self.assertIsNotNone(health)
            assert health is not None
            self.assertEqual(health.status, expected_coverage)

    def test_same_base_and_health_url_enforces_dependency_coverage(self) -> None:
        self.runtime({"DATABASE_URL": "opaque"})
        target = discover_public_ingress_monitor_targets(self.store, profiles=(self.profile,))[0]
        target = replace(
            target,
            health_url=target.base_url,
            dependency_policy=RuntimeDependencyHealthPolicy(mode="required"),
        )
        result = check_public_ingress_target(
            target=target,
            checked_at=STAMP,
            timeout_seconds=1,
            http_get=lambda url, _: HttpObservation(200, url, 0, {}),
            tls_get=lambda *_: self.fail("Unexpected TLS probe"),
        )
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.failure_code, "dependency_not_covered")

    def test_read_model_rejects_old_pass_after_adoption_or_new_dependency(self) -> None:
        target = discover_public_ingress_monitor_targets(self.store, profiles=(self.profile,))[0]
        result = check_public_ingress_target(
            target=target,
            checked_at=STAMP,
            timeout_seconds=1,
            http_get=lambda url, _: HttpObservation(200, url, 0, {"dependencies": {}}),
            tls_get=lambda *_: self.fail("Unexpected TLS probe"),
        )
        self.store.write_public_ingress_observation_record(result)
        self.runtime({"DATABASE_URL": "opaque"})
        lane = self.lane.model_copy(
            update={
                "health_monitoring": self.lane.health_monitoring.model_copy(
                    update={"dependency_health": RuntimeDependencyHealthPolicy(mode="required")}
                )
            }
        )
        summary = _health_monitoring_summary(
            record_store=self.store, profile=self.profile, lane=lane
        )
        self.assertEqual(summary.checks[0].status, "missing")
        self.assertIn("not covered", summary.checks[0].summary)
        self.assertEqual(
            summary.dependency_expectations.dependencies if summary.dependency_expectations else (),
            ("database",),
        )

    def test_required_policy_cannot_disable_its_only_http_check(self) -> None:
        request = ProductHealthMonitoringApplyRequest(
            product=self.profile.product,
            context=self.lane.context,
            instance=self.lane.instance,
            check_name="public-ingress",
            monitoring_intent="prelaunch",
            enabled=False,
            reason="Disable probe",
            dependency_health=RuntimeDependencyHealthPolicy(mode="required"),
        )
        with self.assertRaises(ValidationError):
            build_product_health_monitoring_plan(profile=self.profile, request=request)

    def test_reviewed_adoption_changes_plan_digest_and_preserves_policy_on_later_edits(
        self,
    ) -> None:
        request = ProductHealthMonitoringApplyRequest(
            product=self.profile.product,
            context=self.lane.context,
            instance=self.lane.instance,
            check_name="public-ingress",
            monitoring_intent="public",
            reason="Adopt contract",
        )
        original = build_product_health_monitoring_plan(profile=self.profile, request=request)
        required = request.model_copy(
            update={"dependency_health": RuntimeDependencyHealthPolicy(mode="required")}
        )
        plan = build_product_health_monitoring_plan(profile=self.profile, request=required)
        self.assertTrue(plan.changed)
        self.assertNotEqual(original.plan_sha256, plan.plan_sha256)
        updated = updated_product_health_monitoring_profile(
            profile=self.profile, request=required, updated_at=STAMP
        )
        later = updated_product_health_monitoring_profile(
            profile=updated, request=request, updated_at=STAMP
        )
        self.assertEqual(later.lanes[0].health_monitoring.dependency_health.mode, "required")
