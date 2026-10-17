from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
    OdooStableTargetReplacementApplyResult,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.product_reconcile import ProductReconcileTarget
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    HealthcheckEvidence,
)
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressNotificationDestination,
    PublicIngressNotificationPolicyRecord,
)
from control_plane.health_monitor_scheduler import HealthMonitorScheduler
from control_plane.outbox_worker import run_outbox_worker_once
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.public_ingress_monitor import (
    HttpObservation,
    PublicIngressNotificationDriverSet,
    run_public_ingress_monitor_once,
)
from tests.support.stores import _sqlite_database_url
from tests.test_public_ingress_monitor import _profile


class TestingDeliveryMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = PostgresRecordStore(database_url=_sqlite_database_url(self.root / "store.db"))
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        base = _profile()
        self.lane = base.lanes[0].model_copy(update={"instance": "testing"})
        self.profile = base.model_copy(update={"lanes": (self.lane,)})
        self.store.write_product_profile_record(self.profile)
        self.now = datetime.now(timezone.utc)
        self.store.write_public_ingress_notification_policy_record(
            PublicIngressNotificationPolicyRecord(
                policy_id="existing-alerts",
                product=self.profile.product,
                created_at=self.now.isoformat(),
                updated_at=self.now.isoformat(),
                destinations=(
                    PublicIngressNotificationDestination(
                        destination_id="existing-discord",
                        kind="discord",
                        discord_webhook_secret="notifications",
                    ),
                ),
            )
        )
        self.deploy("old", self.now - timedelta(minutes=5))

    def deploy(
        self, candidate: str, at: datetime, *, instance: str = "testing", failed: bool = False
    ) -> None:
        self.store.write_deployment_record(
            DeploymentRecord(
                record_id="deploy-" + instance + "-" + candidate,
                context=self.lane.context,
                instance=instance,
                source_git_ref=candidate,
                artifact_identity=ArtifactIdentityReference(artifact_id=candidate),
                deploy=DeploymentEvidence(
                    deploy_mode="test",
                    target_name="example",
                    target_type="application",
                    status="fail" if failed else "pass",
                    finished_at=at.isoformat(),
                ),
                destination_health=HealthcheckEvidence(status="pass"),
            )
        )

    def reconcile(
        self, *, error: str = "failed provider text must stay private", **plan: object
    ) -> None:
        target = ProductReconcileTarget(product=self.profile.product, target_kind="testing")
        self.store.request_product_reconcile(target, self.now.isoformat())
        request = self.store.claim_next_product_reconcile_request(
            "test", 300, now=self.now.isoformat()
        )
        self.assertIsNotNone(request)
        self.store.complete_product_reconcile_request(
            target.target_key,
            "test",
            "failed" if error else "done",
            {"target": "testing", "desired_artifact_id": "new", **plan},
            error,
            now=self.now.isoformat(),
        )

    def monitor(self) -> None:
        scheduler = HealthMonitorScheduler(self.store, clock=lambda: self.now.timestamp())
        scheduler._run_delivery_monitor()

    def test_failed_reconcile_alerts_despite_healthy_old_build_and_deduplicates(self) -> None:
        self.reconcile(
            last_failed_error_code="plan_not_ready.compose_or_override_render_failed",
            failure_trace_id="launchplane_req_render",
        )
        self.monitor()
        self.monitor()
        incidents = self.store.list_public_ingress_incident_records(status="open")
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].instance, "testing")
        self.assertIn("compose_or_override_render_failed", incidents[0].summary)
        self.assertIn("launchplane_req_render", incidents[0].summary)
        self.assertIn("path-check", incidents[0].summary)
        self.assertNotIn("private", incidents[0].summary)
        self.assertEqual(len(self.store.list_outbox_delivery_records()), 1)
        run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=self.now.isoformat(),
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200, final_url=url, redirect_count=0, payload={"status": "ok"}
            ),
        )
        self.assertEqual(len(self.store.list_public_ingress_incident_records(status="open")), 1)
        self.assertTrue(
            all(
                record.destination_health.status == "pass"
                for record in self.store.list_deployment_records()
            )
        )

    def test_only_later_matching_testing_deployment_recovers(self) -> None:
        self.reconcile(last_failed_error_code="deployment_failed")
        self.monitor()
        self.assertEqual(len(self.store.list_public_ingress_incident_records(status="open")), 1)
        self.now += timedelta(minutes=1)
        self.deploy("unrelated", self.now)
        self.deploy("new", self.now, instance="prod")
        self.monitor()
        self.assertEqual(len(self.store.list_public_ingress_incident_records(status="open")), 1)
        self.now += timedelta(minutes=1)
        self.deploy("new", self.now)
        self.reconcile(error="", action="none", reason="already_deployed")
        self.monitor()
        self.assertEqual(self.store.list_public_ingress_incident_records(status="open"), ())

    def test_expected_hold_or_guard_refusal_never_pages(self) -> None:
        for plan in (
            {"held": True, "reason": "staff_testing_hold"},
            {"last_failed_error_code": "deploy_blocked.retained_database_credential_changed"},
        ):
            with self.subTest(plan=plan):
                self.reconcile(**plan)
                self.monitor()
                self.assertEqual(self.store.list_public_ingress_incident_records(), ())
                self.assertEqual(self.store.list_outbox_delivery_records(), ())

    def test_notification_transport_failure_retries_without_duplicate_incident(self) -> None:
        self.reconcile(last_failed_error_code="deployment_failed")
        self.monitor()
        sender = Mock(side_effect=[RuntimeError("transport failed"), None])
        drivers = PublicIngressNotificationDriverSet(
            secret_resolver=lambda _key: "https://discord.com/api/webhooks/example/test",
            discord_sender=sender,
        )
        first = run_outbox_worker_once(
            record_store=self.store,
            control_plane_root=self.root,
            lease_owner="test",
            notification_drivers=drivers,
        )
        self.assertEqual(first.status, "pending")
        self.assertEqual(
            self.store.list_public_ingress_notification_attempt_records()[0].delivery_status,
            "failed",
        )
        queued = self.store.list_outbox_delivery_records()[0]
        self.store.write_outbox_delivery_record(
            queued.model_copy(update={"next_attempt_at": self.now.isoformat()})
        )
        self.monitor()
        second = run_outbox_worker_once(
            record_store=self.store,
            control_plane_root=self.root,
            lease_owner="test",
            notification_drivers=drivers,
        )
        self.assertEqual(second.status, "delivered")
        self.assertEqual(sender.call_count, 2)
        incident = self.store.list_public_ingress_incident_records()[0]
        self.assertIn(incident.incident_id, str(sender.call_args))
        self.assertEqual(len(self.store.list_public_ingress_incident_records()), 1)
        self.assertEqual(
            self.store.list_public_ingress_notification_attempt_records()[0].delivery_status,
            "delivered",
        )
        self.assertEqual(self.store.list_outbox_delivery_records()[0].attempt, 2)

    def test_failed_render_operation_is_detected_before_reconcile_retries(self) -> None:
        operation = OdooStableTargetReplacementOperationRecord(
            operation_id="render-failed",
            product=self.profile.product,
            context=self.lane.context,
            instance="testing",
            idempotency_key="attempt-1",
            request_fingerprint="fixture",
            request=OdooStableTargetReplacementApplyRequest(
                product=self.profile.product,
                instance="testing",
                strategy="recreate-in-place",
                artifact_id="new",
                source_git_ref="new",
            ),
            status="fail",
            phase="failed",
            created_at=self.now.isoformat(),
            updated_at=self.now.isoformat(),
            finished_at=self.now.isoformat(),
            runner_trace_id="launchplane_req_render",
            error_code="plan_not_ready.compose_or_override_render_failed",
            error_message="password=private-provider-detail",
        )
        self.store.write_odoo_stable_target_replacement_operation_record(operation)
        self.reconcile(error="", queued_operation_id=operation.operation_id)
        self.monitor()
        incident = self.store.list_public_ingress_incident_records(status="open")[0]
        self.assertIn(operation.error_code, incident.summary)
        self.assertIn(operation.runner_trace_id, incident.summary)
        self.assertNotIn(operation.error_message, incident.summary)
        self.assertEqual(incident.severity, "warning")
        self.now += timedelta(seconds=1)
        self.store.write_odoo_stable_target_replacement_operation_record(
            operation.model_copy(
                update={
                    "error_code": "",
                    "result": OdooStableTargetReplacementApplyResult(
                        product=self.profile.product,
                        context=self.lane.context,
                        instance="testing",
                        strategy="recreate-in-place",
                        deploy_status="pass",
                        post_deploy_status="pass",
                        health_status="fail",
                        target_id="example",
                        target_name="example",
                    ),
                }
            )
        )
        self.monitor()
        latest = self.store.list_public_ingress_incident_records(status="open")[0]
        self.assertIn("health_check_failed", latest.summary)

    def test_attention_persistence_failure_is_retried_after_restart(self) -> None:
        self.reconcile(last_failed_error_code="deployment_failed")
        with (
            patch(
                "control_plane.testing_delivery_monitor._observe",
                side_effect=RuntimeError("storage unavailable"),
            ),
            self.assertLogs("control_plane.testing_delivery_monitor", "ERROR"),
        ):
            self.monitor()
        self.assertEqual(self.store.list_public_ingress_incident_records(), ())
        database_url = self.store.database_url
        self.store.close()
        self.store = PostgresRecordStore(database_url=database_url)
        self.addCleanup(self.store.close)
        self.monitor()
        self.assertEqual(len(self.store.list_public_ingress_incident_records()), 1)
        self.assertEqual(len(self.store.list_outbox_delivery_records()), 1)

    def test_failed_testing_evidence_read_does_not_stop_production_monitor(self) -> None:
        with (
            patch.object(
                self.store,
                "read_product_reconcile_request",
                side_effect=RuntimeError("read failed"),
            ),
            patch(
                "control_plane.production_delivery_monitor.monitor_production_delivery_once"
            ) as production,
            self.assertLogs("control_plane.testing_delivery_monitor", "ERROR"),
        ):
            self.monitor()
        production.assert_called_once()

    def test_preplan_failure_recovers_only_after_a_successful_testing_reconcile(self) -> None:
        self.reconcile(desired_artifact_id="", last_failed_error_code="testing_reconcile_failed")
        self.monitor()
        self.assertEqual(len(self.store.list_public_ingress_incident_records(status="open")), 1)
        self.now += timedelta(minutes=1)
        self.deploy("new", self.now)
        self.reconcile(error="", reason="already_deployed")
        self.monitor()
        self.assertEqual(self.store.list_public_ingress_incident_records(status="open"), ())

    def test_a_newer_failure_is_not_erased_by_an_earlier_matching_success(self) -> None:
        self.reconcile(last_failed_error_code="deployment_failed")
        self.monitor()
        self.now += timedelta(minutes=1)
        self.deploy("new", self.now)
        self.now += timedelta(minutes=1)
        self.reconcile(last_failed_error_code="deployment_failed")
        self.monitor()
        self.assertEqual(len(self.store.list_public_ingress_incident_records(status="open")), 1)
