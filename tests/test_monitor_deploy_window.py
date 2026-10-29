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
    HealthcheckEvidence,
    RecordFailure,
)
from control_plane.contracts.ship_request import ShipRequest
from control_plane.workflows.ship import build_deployment_record
from control_plane.health_monitor_scheduler import HealthMonitorScheduler
from control_plane.outbound_http import PublicHttpDestinationError
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.public_ingress_monitor import (
    HttpObservation,
    DEPLOY_INCIDENT_DEFERRAL,
    run_public_ingress_monitor_once,
)
from tests.support.stores import _sqlite_database_url
from tests.test_public_ingress_monitor import _identity, _notification_policy, _profile
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressNotificationDestination,
    PublicIngressTargetObservation,
)


class MonitorDeployWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=_sqlite_database_url(Path(directory.name) / "records.sqlite3")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.store.write_product_profile_record(_profile())
        self.store.write_public_ingress_notification_policy_record(
            _notification_policy(
                PublicIngressNotificationDestination(
                    destination_id="discord",
                    kind="discord",
                    discord_webhook_secret="TEST_WEBHOOK",
                )
            )
        )
        self.started = datetime(2026, 10, 28, 13, 43, tzinfo=timezone.utc)
        self.deployment = DeploymentRecord(
            record_id="deploy-1",
            context="example-site",
            instance="prod",
            source_git_ref="abc123",
            runtime_identity=_identity(),
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

    def test_own_pending_deploy_retains_failed_probe_without_incident_or_alert(self) -> None:
        old = self.deployment.model_copy(
            update={
                "record_id": "old-deploy",
                "deploy": self.deployment.deploy.model_copy(
                    update={
                        "status": "pass",
                        "started_at": (self.started - timedelta(hours=1)).isoformat(),
                        "finished_at": (self.started - timedelta(minutes=59)).isoformat(),
                    }
                ),
            }
        )
        self.store.write_deployment_record(old)
        self.store.write_deployment_record(self.deployment)
        for second in (24, 75):
            result = run_public_ingress_monitor_once(
                record_store=self.store,
                checked_at=(self.started + timedelta(seconds=second)).isoformat(),
                http_get=lambda url, timeout: HttpObservation(503, url, 0),
            )
            self.assertEqual(result.fail_count, 1)
            self.assertEqual(result.incidents, ())
            observation = self.store.read_public_ingress_observation_record(
                result.records[0].record_id
            )
            self.assertEqual(observation.status, "fail")
            self.assertEqual(observation.failure_code, "http_error")
        self.assertEqual(self.store.list_outbox_delivery_records(), ())

    def test_failed_completed_expired_foreign_and_future_deploys_do_not_hide_failure(self) -> None:
        cases: tuple[tuple[str, dict[str, str], dict[str, object], timedelta], ...] = (
            ("failed", {"status": "fail"}, {}, timedelta(seconds=24)),
            (
                "recorded failure",
                {},
                {"failure": RecordFailure(code="failed", description="Deployment failed.")},
                timedelta(seconds=24),
            ),
            ("expired", {}, {}, DEPLOY_INCIDENT_DEFERRAL),
            ("foreign lane", {}, {"instance": "testing"}, timedelta(seconds=24)),
            (
                "foreign product",
                {},
                {"runtime_identity": _identity().model_copy(update={"product": "other"})},
                timedelta(seconds=24),
            ),
            ("future", {}, {}, -timedelta(seconds=1)),
        )
        for name, deploy_changes, changes, offset in cases:
            with self.subTest(name=name), TemporaryDirectory() as directory:
                store = PostgresRecordStore(
                    database_url=_sqlite_database_url(Path(directory) / "db")
                )
                self.addCleanup(store.close)
                store.ensure_schema()
                store.write_product_profile_record(_profile())
                store.write_deployment_record(
                    self.deployment.model_copy(
                        update={
                            **changes,
                            "deploy": self.deployment.deploy.model_copy(update=deploy_changes),
                        }
                    )
                )
                result = run_public_ingress_monitor_once(
                    record_store=store,
                    checked_at=(self.started + offset).isoformat(),
                    http_get=lambda url, timeout: HttpObservation(503, url, 0),
                    notify=False,
                )
                self.assertEqual(result.open_incident_count, 1)

    def test_provider_completion_does_not_prove_replacement_readiness(self) -> None:
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
                http_get=refused,
            )
            self.assertEqual(result.fail_count, 1)
            self.assertEqual(result.incidents, ())
        self.assertEqual(self.store.list_outbox_delivery_records(), ())
        expired = run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=(self.started + DEPLOY_INCIDENT_DEFERRAL).isoformat(),
            http_get=refused,
        )
        self.assertEqual(expired.open_incident_count, 1)
        self.assertTrue(self.store.list_outbox_delivery_records())

    def test_ready_replacement_failure_alerts_inside_the_deploy_window(self) -> None:
        self.store.write_deployment_record(self.completed)
        replacing = run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=(self.started + timedelta(seconds=7)).isoformat(),
            http_get=lambda url, timeout: HttpObservation(503, url, 0),
        )
        self.assertEqual(replacing.incidents, ())
        ready = run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=(self.started + timedelta(seconds=10)).isoformat(),
            http_get=lambda url, timeout: HttpObservation(
                200, url, 0, payload={"runtime_identity": _identity().model_dump(mode="json")}
            ),
        )
        self.assertEqual(ready.fail_count, 0)
        failed = run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=(self.started + timedelta(seconds=24)).isoformat(),
            http_get=lambda url, timeout: HttpObservation(503, url, 0),
        )
        self.assertEqual(failed.open_incident_count, 1)
        self.assertTrue(self.store.list_outbox_delivery_records())

    def test_old_or_missing_identity_does_not_end_replacement_deferral(self) -> None:
        self.store.write_deployment_record(self.completed)
        for second, payload in (
            (10, {}),
            (
                12,
                {
                    "runtime_identity": _identity()
                    .model_copy(update={"deployment_record_id": "old"})
                    .model_dump(mode="json")
                },
            ),
        ):
            result = run_public_ingress_monitor_once(
                record_store=self.store,
                checked_at=(self.started + timedelta(seconds=second)).isoformat(),
                http_get=lambda url, timeout: HttpObservation(200, url, 0, payload=payload),
            )
            self.assertEqual(result.incidents, ())
        failed = run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=(self.started + timedelta(seconds=24)).isoformat(),
            http_get=lambda url, timeout: HttpObservation(503, url, 0),
        )
        self.assertEqual(failed.incidents, ())

    def test_failed_release_health_cannot_defer_an_outage(self) -> None:
        self.store.write_deployment_record(
            self.completed.model_copy(
                update={
                    "destination_health": HealthcheckEvidence(status="fail"),
                }
            )
        )
        result = run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=(self.started + timedelta(seconds=24)).isoformat(),
            http_get=lambda url, timeout: HttpObservation(503, url, 0),
        )
        self.assertEqual(result.open_incident_count, 1)

    def test_release_verified_readiness_also_ends_deferral(self) -> None:
        self.store.write_deployment_record(
            self.completed.model_copy(
                update={
                    "destination_health": HealthcheckEvidence(
                        status="pass",
                        verified=True,
                        urls=("https://example.test/healthz",),
                        timeout_seconds=10,
                        runtime_identity_status="match",
                    ),
                }
            )
        )
        result = run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=(self.started + timedelta(seconds=24)).isoformat(),
            http_get=lambda url, timeout: HttpObservation(503, url, 0),
        )
        self.assertEqual(result.open_incident_count, 1)

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

    def test_deploy_failure_ends_deferral_and_enqueues_the_alert(self) -> None:
        self.store.write_deployment_record(self.deployment)
        run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=(self.started + timedelta(seconds=24)).isoformat(),
            http_get=lambda url, timeout: HttpObservation(503, url, 0),
        )
        self.store.write_deployment_record(
            self.deployment.model_copy(
                update={
                    "deploy": self.deployment.deploy.model_copy(update={"status": "fail"}),
                }
            )
        )
        result = run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=(self.started + timedelta(seconds=25)).isoformat(),
            http_get=lambda url, timeout: HttpObservation(503, url, 0),
        )
        self.assertEqual(result.open_incident_count, 1)
        deliveries = self.store.list_outbox_delivery_records(kind="public_ingress_notification")
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].state, "pending")

    def test_new_deploy_cannot_extend_deferral_over_an_existing_failed_probe(self) -> None:
        self.store.write_deployment_record(self.deployment)
        failed = run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=(self.started + timedelta(seconds=24)).isoformat(),
            http_get=lambda url, timeout: HttpObservation(503, url, 0),
        )
        original = failed.records[0]
        self.store.write_public_ingress_observation_record(
            original.model_copy(
                update={
                    "record_id": "intent-reconciliation",
                    "observed_at": (
                        self.started + timedelta(seconds=24, microseconds=1)
                    ).isoformat(),
                    "purpose": "reconciliation",
                    "status": "skipped",
                    "failure_code": "monitoring_intent_changed",
                    "targets": (
                        PublicIngressTargetObservation(
                            target="monitoring_intent",
                            url="monitoring-intent://prelaunch",
                            status="skipped",
                            failure_code="monitoring_intent_changed",
                            summary="Monitoring configuration changed.",
                        ),
                    ),
                }
            )
        )
        successor = self.deployment.model_copy(
            update={
                "record_id": "deploy-2",
                "runtime_identity": _identity().model_copy(
                    update={"deployment_record_id": "deploy-2"}
                ),
                "deploy": self.deployment.deploy.model_copy(
                    update={
                        "started_at": (self.started + timedelta(seconds=25)).isoformat(),
                    }
                ),
            }
        )
        self.store.write_deployment_record(successor)
        result = run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=(self.started + timedelta(seconds=26)).isoformat(),
            http_get=lambda url, timeout: HttpObservation(503, url, 0),
        )
        self.assertEqual(result.open_incident_count, 1)

    def test_dns_and_tls_failures_remain_alertable_during_deploy(self) -> None:
        self.store.write_deployment_record(self.deployment)
        for code in ("dns_failure", "tls_failure"):
            with self.subTest(code=code):

                def failed_get(_url: str, _timeout: int) -> HttpObservation:
                    if code == "dns_failure":
                        raise PublicHttpDestinationError("dns_failure", "test DNS failure")
                    raise ssl.SSLError("test TLS failure")

                result = run_public_ingress_monitor_once(
                    record_store=self.store,
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

    def test_ordinary_ship_pending_shape_requires_unique_product_scope(self) -> None:
        pending = build_deployment_record(
            request=ShipRequest(
                artifact_id="example-artifact",
                context="example-site",
                instance="prod",
                source_git_ref="abc123",
                target_name="example-site-prod",
                target_type="application",
                provider_id="dokploy",
                target_category="application",
                provider_target_type="application",
                deploy_mode="git",
            ),
            record_id="ordinary-ship",
            deployment_id="control-plane-dokploy",
            deployment_status="pending",
            started_at=self.started.isoformat(),
            finished_at="",
        )
        self.store.write_deployment_record(pending)
        at = self.started + timedelta(seconds=24)
        result = run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=at.isoformat(),
            http_get=lambda url, timeout: HttpObservation(503, url, 0),
        )
        self.assertEqual(result.incidents, ())
        profile = _profile()
        other = profile.model_copy(update={"product": "other-site"})
        self.store.write_product_profile_record(other)
        result = run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=(at + timedelta(seconds=1)).isoformat(),
            http_get=lambda url, timeout: HttpObservation(503, url, 0),
        )
        self.assertTrue(result.incidents)

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

    def test_strict_identity_mismatch_during_own_deploy_is_retained_without_alert(self) -> None:
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
        self.assertEqual(result.incidents, ())
