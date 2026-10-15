from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import DEFAULT, Mock, patch

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_environment_read_model import (
    ProductEnvironmentDetail,
    build_product_environment_detail,
)
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    HealthcheckEvidence,
    PromotionRecord,
)
from control_plane.contracts.product_profile_record import (
    ProductLaneHealthMonitoringPolicy,
    ProductOwnerProfile,
)
from control_plane.contracts.public_ingress_monitoring import (
    RETIRED_PRODUCTION_DELIVERY_LAG_CHECK,
    PublicIngressNotificationDestination,
    PublicIngressNotificationPolicyRecord,
)
from control_plane.health_monitor_scheduler import HealthMonitorScheduler
from control_plane.outbox_worker import run_outbox_worker_once
from control_plane.outbound_http import PublicTlsProbeResult
from control_plane.production_delivery_monitor import (
    monitor_production_delivery_once,
    record_production_delivery_failure,
    _record_rejected_payload,
    _observe,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.public_ingress_monitor import (
    HttpObservation,
    PublicIngressNotificationDriverSet,
    deliver_public_ingress_notification_outbox_delivery,
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
        )

    def seed_legacy_lag(self) -> None:
        _observe(
            self.store,
            self.profile,
            self.lane,
            check_name=RETIRED_PRODUCTION_DELIVERY_LAG_CHECK,
            failed=True,
            summary="Merged source has not reached production; Client acceptance pending.",
            observed_at=self.timestamp(),
        )

    def test_waiting_merged_source_never_opens_a_failure_or_notification(self) -> None:
        self.profile = self.profile.model_copy(
            update={
                "owner": ProductOwnerProfile(github_login="example-client", github_id="1234"),
                "release_on_acceptance": "promote",
            }
        )
        self.store.write_product_profile_record(self.profile)
        self.transport.get_json.side_effect = lambda _path: {
            "sha": self.head,
            "commit": {"committer": {"date": (self.now - timedelta(days=7)).isoformat()}},
        }
        self.monitor()
        self.now += timedelta(days=1)
        self.head = "c" * 40
        self.store.close()
        self.store = PostgresRecordStore(database_url=self.store.database_url)
        self.addCleanup(self.store.close)
        self.monitor()
        self.assertEqual(self.store.list_public_ingress_incident_records(status="open"), ())
        self.assertEqual(self.store.list_outbox_delivery_records(), ())

    def test_legacy_lag_retires_without_recovering_a_real_delivery_failure(self) -> None:
        self.seed_legacy_lag()
        legacy = self.store.list_public_ingress_incident_records(status="open")[0]
        self.reject()
        before = self.store.list_outbox_delivery_records()
        self.now += timedelta(seconds=1)
        self.monitor()
        incidents = self.store.list_public_ingress_incident_records(status="open")
        self.assertEqual(len(incidents), 1)
        self.assertIn("launchplane_req_failure", incidents[0].summary)
        self.assertEqual(self.store.list_outbox_delivery_records(), before)
        retired = self.store.list_public_ingress_incident_records(status="resolved")
        self.assertEqual(retired[0].check_name, RETIRED_PRODUCTION_DELIVERY_LAG_CHECK)
        self.assertIn(legacy.summary, retired[0].summary)
        self.now += timedelta(days=1)
        self.monitor()
        self.assertEqual(
            {
                incident.incident_id
                for incident in self.store.list_public_ingress_incident_records(status="open")
            },
            {incidents[0].incident_id},
        )

    def test_queued_legacy_lag_never_reaches_discord_even_before_monitor_runs(self) -> None:
        self.seed_legacy_lag()
        sender = Mock()
        resolver = Mock(return_value="https://discord.com/api/webhooks/example/test")
        result = run_outbox_worker_once(
            record_store=self.store,
            control_plane_root=self.root,
            lease_owner="test",
            notification_drivers=PublicIngressNotificationDriverSet(
                secret_resolver=resolver,
                discord_sender=sender,
            ),
        )
        sender.assert_not_called()
        resolver.assert_not_called()
        self.assertEqual(result.status, "delivered")
        attempts = self.store.list_public_ingress_notification_attempt_records()
        self.assertEqual(attempts[0].delivery_status, "skipped")

    @patch.dict(
        "os.environ",
        {
            "LAUNCHPLANE_MASTER_ENCRYPTION_KEY": "incident-test-key",
            "LAUNCHPLANE_SECRET_KEYS_JSON": "",
        },
    )
    def test_retired_lag_is_absent_from_the_environment_used_by_the_ui(self) -> None:
        self.seed_legacy_lag()

        def detail() -> ProductEnvironmentDetail:
            return build_product_environment_detail(
                record_store=self.store,
                product=self.profile.product,
                environment="prod",
                action_allowed=lambda *_: False,
            )

        self.assertTrue(detail().health_monitoring.open_incidents)
        self.now += timedelta(seconds=1)
        self.monitor()
        self.assertEqual(detail().health_monitoring.open_incidents, ())

    def test_retired_lag_with_an_interrupted_send_preserves_unknown_outcome(self) -> None:
        self.seed_legacy_lag()
        queued = self.store.list_outbox_delivery_records()[0].model_copy(
            update={"provider_operation_key": "interrupted-send", "provider_id": "discord"}
        )
        sender = Mock()
        started = Mock()
        result = deliver_public_ingress_notification_outbox_delivery(
            record=queued,
            record_store=self.store,
            drivers=PublicIngressNotificationDriverSet(discord_sender=sender),
            mark_provider_started=started,
        )
        self.assertEqual(result.state, "failed")
        self.assertTrue(result.error_code)
        self.assertEqual(result.provider_operation_key, queued.provider_operation_key)
        sender.assert_not_called()
        started.assert_not_called()

    def test_retirement_failure_does_not_interrupt_real_failure_detection(self) -> None:
        self.seed_legacy_lag()
        self.now += timedelta(seconds=1)
        self.deploy(self.head, failed=True)

        def observe(*_args: object, **kwargs: object) -> object:
            if kwargs.get("retired"):
                raise RuntimeError("Incident changed during retirement")
            return DEFAULT

        with (
            patch(
                "control_plane.production_delivery_monitor._observe",
                side_effect=observe,
                wraps=_observe,
            ),
            self.assertLogs("control_plane.production_delivery_monitor", level="ERROR"),
        ):
            self.monitor()
        incidents = self.store.list_public_ingress_incident_records(status="open")
        self.assertTrue(any("deploy-" + self.head in incident.summary for incident in incidents))
        self.now += timedelta(seconds=1)
        self.monitor()
        self.assertEqual(len(self.store.list_public_ingress_incident_records(status="open")), 1)

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

    def test_source_read_failure_cannot_resolve_a_real_failure(self) -> None:
        self.reject()
        self.monitor()
        original = self.store.list_public_ingress_incident_records(status="open")[0]
        self.transport.get_json.side_effect = RuntimeError("source unavailable")
        with self.assertLogs("control_plane.production_delivery_monitor", level="ERROR"):
            self.monitor()
        incidents = self.store.list_public_ingress_incident_records(status="open")
        self.assertIn(original.incident_id, {incident.incident_id for incident in incidents})
        self.assertEqual(len(incidents), 2)
        self.transport.get_json.side_effect = self.read_source
        self.monitor()
        self.assertEqual(
            {
                incident.incident_id
                for incident in self.store.list_public_ingress_incident_records(status="open")
            },
            {original.incident_id},
        )

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
        self.now += timedelta(seconds=61)
        self.deploy(self.head, failed=True)
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

    def test_prelaunch_product_has_no_automatic_release_deadline(self) -> None:
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"production_use": "prelaunch"})
        )
        self.monitor()
        self.now += timedelta(hours=2)
        self.monitor()
        self.factory.assert_not_called()
        self.assertEqual(self.store.list_public_ingress_incident_records(status="open"), ())

    def test_new_failure_stage_updates_existing_attention_and_replays_do_not_spam(self) -> None:
        self.reject()
        original = self.store.list_public_ingress_incident_records(status="open")[0]
        self.now += timedelta(seconds=1)
        for _attempt in range(2):
            record_production_delivery_failure(
                self.store,
                self.profile,
                self.lane,
                candidate="image@" + self.head,
                reason="provider_mutation_failed",
                trace_id="launchplane_req_new_stage",
                observed_at=self.timestamp(),
            )
        incidents = self.store.list_public_ingress_incident_records(status="open")
        self.assertEqual({incident.incident_id for incident in incidents}, {original.incident_id})
        self.assertIn("launchplane_req_new_stage", incidents[0].summary)
        self.assertEqual(len(self.store.list_outbox_delivery_records()), 2)

    def test_explicit_lifecycle_change_retires_lag_attention(self) -> None:
        self.seed_legacy_lag()
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"production_use": "prelaunch"})
        )
        self.now += timedelta(seconds=1)
        self.monitor()
        self.assertEqual(self.store.list_public_ingress_incident_records(status="open"), ())
        incident = self.store.list_public_ingress_incident_records()[0]
        self.assertEqual(incident.resolution_reason, "monitoring_intent_changed")

    def test_unidentified_rollback_is_not_cleared_by_promoting_testing(self) -> None:
        testing = self.lane.model_copy(update={"instance": "testing"})
        self.profile = self.profile.model_copy(update={"lanes": (self.lane, testing)})
        self.store.write_product_profile_record(self.profile)
        reference = self.store.list_deployment_records()[0]
        testing_source = "d" * 40
        self.store.write_environment_inventory(
            EnvironmentInventory(
                context=testing.context,
                instance="testing",
                source_git_ref=testing_source,
                artifact_identity=ArtifactIdentityReference(artifact_id="image@" + testing_source),
                deploy=reference.deploy,
                updated_at=self.timestamp(),
                deployment_record_id="testing-deploy",
            )
        )
        _record_rejected_payload(
            self.store,
            product=self.profile.product,
            instance="prod",
            candidate="",
            authorized_scope=(self.profile.product, self.lane.context, "prod"),
            path="/v1/odoo-prod-rollbacks",
            reason="rollback_target_missing",
            trace_id="launchplane_req_rollback",
            request_key="rollback-request",
        )
        self.now += timedelta(seconds=1)
        self.deploy(testing_source)
        self.monitor()
        incident = self.store.list_public_ingress_incident_records(status="open")[0]
        self.assertIn("launchplane_req_rollback", incident.summary)

    def test_first_scan_does_not_page_for_a_promotion_older_than_running_production(self) -> None:
        reference = self.store.list_deployment_records()[0]
        old = (self.now - timedelta(hours=2)).isoformat()
        failed = PromotionRecord(
            record_id="historical-promotion",
            artifact_identity=ArtifactIdentityReference(artifact_id="image@" + self.head),
            context=self.lane.context,
            from_instance="testing",
            to_instance="prod",
            deploy=reference.deploy.model_copy(update={"status": "fail", "finished_at": old}),
        )
        self.store.write_promotion_record(failed)
        self.monitor()
        self.assertEqual(self.store.list_public_ingress_incident_records(status="open"), ())
        self.store.write_promotion_record(
            failed.model_copy(update={"record_id": "late-ingested-promotion"})
        )
        self.monitor()
        self.assertEqual(len(self.store.list_public_ingress_incident_records(status="open")), 1)
