from datetime import datetime, timedelta, timezone
from pathlib import Path
from itertools import count
from tempfile import TemporaryDirectory
import ssl
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.contracts.product_profile_record import (
    ProductLaneHealthCheck,
    ProductLaneHealthMonitoringPolicy,
    ProductLaneProfile,
)
from control_plane.contracts.promotion_record import (
    DeploymentEvidence,
)
from control_plane.health_monitor_scheduler import HealthMonitorScheduler
from control_plane.lane_health_history import read_lane_health_history, summarize_history_pages
from control_plane.outbound_http import PublicHttpDestinationError
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.public_ingress_monitor import (
    HttpObservation,
    run_public_ingress_monitor_once,
)
from tests.support.stores import _sqlite_database_url
from tests.test_public_ingress_monitor import (
    _identity,
    _lane_summary,
    _notification_policy,
    _profile,
)
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressNotificationDestination,
)


class MonitorDeployWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = self._new_store()
        self.started = datetime(2026, 10, 28, 13, 43, tzinfo=timezone.utc)
        self.identity = _identity()
        self.deployment = DeploymentRecord(
            record_id="deploy-1",
            context="example-site",
            instance="prod",
            source_git_ref="abc123",
            runtime_identity=self.identity,
            deploy=DeploymentEvidence(
                target_name="example-site-prod",
                target_type="application",
                deploy_mode="git",
                started_at=self.started.isoformat(),
            ),
        )
        self.completed = self.deployment.model_copy(
            update={
                "deploy": self.deployment.deploy.model_copy(
                    update={"status": "pass", "finished_at": self.started.isoformat()}
                )
            }
        )

    def _new_store(self) -> PostgresRecordStore:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = PostgresRecordStore(
            database_url=_sqlite_database_url(Path(directory.name) / "records.sqlite3")
        )
        self.addCleanup(store.close)
        store.ensure_schema()
        store.write_product_profile_record(_profile())
        store.write_public_ingress_notification_policy_record(
            _notification_policy(
                PublicIngressNotificationDestination(
                    destination_id="discord",
                    kind="discord",
                    discord_webhook_secret="TEST_WEBHOOK",
                )
            )
        )
        return store

    def test_deployment_failure_immediately_links_incident_and_counts_failed_time(self) -> None:
        self.store.write_deployment_record(self.deployment)
        incidents = []
        for second in (24, 75):
            result = run_public_ingress_monitor_once(
                record_store=self.store,
                checked_at=(self.started + timedelta(seconds=second)).isoformat(),
                http_get=lambda url, timeout: HttpObservation(503, url, 0),
            )
            self.assertEqual(result.fail_count, 1)
            self.assertEqual(result.open_incident_count, 1)
            incident = self.store.read_public_ingress_incident_record(
                result.incidents[0].incident_id
            )
            observation = self.store.read_public_ingress_observation_record(
                result.records[0].record_id
            )
            self.assertEqual(incident.latest_observation_id, observation.record_id)
            self.assertEqual(incident.opened_at, (self.started + timedelta(seconds=24)).isoformat())
            self.assertEqual(observation.incident_id, incident.incident_id)
            self.assertEqual(
                observation.expected_runtime_identity, self.deployment.runtime_identity
            )
            self.assertEqual(observation.status, "fail")
            incidents.append(incident.incident_id)
            self.assertEqual(
                len(self.store.list_outbox_delivery_records(kind="public_ingress_notification")), 1
            )
        self.assertEqual(incidents[0], incidents[1])
        recovered = run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=(self.started + timedelta(seconds=90)).isoformat(),
            http_get=lambda url, timeout: HttpObservation(
                200, url, 0, payload={"runtime_identity": _identity().model_dump(mode="json")}
            ),
        )
        self.assertEqual(recovered.resolved_incident_count, 1)
        self.assertEqual(recovered.incidents[0].incident_id, incidents[0])
        history = read_lane_health_history(
            self.store,
            product=self.identity.product,
            context=self.deployment.context,
            instance=self.deployment.instance,
            since=self.started + timedelta(seconds=24),
            until=self.started + timedelta(seconds=120),
        )
        checks = tuple({(record.check_kind, record.check_name) for record in history.observations})
        accounting = summarize_history_pages(
            (history,), required_checks=checks, identity_checks=checks
        )
        self.assertEqual(accounting.lane_health_fail_seconds, 90 - 24)
        self.assertEqual(accounting.lane_health_pass_seconds, 120 - 90)
        self.assertEqual(accounting.lane_health_unknown_seconds, 0)
        self.assertEqual(accounting.lane_health_pass_percent, 100 * (120 - 90) / (120 - 24))
        failed_history = [record for record in history.observations if record.status == "fail"]
        self.assertEqual(len(failed_history), 2)
        for record in failed_history:
            self.assertEqual(record.incident_id, incidents[0])
            self.assertIsNotNone(record.expected_runtime_identity)
            assert record.expected_runtime_identity is not None
            self.assertEqual(
                record.expected_runtime_identity.deployment_record_id, self.deployment.record_id
            )

    def test_failure_tags_current_deployment_while_inventory_still_expects_previous_runtime(
        self,
    ) -> None:
        previous = self.identity.model_copy(update={"deployment_record_id": "previous"})
        inventory = _lane_summary(previous).inventory
        assert inventory is not None
        self.store.write_environment_inventory(inventory)
        self.store.write_deployment_record(self.deployment)
        result = run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=(self.started + timedelta(seconds=24)).isoformat(),
            http_get=lambda url, timeout: HttpObservation(503, url, 0),
        )
        observation = self.store.read_public_ingress_observation_record(result.records[0].record_id)
        self.assertEqual(observation.expected_runtime_identity, previous)
        self.assertIn(self.deployment.record_id, observation.summary)
        incident = self.store.read_public_ingress_incident_record(observation.incident_id)
        self.assertIn(self.deployment.record_id, incident.summary)

    def test_missing_or_unorderable_deployment_evidence_keeps_failure_alertable(self) -> None:
        for started_at in (None, "unknown", (self.started + timedelta(minutes=1)).isoformat()):
            with self.subTest(started_at=started_at):
                store = self._new_store()
                if started_at is not None:
                    store.write_deployment_record(
                        self.deployment.model_copy(
                            update={
                                "deploy": self.deployment.deploy.model_copy(
                                    update={"started_at": started_at}
                                )
                            }
                        )
                    )
                result = run_public_ingress_monitor_once(
                    record_store=store,
                    checked_at=(self.started + timedelta(seconds=24)).isoformat(),
                    http_get=lambda url, timeout: HttpObservation(503, url, 0),
                )
                observation = store.read_public_ingress_observation_record(
                    result.records[0].record_id
                )
                self.assertEqual(result.open_incident_count, 1)
                self.assertTrue(observation.incident_id)
                self.assertNotIn(self.deployment.record_id, observation.summary)
                self.assertTrue(
                    store.list_outbox_delivery_records(kind="public_ingress_notification")
                )

    def test_new_deployment_during_probe_keeps_the_deployment_at_probe_time(self) -> None:
        self.store.write_deployment_record(self.deployment)
        newer = self.deployment.model_copy(
            update={
                "record_id": "next-deployment",
                "runtime_identity": self.identity.model_copy(
                    update={"deployment_record_id": "next-deployment"}
                ),
                "deploy": self.deployment.deploy.model_copy(
                    update={"started_at": (self.started + timedelta(seconds=25)).isoformat()}
                ),
            }
        )

        def fail_while_deploy_starts(url: str, _timeout: int) -> HttpObservation:
            self.store.write_deployment_record(newer)
            return HttpObservation(503, url, 0)

        result = run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=(self.started + timedelta(seconds=24)).isoformat(),
            http_get=fail_while_deploy_starts,
        )
        observation = self.store.read_public_ingress_observation_record(result.records[0].record_id)
        self.assertIn(self.deployment.record_id, observation.summary)
        self.assertNotIn(newer.record_id, observation.summary)

    def test_completed_deployment_connection_failure_opens_incident_immediately(self) -> None:
        self.store.write_product_profile_record(
            _profile(
                lane=ProductLaneProfile(
                    context="example-site",
                    instance="prod",
                    health_monitoring=ProductLaneHealthMonitoringPolicy(
                        monitoring_intent="private",
                        checks=(
                            ProductLaneHealthCheck(
                                name="runtime", kind="private_http", private_endpoint_key="runtime"
                            ),
                        ),
                    ),
                )
            )
        )
        self.store.write_private_health_endpoint_record(
            PrivateHealthEndpointRecord(
                endpoint_key="runtime",
                product="example-site",
                context="example-site",
                instance="prod",
                url="http://10.0.0.1:8787/health",
                updated_at=self.started.isoformat(),
            )
        )
        self.store.write_deployment_record(self.completed)

        def refused(_url: str, _timeout: int) -> HttpObservation:
            raise ConnectionRefusedError(111, "Connection refused")

        for second in (24, 75):
            result = run_public_ingress_monitor_once(
                record_store=self.store,
                checked_at=(self.started + timedelta(seconds=second)).isoformat(),
                private_http_get=refused,
            )
            self.assertEqual(result.fail_count, 1)
            self.assertEqual(result.open_incident_count, 1)
        self.assertTrue(self.store.list_outbox_delivery_records())

    def test_deploy_does_not_clear_or_quiet_an_existing_outage(self) -> None:
        failed = run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=(self.started - timedelta(seconds=1)).isoformat(),
            http_get=lambda url, timeout: HttpObservation(503, url, 0),
        )
        self.store.write_deployment_record(self.deployment)
        during = run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=(self.started + timedelta(seconds=24)).isoformat(),
            http_get=lambda url, timeout: HttpObservation(503, url, 0),
        )
        self.assertEqual(during.open_incident_count, 1)
        self.assertEqual(during.incidents[0].incident_id, failed.incidents[0].incident_id)

    def test_deploy_terminal_state_triggers_verification_without_inventory_change(self) -> None:
        now = self.started.timestamp()
        probe = Mock()
        scheduler = HealthMonitorScheduler(self.store, clock=lambda: now, run_monitor=probe)
        self.assertTrue(scheduler.run_once())
        self.assertFalse(scheduler.run_once())
        self.store.write_deployment_record(self.deployment)
        self.assertTrue(scheduler.run_once())
        self.store.write_deployment_record(
            self.deployment.model_copy(
                update={
                    "deploy": self.deployment.deploy.model_copy(update={"status": "fail"}),
                }
            )
        )
        self.assertTrue(scheduler.run_once())
        self.assertFalse(scheduler.run_once())
        self.assertEqual(probe.call_count, 3)

    def test_dns_and_tls_failures_remain_alertable_during_deploy(self) -> None:
        for code in ("dns_failure", "tls_failure"):
            with self.subTest(code=code):
                store = self._new_store()
                store.write_deployment_record(self.deployment)

                def failed_get(_url: str, _timeout: int) -> HttpObservation:
                    if code == "dns_failure":
                        raise PublicHttpDestinationError("dns_failure", "test DNS failure")
                    raise ssl.SSLError("test TLS failure")

                result = run_public_ingress_monitor_once(
                    record_store=store,
                    checked_at=(self.started + timedelta(seconds=24)).isoformat(),
                    http_get=failed_get,
                )
                self.assertEqual(result.open_incident_count, 1)

    def test_observation_uses_probe_time_after_discovery(self) -> None:
        ticks = count()
        with patch(
            "control_plane.workflows.public_ingress_monitor.utc_now_timestamp",
            side_effect=lambda: (self.started + timedelta(seconds=next(ticks))).isoformat(),
        ):
            result = run_public_ingress_monitor_once(
                record_store=self.store,
                http_get=lambda url, timeout: HttpObservation(200, url, 0),
            )
        self.assertGreater(result.records[0].observed_at, result.checked_at)

    def test_checkpoint_payload_does_not_reprobe_and_offset_time_selects_newest_attempt(
        self,
    ) -> None:
        now = self.started.timestamp()
        scheduler = HealthMonitorScheduler(self.store, clock=lambda: now, run_monitor=Mock())
        self.store.write_deployment_record(self.deployment)
        self.assertTrue(scheduler.run_once())
        checkpoint = self.deployment.model_copy(update={"runtime_source": {"checkpoint": "next"}})
        self.store.write_deployment_record(checkpoint)
        self.assertFalse(scheduler.run_once())
        newer = self.deployment.model_copy(
            update={
                "record_id": "newer",
                "deploy": self.deployment.deploy.model_copy(
                    update={
                        "status": "fail",
                        "started_at": "2026-10-28T09:44:00-04:00",
                    }
                ),
            }
        )
        self.store.write_deployment_record(newer)
        latest = self.store.read_latest_lane_deployment_record(
            context_name="example-site", instance_name="prod"
        )
        self.assertEqual(latest, newer)
        self.assertTrue(scheduler.run_once())

    def test_strict_identity_mismatch_during_deployment_opens_incident(self) -> None:
        profile = _profile()
        lane = profile.lanes[0]
        policy = lane.health_monitoring.model_copy(
            update={
                "checks": tuple(
                    check.model_copy(update={"require_runtime_identity": True})
                    for check in lane.health_monitoring.checks
                ),
            }
        )
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "lanes": (lane.model_copy(update={"health_monitoring": policy}),),
                }
            )
        )
        self.store.write_deployment_record(self.deployment)
        previous_identity = _identity().model_copy(update={"deployment_record_id": "old"})
        result = run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=(self.started + timedelta(seconds=24)).isoformat(),
            http_get=lambda url, timeout: HttpObservation(
                200, url, 0, payload={"runtime_identity": previous_identity.model_dump(mode="json")}
            ),
        )
        self.assertEqual(result.fail_count, 1)
        self.assertEqual(result.records[0].failure_code, "wrong_runtime_identity")
        self.assertEqual(result.open_incident_count, 1)
        self.assertTrue(self.store.list_outbox_delivery_records())
