"""Lifetime generation qualification rejects served-once and missing evidence."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest
from typing import cast

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.contracts.product_profile_record import (
    ProductLaneHealthCheck,
    ProductLaneHealthMonitoringPolicy,
)
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressObservationRecord,
)
from control_plane.odoo_testing_usage import OdooTestingUsage
from control_plane.odoo_testing_restore import generation_binding
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from control_plane.testing_never_served import (
    NON_USE_EVIDENCE_PREFIX,
    qualify_never_served,
    read_never_served_testing_usage,
    NeverServedRecords,
)
from tests.support.disposable_lanes import record_safety
from tests.test_odoo_testing_restore import generation
from tests.test_testing_replacement_bootstrap import FreshTestingBootstrapScenario
from control_plane.workflows.public_ingress_monitor import (
    HttpObservation,
    check_public_ingress_target,
    effective_health_check_monitor_target,
)


class NeverServedTestingTests(unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        self.fixture = FreshTestingBootstrapScenario()
        self.fixture.seed(self.database_url)
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.product = self.fixture.profile.product
        self.context = self.fixture.fixture.context
        self.at = datetime.now(timezone.utc)
        self.gen = generation().model_copy(
            update={
                "product": self.product,
                "context": self.context,
                "operation_id": self.fixture.fixture.operation.operation_id,
                "target_id": self.fixture.resources.target_id,
                "domain_host": "testing.example.com",
                "namespace": self.fixture.resources.namespace,
                "resources_binding_sha256": self.fixture.resources.binding_sha256,
                "prepared_at": self.at.isoformat(),
                "non_use_tracking_started_at": self.at.isoformat(),
            }
        )
        self.store.write_fresh_odoo_testing_generation(self.gen)
        self.profile = self.fixture.profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(
                        update={
                            "base_url": f"https://{self.gen.domain_host}",
                            "health_url": "",
                            "health_monitoring": ProductLaneHealthMonitoringPolicy(
                                monitoring_intent="public",
                                checks=(
                                    ProductLaneHealthCheck(
                                        name="public",
                                        kind="public_http",
                                        require_runtime_identity=False,
                                    ),
                                    ProductLaneHealthCheck(
                                        name="private",
                                        kind="private_http",
                                        private_endpoint_key="testing-private",
                                        require_runtime_identity=False,
                                    ),
                                ),
                            ),
                        }
                    )
                    if lane.instance == "testing"
                    else lane
                    for lane in self.fixture.profile.lanes
                )
            }
        )
        self.store.write_product_profile_record(self.profile)
        self.store.write_private_health_endpoint_record(
            PrivateHealthEndpointRecord(
                endpoint_key="testing-private",
                product=self.product,
                context=self.context,
                instance="testing",
                url="http://127.0.0.1:8069/health",
                updated_at=self.at.isoformat(),
            )
        )
        for kind in ("public_http", "private_http"):
            self.store.write_public_ingress_observation_record(self.check(kind))

    def check(self, kind: str, *, passed: bool = False) -> PublicIngressObservationRecord:
        lane = next(lane for lane in self.profile.lanes if lane.instance == "testing")
        check = next(check for check in lane.health_monitoring.checks if check.kind == kind)
        target = effective_health_check_monitor_target(
            profile=self.profile, lane=lane, check=check, record_store=self.store
        )
        assert target is not None

        def http_get(url: str, _timeout: int) -> HttpObservation:
            if not passed:
                raise TimeoutError("fixture unavailable")
            return HttpObservation(status_code=200, final_url=url, redirect_count=0)

        return check_public_ingress_target(
            target=target,
            checked_at=(self.at + timedelta(seconds=1)).isoformat(),
            timeout_seconds=1,
            http_get=http_get,
            tls_get=lambda *_: self.fail("HTTP monitoring does not probe TLS independently"),
        ).model_copy(update={"record_id": f"{kind}-{'pass' if passed else 'fail'}"})

    def test_private_checks_bind_to_endpoint_authority_without_a_public_hostname(self) -> None:
        private = self.check("private_http")
        self.assertFalse(private.base_url)
        self.assertEqual(self.read().usage, "unused")
        records = self.snapshot()
        self.assertEqual(qualify_never_served(replace(records, private_checks=())).usage, "unknown")
        self.assertEqual(
            read_never_served_testing_usage(
                object(), product=self.product, context=self.context, target_id=self.gen.target_id
            ).usage,
            "unknown",
        )

    def test_public_coverage_cannot_include_an_explicit_foreign_health_url(self) -> None:
        records = self.snapshot()
        public = self.check("public_http")
        foreign = public.model_copy(
            update={
                "health_url": "https://old.example.invalid/health",
                "targets": tuple(
                    t.model_copy(update={"url": "https://old.example.invalid/health"})
                    if t.target == "health_url"
                    else t
                    for t in public.targets
                ),
            }
        )
        self.assertEqual(
            qualify_never_served(
                replace(records, checks=(self.check("private_http"), foreign))
            ).usage,
            "unknown",
        )

    def test_replacement_binding_adapter_rechecks_the_locked_generation_snapshot(self) -> None:
        from control_plane.disposable_testing_rebuild import ReplacementBindings
        from control_plane.disposable_lanes import (
            require_disposable_lane,
            DisposableLaneSafetyUnknown,
            DisposableLaneStore,
        )

        records = self.snapshot()
        proof = self.read()
        record_safety(
            self.store,
            self.product,
            self.context,
            "testing",
            usage_evidence=NON_USE_EVIDENCE_PREFIX + proof.proof_sha256,
        )
        evidence = self.store.read_disposable_lane_safety(self.product, self.context, "testing")
        assert evidence is not None
        bindings = ReplacementBindings(
            profile=self.profile,
            target=records.target,
            target_id=self.store.read_dokploy_target_id_record(
                context_name=self.context, instance_name="testing"
            ),
            all_target_ids=self.store.list_dokploy_target_id_records(),
            runtime_records=self.store.list_runtime_environment_records(),
            secret_records=self.store.list_secret_records(integration="runtime_environment"),
            secret_bindings=self.store.list_secret_bindings(integration="runtime_environment"),
            safety=evidence,
            preparation=records.allocation,
            non_use_records=records,
        )
        self.assertTrue(
            require_disposable_lane(
                cast(DisposableLaneStore, bindings),
                product=self.product,
                context=self.context,
                instance="testing",
            )
        )
        bindings.non_use_records = replace(
            records, generation=self.gen.model_copy(update={"non_use_invalidated": True})
        )
        with self.assertRaises(DisposableLaneSafetyUnknown):
            require_disposable_lane(
                cast(DisposableLaneStore, bindings),
                product=self.product,
                context=self.context,
                instance="testing",
            )

    def test_pending_testing_deployment_permanently_revokes_a_published_proof(self) -> None:
        proof = self.read()
        record_safety(
            self.store,
            self.product,
            self.context,
            "testing",
            usage_evidence=NON_USE_EVIDENCE_PREFIX + proof.proof_sha256,
        )
        self.store.write_deployment_record(
            DeploymentRecord(
                record_id="pending-before-provider",
                context=self.context,
                instance="testing",
                source_git_ref="fixture",
                deploy=DeploymentEvidence(
                    target_name="fixture",
                    target_type="compose",
                    deploy_mode="compose",
                    status="pending",
                    started_at=self.at.isoformat(),
                ),
            )
        )
        current = self.store.read_fresh_odoo_testing_generation(self.product, self.context)
        assert current is not None
        self.assertTrue(current.non_use_invalidated)
        self.assertIsNone(
            self.store.read_disposable_lane_safety(self.product, self.context, "testing")
        )
        self.assertEqual(self.read().usage, "unknown")

    def read(self) -> OdooTestingUsage:
        return read_never_served_testing_usage(
            self.store, product=self.product, context=self.context, target_id=self.gen.target_id
        )

    def snapshot(self) -> NeverServedRecords:
        records = self.store.read_testing_non_use_records(
            self.product, self.context, self.gen.target_id
        )
        assert records is not None
        return records

    def test_complete_unopened_generation_is_unused_without_inventing_runtime_reads(self) -> None:
        result = self.read()
        self.assertEqual(result.usage, "unused")
        self.assertEqual(result.reason_code, "never_served")
        self.assertIsNone(result.recent_signins)
        self.assertIsNone(result.authenticated_sessions)
        self.assertTrue(result.proof_sha256)
        self.assertEqual(self.read(), result)

    def test_served_once_stays_unknown_even_after_later_failed_checks(self) -> None:
        for kind in ("public_http", "private_http"):
            with self.subTest(kind=kind):
                records = self.snapshot()
                checks = (*records.checks, self.check(kind, passed=True), self.check(kind))
                self.assertEqual(
                    qualify_never_served(replace(records, checks=checks)).usage, "unknown"
                )
        records = self.snapshot()
        deployed = DeploymentRecord(
            record_id="served-once",
            context=self.context,
            instance="testing",
            source_git_ref="fixture",
            deploy=DeploymentEvidence(
                target_name="fixture",
                target_type="compose",
                deploy_mode="compose",
                status="fail",
                started_at=self.at.isoformat(),
            ),
        )
        self.assertEqual(
            qualify_never_served(replace(records, deployments=(deployed,))).usage, "unknown"
        )

    def test_missing_legacy_or_incomplete_records_are_not_negative_proof(self) -> None:
        records = self.snapshot()
        for change in (
            {"checks": ()},
            {"checks": records.checks[:1]},
            {"generation": self.gen.model_copy(update={"non_use_tracking_started_at": ""})},
            {"generation": self.gen.model_copy(update={"completed_at": self.at.isoformat()})},
            {"generation": self.gen.model_copy(update={"non_use_invalidated": True})},
            {"generation": self.gen.model_copy(update={"resources_binding_sha256": "e" * 64})},
            {"generation": self.gen.model_copy(update={"prepared_at": "unreadable"})},
        ):
            with self.subTest(change=change):
                self.assertEqual(qualify_never_served(replace(records, **change)).usage, "unknown")
        self.assertEqual(
            read_never_served_testing_usage(
                self.store, product=self.product, context=self.context, target_id="other-target"
            ).usage,
            "unknown",
        )

    def test_dispatch_revokes_published_proof_and_a_delayed_publish_is_rejected(self) -> None:
        proof = self.read()
        record_safety(
            self.store,
            self.product,
            self.context,
            "testing",
            usage_evidence=NON_USE_EVIDENCE_PREFIX + proof.proof_sha256,
        )
        evidence = self.store.read_disposable_lane_safety(self.product, self.context, "testing")
        assert evidence is not None
        self.assertTrue(self.store.publish_collected_disposable_lane_safety(evidence))
        self.store.invalidate_testing_non_use(self.product, self.context)
        self.assertEqual(self.read().usage, "unknown")
        self.assertIsNone(
            self.store.read_disposable_lane_safety(self.product, self.context, "testing")
        )
        self.assertFalse(self.store.publish_collected_disposable_lane_safety(evidence))
        # A stale completion writer cannot erase lifetime revocation.
        self.store.write_fresh_odoo_testing_generation(self.gen)
        self.assertEqual(self.read().usage, "unknown")

    def hold_cycle(self, writer: str) -> None:
        original = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        held = original.model_copy(
            update={
                "policies": original.policies.model_copy(
                    update={
                        "staff_testing_hold": DokployTargetStaffTestingHold(reason="staff testing")
                    }
                )
            }
        )
        self.assertEqual(self.read().usage, "unused")
        if writer == "plain":
            self.store.write_dokploy_target_record(held)
        elif writer == "bundle":
            self.store.write_product_authority_bundle(
                ProductAuthorityBundle(dokploy_targets=(held,))
            )
        else:
            self.store.compare_and_write_dokploy_target_record(
                expected_record=original, replacement_record=held
            )
        self.store.write_dokploy_target_record(original)
        self.assertEqual(self.read().usage, "unknown")
        current = self.store.read_fresh_odoo_testing_generation(self.product, self.context)
        assert current is not None
        self.assertEqual(generation_binding(self.gen), generation_binding(current))

    def test_set_then_lifted_hold_revokes_plain_target_writes(self) -> None:
        self.hold_cycle("plain")

    def test_set_then_lifted_hold_revokes_bundle_target_writes(self) -> None:
        self.hold_cycle("bundle")

    def test_set_then_lifted_hold_revokes_compare_target_writes(self) -> None:
        self.hold_cycle("compare")

    def test_hold_already_set_at_creation_cannot_be_forgotten_after_lifting(self) -> None:
        from control_plane.storage.postgres import LaunchplaneFreshOdooTestingGenerationRow

        # Model the bootstrap's first write after a hold raced its earlier check.
        with self.store._session_factory() as session:
            row = session.get(
                LaunchplaneFreshOdooTestingGenerationRow, (self.product, self.context)
            )
            assert row is not None
            session.delete(row)
            session.commit()
        original = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        self.store.write_dokploy_target_record(
            original.model_copy(
                update={
                    "policies": original.policies.model_copy(
                        update={
                            "staff_testing_hold": DokployTargetStaffTestingHold(
                                reason="hold at creation"
                            )
                        }
                    )
                }
            )
        )
        self.store.write_fresh_odoo_testing_generation(self.gen)
        self.store.write_dokploy_target_record(original)
        self.assertEqual(self.read().usage, "unknown")

    def test_delayed_positive_use_revokes_a_newer_negative_even_when_rejected(self) -> None:
        proof = self.read()
        record_safety(
            self.store,
            self.product,
            self.context,
            "testing",
            usage_evidence="service:later-runtime-negative",
            observed_at=self.at + timedelta(seconds=10),
        )
        negative = self.store.read_disposable_lane_safety(self.product, self.context, "testing")
        assert negative is not None
        positive = negative.model_copy(
            update={
                "usage": "in_use",
                "usage_evidence": "service:positive-use",
                "observed_at": self.at,
            }
        )
        self.assertFalse(self.store.publish_collected_disposable_lane_safety(positive))
        self.assertEqual(self.read().usage, "unknown")
        self.assertFalse(
            self.store.publish_collected_disposable_lane_safety(
                negative.model_copy(
                    update={"usage_evidence": NON_USE_EVIDENCE_PREFIX + proof.proof_sha256}
                )
            )
        )

    def test_additional_failed_checks_do_not_change_proof_but_a_pass_revokes_it(self) -> None:
        proof = self.read()
        old = self.check("public_http").model_copy(
            update={"record_id": "old-host-failure", "base_url": "https://old.example.invalid"}
        )
        self.store.write_public_ingress_observation_record(old)
        new = self.check("public_http").model_copy(update={"record_id": "new-failure"})
        self.store.write_public_ingress_observation_record(new)
        self.assertEqual(self.read(), proof)
        self.store.write_public_ingress_observation_record(self.check("private_http", passed=True))
        self.assertEqual(self.read().usage, "unknown")

    def test_known_use_cannot_be_replaced_with_never_served_proof(self) -> None:
        record_safety(self.store, self.product, self.context, "testing", usage="in_use")
        self.assertEqual(self.read().usage, "unknown")


class NeverServedHttpTests(unittest.IsolatedAsyncioTestCase):
    database_url = ""
    revocation_protections: tuple[dict[str, str], ...] = (
        {"data": "only_copy", "production_resources": "unknown"},
        {"data": "unknown", "production_resources": "shared"},
        {"data": "unknown", "production_resources": "separation_ready"},
    )

    async def test_usage_revocation_preserves_independent_copy_and_resource_protection(
        self,
    ) -> None:
        from unittest.mock import patch
        from tests.support.http import request

        for changes in self.revocation_protections:
            with self.subTest(protection=changes):
                fixture = NeverServedTestingTests()
                fixture.database_url = self.database_url
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                app = fixture.fixture.fixture.app
                previous = fixture.store.read_disposable_lane_safety(
                    fixture.product, fixture.context, "testing"
                )
                assert previous is not None
                fixture.store.write_disposable_lane_safety(previous.model_copy(update=changes))
                body = {
                    "product": fixture.product,
                    "context": fixture.context,
                    "instance": "testing",
                }

                async def collect() -> dict[str, object]:
                    inspected = await request(
                        app,
                        "POST",
                        "/v1/disposable-lanes/observations",
                        headers={"Authorization": "Bearer local-operator-token"},
                        payload=body,
                    )
                    response = await request(
                        app,
                        "POST",
                        "/v1/disposable-lanes/observations",
                        headers={"Authorization": "Bearer local-operator-token"},
                        payload={
                            **body,
                            "mode": "collect",
                            "expected_binding_sha256": inspected.json()["result"]["binding_sha256"],
                        },
                    )
                    self.assertEqual(response.status_code, 202, response.text)
                    return dict(response.json()["result"])

                with patch(
                    "control_plane.http_app.read_odoo_testing_usage",
                    return_value=OdooTestingUsage(
                        reason_code="serving_container_absent",
                        never_served_candidate=True,
                    ),
                ):
                    first = await collect()
                with patch(
                    "control_plane.http_app.read_odoo_testing_usage",
                    return_value=OdooTestingUsage(
                        "in_use",
                        True,
                        True,
                        "complete",
                    ),
                ):
                    positive = await collect()
                self.assertEqual(first["usage"], "unused")
                self.assertEqual(first["data"], changes["data"])
                self.assertEqual(first["production_resources"], changes["production_resources"])
                self.assertEqual(positive["data"], first["data"])
                self.assertEqual(positive["production_resources"], first["production_resources"])
                self.assertEqual(positive["usage"], "in_use")
                self.assertEqual(fixture.read().usage, "unknown")
                fixture.store.clear_disposable_lane_safety(
                    fixture.product, fixture.context, "testing"
                )
                self.assertEqual(fixture.read().usage, "unknown")

    async def test_unavailable_runtime_uses_only_complete_service_records(self) -> None:
        from unittest.mock import patch
        from tests.support.http import request

        fixture = NeverServedTestingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        app = fixture.fixture.fixture.app
        body = {"product": fixture.product, "context": fixture.context, "instance": "testing"}
        with patch(
            "control_plane.http_app.read_odoo_testing_usage",
            return_value=OdooTestingUsage(
                reason_code="serving_container_absent", never_served_candidate=True
            ),
        ):
            for revoked in (False, True):
                if revoked:
                    fixture.store.invalidate_testing_non_use(fixture.product, fixture.context)
                previous = fixture.store.read_disposable_lane_safety(
                    fixture.product, fixture.context, "testing"
                )
                inspected = await request(
                    app,
                    "POST",
                    "/v1/disposable-lanes/observations",
                    headers={"Authorization": "Bearer local-operator-token"},
                    payload=body,
                )
                self.assertEqual(inspected.status_code, 202, inspected.text)
                response = await request(
                    app,
                    "POST",
                    "/v1/disposable-lanes/observations",
                    headers={"Authorization": "Bearer local-operator-token"},
                    payload={
                        **body,
                        "mode": "collect",
                        "expected_binding_sha256": inspected.json()["result"]["binding_sha256"],
                    },
                )
                self.assertEqual(response.status_code, 202, response.text)
                result = response.json()["result"]
                self.assertEqual(result["usage"], "unknown" if revoked else "unused")
                self.assertEqual(
                    result["production_resources"],
                    previous.production_resources
                    if previous is not None
                    else inspected.json()["result"]["production_resources"],
                )
                self.assertEqual(result["data"], "unknown")
                self.assertIsNone(result["odoo_usage"]["recent_signins"])
                self.assertIsNone(result["odoo_usage"]["authenticated_sessions"])
                self.assertIs(result["provider_mutation"], False)

    async def test_running_or_unverified_runtime_cannot_be_overridden_by_durable_records(
        self,
    ) -> None:
        from unittest.mock import patch
        from tests.support.http import request

        fixture = NeverServedTestingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.assertEqual(fixture.read().usage, "unused")
        body = {"product": fixture.product, "context": fixture.context, "instance": "testing"}
        for reason in (
            "web_configuration_unavailable",
            "session_file_unreadable",
            "login_history_unavailable",
            "runtime_changed",
            "serving_history_present",
            "serving_process_absent",
        ):
            with (
                self.subTest(reason=reason),
                patch(
                    "control_plane.http_app.read_odoo_testing_usage",
                    return_value=OdooTestingUsage(reason_code=reason),
                ),
            ):
                inspected = await request(
                    fixture.fixture.fixture.app,
                    "POST",
                    "/v1/disposable-lanes/observations",
                    headers={"Authorization": "Bearer local-operator-token"},
                    payload=body,
                )
                response = await request(
                    fixture.fixture.fixture.app,
                    "POST",
                    "/v1/disposable-lanes/observations",
                    headers={"Authorization": "Bearer local-operator-token"},
                    payload={
                        **body,
                        "mode": "collect",
                        "expected_binding_sha256": inspected.json()["result"]["binding_sha256"],
                    },
                )
                self.assertEqual(response.status_code, 202, response.text)
                self.assertEqual(response.json()["result"]["usage"], "unknown")
                self.assertEqual(
                    response.json()["result"]["never_served_qualification"],
                    "never_served_not_attempted",
                )
