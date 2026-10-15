from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    HealthcheckEvidence,
)
from control_plane.contracts.product_profile_record import ProductLaneHealthMonitoringPolicy
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressNotificationDestination,
    PublicIngressNotificationPolicyRecord,
)
from control_plane.health_monitor_scheduler import HealthMonitorScheduler
from control_plane.outbox_worker import run_outbox_worker_once
from control_plane.outbound_http import PublicTlsProbeResult
from control_plane.production_delivery_monitor import (
    DELIVERY_LAG_CHECK,
    monitor_production_delivery_once,
    record_production_delivery_failure,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.public_ingress_monitor import (
    HttpObservation,
    PublicIngressNotificationDriverSet,
    run_public_ingress_monitor_once,
)
from tests.support.stores import _sqlite_database_url
from tests.test_public_ingress_monitor import _profile


class ProductionDeliveryMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = PostgresRecordStore(database_url=_sqlite_database_url(self.root / "store.db"))
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.profile = _profile().model_copy(update={"production_use": "live"})
        self.lane = self.profile.lanes[0]
        self.store.write_product_profile_record(self.profile)
        self.now = datetime.now(timezone.utc)
        self.head = "b" * 40
        self.current = "a" * 40
        self.transport = Mock()
        self.transport.get_json.side_effect = self.read_source
        self.factory = Mock(return_value=self.transport)
        self.store.write_public_ingress_notification_policy_record(
            PublicIngressNotificationPolicyRecord(
                policy_id="existing-owner-alerts",
                product=self.profile.product,
                created_at=self.timestamp(),
                updated_at=self.timestamp(),
                destinations=(
                    PublicIngressNotificationDestination(
                        destination_id="existing-discord",
                        kind="discord",
                        discord_webhook_secret="owner-notifications",
                    ),
                ),
            )
        )
        self.deploy(self.current, minutes_ago=10)

    def timestamp(self) -> str:
        return self.now.isoformat()

    def read_source(self, path: str) -> object:
        if "/commits/" in path:
            return {"sha": self.head}
        return {"status": "behind"}

    def deploy(self, source: str, *, minutes_ago: int = 0, failed: bool = False) -> None:
        self.store.write_deployment_record(
            DeploymentRecord(
                record_id="deploy-" + source,
                context=self.lane.context,
                instance="prod",
                source_git_ref=source,
                artifact_identity=ArtifactIdentityReference(
                    artifact_id="image@" + source,
                ),
                deploy=DeploymentEvidence(
                    deploy_mode="test",
                    target_name="example",
                    target_type="application",
                    status="fail" if failed else "pass",
                    finished_at=(self.now - timedelta(minutes=minutes_ago)).isoformat(),
                ),
                destination_health=HealthcheckEvidence(status="pass"),
            )
        )

    def reject(self, candidate: str = "image@" + "b" * 40) -> None:
        record_production_delivery_failure(
            self.store,
            self.profile,
            self.lane,
            candidate=candidate,
            reason="promotion_required",
            trace_id="launchplane_req_failure",
            observed_at=self.timestamp(),
        )

    def monitor(self) -> None:
        monitor_production_delivery_once(
            self.store,
            observed_at=self.timestamp(),
            transport_factory=self.factory,
            max_age_seconds=60,
        )

    def test_healthy_previous_artifact_does_not_erase_rejection_and_replays_deduplicate(
        self,
    ) -> None:
        self.reject()
        self.reject()
        self.monitor()
        run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=self.timestamp(),
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
                payload={"status": "ok"},
            ),
            tls_get=lambda _domain, _timeout: PublicTlsProbeResult(
                status="unsupported",
                failure_code="tls_unsupported",
                summary="TLS is outside this health fixture.",
            ),
        )
        incidents = self.store.list_public_ingress_incident_records(status="open")
        self.assertEqual(len(incidents), 1)
        self.assertIn("launchplane_req_failure", incidents[0].summary)
        self.assertEqual(len(self.store.list_outbox_delivery_records()), 1)
        self.assertEqual(self.store.list_deployment_records()[0].destination_health.status, "pass")

    def test_notification_failure_retries_and_records_recovery(self) -> None:
        self.reject()
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
        attempts = self.store.list_public_ingress_notification_attempt_records()
        self.assertEqual(attempts[0].delivery_status, "failed")
        queued = self.store.list_outbox_delivery_records()[0]
        self.store.write_outbox_delivery_record(
            queued.model_copy(
                update={
                    "next_attempt_at": self.timestamp(),
                }
            )
        )
        second = run_outbox_worker_once(
            record_store=self.store,
            control_plane_root=self.root,
            lease_owner="test",
            notification_drivers=drivers,
        )
        self.assertEqual(second.status, "delivered")
        self.assertEqual(sender.call_count, 2)
        self.assertIn("launchplane_req_failure", str(sender.call_args))
        self.assertEqual(len(self.store.list_public_ingress_incident_records(status="open")), 1)

    def test_only_a_later_matching_release_clears_failure(self) -> None:
        self.reject()
        self.now += timedelta(minutes=1)
        self.deploy("c" * 40)
        self.monitor()
        self.assertEqual(len(self.store.list_public_ingress_incident_records(status="open")), 1)
        self.now += timedelta(minutes=1)
        self.deploy(self.head)
        self.monitor()
        self.assertEqual(self.store.list_public_ingress_incident_records(status="open"), ())

    def test_terminal_deployment_failure_alerts_without_a_request(self) -> None:
        self.now += timedelta(minutes=1)
        self.deploy(self.head, failed=True)
        self.monitor()
        incident = self.store.list_public_ingress_incident_records(status="open")[0]
        self.assertIn("deploy-" + self.head, incident.summary)

    def test_lag_survives_restart_and_new_merges_without_resetting_deadline(self) -> None:
        self.monitor()
        self.now += timedelta(seconds=61)
        self.head = "c" * 40
        restarted = PostgresRecordStore(database_url=self.store.database_url)
        self.addCleanup(restarted.close)
        monitor_production_delivery_once(
            restarted,
            observed_at=self.timestamp(),
            transport_factory=self.factory,
            max_age_seconds=60,
        )
        incident = self.store.list_public_ingress_incident_records(status="open")[0]
        self.assertEqual(incident.check_name, DELIVERY_LAG_CHECK)
        self.assertEqual(len(self.store.list_outbox_delivery_records()), 1)
        self.monitor()
        self.assertEqual(len(self.store.list_outbox_delivery_records()), 1)
        self.now += timedelta(minutes=1)
        self.deploy(self.head)
        self.monitor()
        self.assertEqual(self.store.list_public_ingress_incident_records(status="open"), ())

    def test_failure_between_polls_is_not_hidden_by_an_unrelated_newer_success(self) -> None:
        self.monitor()
        self.now += timedelta(seconds=10)
        self.deploy(self.head, failed=True)
        self.now += timedelta(seconds=10)
        self.deploy("c" * 40)
        self.monitor()
        incidents = self.store.list_public_ingress_incident_records(status="open")
        self.assertEqual(len(incidents), 1)
        self.assertIn("deploy-" + self.head, incidents[0].summary)

    def test_source_read_failure_cannot_resolve_open_lag(self) -> None:
        self.monitor()
        self.now += timedelta(seconds=61)
        self.monitor()
        self.transport.get_json.side_effect = RuntimeError("source unavailable")
        with self.assertLogs("control_plane.production_delivery_monitor", level="ERROR"):
            self.monitor()
        self.assertEqual(len(self.store.list_public_ingress_incident_records(status="open")), 1)

    def test_testing_rejection_is_ignored(self) -> None:
        record_production_delivery_failure(
            self.store,
            self.profile,
            self.lane.model_copy(update={"instance": "testing"}),
            candidate="example",
            reason="delivery_failed",
            trace_id="test",
        )
        self.assertEqual(self.store.list_public_ingress_incident_records(), ())

    def test_scheduler_deduplicates_completed_delivery_slots_without_health_checks(self) -> None:
        self.store.write_product_profile_record(
            self.profile.model_copy(
                update={
                    "lanes": (
                        self.lane.model_copy(
                            update={
                                "health_monitoring": ProductLaneHealthMonitoringPolicy(
                                    monitoring_intent="prelaunch"
                                )
                            }
                        ),
                    ),
                }
            )
        )
        action = Mock()
        first = HealthMonitorScheduler(
            self.store, clock=lambda: self.now.timestamp(), run_delivery_monitor=action
        )
        second = HealthMonitorScheduler(
            self.store, clock=lambda: self.now.timestamp(), run_delivery_monitor=action
        )
        self.assertTrue(first.run_delivery_once())
        self.assertFalse(second.run_delivery_once())
        action.assert_called_once()
        self.monitor()
        self.now += timedelta(seconds=61)
        self.monitor()
        self.assertEqual(len(self.store.list_public_ingress_incident_records(status="open")), 1)

    def test_delayed_failure_ingestion_is_detected_after_a_later_scan(self) -> None:
        failed_at = self.now
        self.monitor()
        self.now += timedelta(seconds=20)
        self.monitor()
        self.deploy(self.head, failed=True)
        record = self.store.list_deployment_records()[0]
        self.store.write_deployment_record(
            record.model_copy(
                update={
                    "deploy": record.deploy.model_copy(
                        update={"finished_at": failed_at.isoformat()},
                    )
                }
            )
        )
        self.monitor()
        self.assertEqual(len(self.store.list_public_ingress_incident_records(status="open")), 1)

    def test_matching_success_between_polls_resolves_even_with_newer_unrelated_release(
        self,
    ) -> None:
        self.reject()
        self.now += timedelta(seconds=10)
        self.deploy(self.head)
        self.now += timedelta(seconds=10)
        self.deploy("c" * 40)
        self.monitor()
        self.assertEqual(self.store.list_public_ingress_incident_records(status="open"), ())

    def test_delivered_pending_source_resolves_lag_before_timing_newer_main(self) -> None:
        pending = self.head
        self.monitor()
        self.now += timedelta(seconds=61)
        self.head = "c" * 40
        self.monitor()
        self.now += timedelta(seconds=1)
        self.deploy(pending)
        self.monitor()
        self.assertEqual(self.store.list_public_ingress_incident_records(status="open"), ())
        self.now += timedelta(seconds=61)
        self.monitor()
        self.assertEqual(len(self.store.list_public_ingress_incident_records(status="open")), 1)

    def test_prelaunch_product_has_no_automatic_release_deadline(self) -> None:
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"production_use": "prelaunch"})
        )
        self.monitor()
        self.now += timedelta(hours=2)
        self.monitor()
        self.factory.assert_not_called()
        self.assertEqual(self.store.list_public_ingress_incident_records(status="open"), ())
