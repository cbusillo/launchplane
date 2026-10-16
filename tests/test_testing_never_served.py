"""Lifetime generation qualification rejects served-once and missing evidence."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressObservationRecord,
    PublicIngressTargetObservation,
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
                "namespace": self.fixture.resources.namespace,
                "resources_binding_sha256": self.fixture.resources.binding_sha256,
                "prepared_at": self.at.isoformat(),
                "non_use_tracking_started_at": self.at.isoformat(),
            }
        )
        self.store.write_fresh_odoo_testing_generation(self.gen)
        for kind in ("public_http", "private_http"):
            self.store.write_public_ingress_observation_record(self.check(kind))

    def check(self, kind: str, *, passed: bool = False) -> PublicIngressObservationRecord:
        return PublicIngressObservationRecord.model_validate(
            {
                "record_id": f"{kind}-{'pass' if passed else 'fail'}",
                "product": self.product,
                "context": self.context,
                "instance": "testing",
                "check_kind": kind,
                "base_url": f"https://{self.gen.domain_host}",
                "observed_at": (self.at + timedelta(seconds=1)).isoformat(),
                "status": "pass" if passed else "fail",
                "failure_code": None if passed else "connection_timeout",
                "targets": [
                    PublicIngressTargetObservation(
                        target="base_url",
                        url=f"https://{self.gen.domain_host}",
                        status="pass" if passed else "fail",
                        failure_code=None if passed else "connection_timeout",
                        summary="fixture check",
                    )
                ],
                "summary": "fixture check",
            }
        )

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
                    (result["data"], result["production_resources"]), ("unknown", "unknown")
                )
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
