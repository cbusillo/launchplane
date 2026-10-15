from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressIncidentRecord,
    PublicIngressNotificationDestination,
    PublicIngressNotificationPolicyRecord,
)
from control_plane.contracts.product_profile_record import ProductLaneHealthCheck
from control_plane.health_alert_drill import (
    DRILL_WINDOW_SECONDS,
    HealthAlertDrillScope,
    health_alert_drill,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.public_ingress_monitor import PublicIngressNotificationDriverSet
from tests.support.stores import _sqlite_database_url
from tests.test_public_ingress_monitor import _profile


class HealthAlertDrillTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=_sqlite_database_url(Path(directory.name) / "records.db")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.profile = _profile()
        self.store.write_product_profile_record(self.profile)
        self.scope = HealthAlertDrillScope(
            product=self.profile.product,
            context=self.profile.lanes[0].context,
            instance=self.profile.lanes[0].instance,
            check_name="public-ingress",
        )
        self.now = datetime.now(timezone.utc).timestamp()
        self.posts: list[tuple[str, dict[str, object]]] = []
        self.drivers = PublicIngressNotificationDriverSet(
            discord_sender=lambda url, payload: self.posts.append((url, payload)),
            secret_resolver=lambda _: "https://discord.com/api/webhooks/test/private-token",
        )
        self.policy = PublicIngressNotificationPolicyRecord(
            policy_id="private-policy",
            product=self.scope.product,
            context=self.scope.context,
            instance=self.scope.instance,
            check_name=self.scope.check_name,
            destinations=(
                PublicIngressNotificationDestination(
                    destination_id="private-destination",
                    kind="discord",
                    discord_webhook_secret="private-secret-id",
                ),
            ),
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )

    def write_policy(self) -> None:
        self.store.write_public_ingress_notification_policy_record(self.policy)

    def test_delivery_uses_matching_route_and_persists_only_sanitized_receipt(self) -> None:
        self.write_policy()
        self.store.write_public_ingress_incident_record(
            PublicIngressIncidentRecord(
                incident_id="real-incident",
                **self.scope.model_dump(),
                status="open",
                opened_at="2026-01-01T00:00:00Z",
                opened_observation_id="real-observation",
                latest_observation_id="real-observation",
                latest_observed_at="2026-01-01T00:00:00Z",
                failure_code="http_error",
                summary="Actual failure.",
            )
        )
        before = self.store.list_public_ingress_incident_records()
        result = health_alert_drill(
            self.store, scope=self.scope, now=self.now, send=True, drivers=self.drivers
        )
        assert result.receipt is not None
        self.assertEqual(result.routing, "enabled")
        self.assertEqual(result.receipt.status, "delivered")
        self.assertEqual(result.receipt.delivered_count, len(self.posts))
        self.assertTrue(self.posts)
        self.assertEqual(self.posts[0][0], "https://discord.com/api/webhooks/test/private-token")
        payload = self.posts[0][1]
        self.assertIn("DRILL", json.dumps(payload))
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        serialized = result.model_dump_json()
        for private_value in (
            "private-policy",
            "private-destination",
            "private-secret-id",
            "private-token",
        ):
            self.assertNotIn(private_value, serialized)
        readback = health_alert_drill(self.store, scope=self.scope, now=self.now)
        self.assertEqual(readback.receipt, result.receipt)
        self.assertEqual(self.store.read_product_profile_record(self.scope.product), self.profile)
        self.assertEqual(self.store.list_public_ingress_incident_records(), before)
        self.assertFalse(self.store.list_public_ingress_observation_records())
        self.assertFalse(self.store.list_public_ingress_notification_attempt_records())
        self.assertFalse(self.store.list_public_ingress_incident_event_records())
        self.assertFalse(self.store.list_public_ingress_incident_reminder_state_records())

    def test_dry_run_missing_disabled_and_failed_delivery(self) -> None:
        missing = health_alert_drill(self.store, scope=self.scope, now=self.now, send=True)
        self.assertEqual(missing.routing, "missing")
        self.assertIsNone(missing.receipt)
        self.policy = self.policy.model_copy(update={"status": "disabled"})
        self.write_policy()
        disabled = health_alert_drill(self.store, scope=self.scope, now=self.now, send=True)
        self.assertEqual(disabled.routing, "disabled")
        self.policy = self.policy.model_copy(update={"status": "enabled"})
        self.write_policy()
        dry_run = health_alert_drill(
            self.store, scope=self.scope, now=self.now, drivers=self.drivers
        )
        self.assertEqual(dry_run.routing, "enabled")
        self.assertIsNone(dry_run.receipt)
        self.assertFalse(dry_run.rate_limited)
        with patch.object(
            self.drivers, "discord_sender", side_effect=RuntimeError("private-token")
        ):
            failed = health_alert_drill(
                self.store, scope=self.scope, now=self.now, send=True, drivers=self.drivers
            )
        assert failed.receipt is not None
        self.assertEqual(failed.receipt.status, "failed")
        self.assertGreater(failed.receipt.failed_count, 0)
        self.assertNotIn("private-token", failed.model_dump_json())
        retried = health_alert_drill(
            self.store, scope=self.scope, now=self.now, send=True, drivers=self.drivers
        )
        self.assertTrue(retried.rate_limited)
        self.assertFalse(self.posts)

    def test_policy_check_match_and_lane_enablement_are_real(self) -> None:
        self.policy = self.policy.model_copy(update={"check_kind": "private_http"})
        self.write_policy()
        result = health_alert_drill(self.store, scope=self.scope, now=self.now, send=True)
        self.assertEqual(result.routing, "missing")
        self.policy = self.policy.model_copy(update={"check_kind": "", "check_name": "other"})
        self.write_policy()
        self.assertEqual(
            health_alert_drill(self.store, scope=self.scope, now=self.now).routing, "missing"
        )
        self.policy = self.policy.model_copy(update={"check_name": ""})
        self.write_policy()
        lane = self.profile.lanes[0]
        self.store.write_product_profile_record(
            self.profile.model_copy(
                update={
                    "lanes": (
                        lane.model_copy(
                            update={
                                "health_monitoring": lane.health_monitoring.model_copy(
                                    update={"monitoring_intent": "prelaunch"}
                                )
                            }
                        ),
                    )
                }
            )
        )
        self.assertEqual(
            health_alert_drill(self.store, scope=self.scope, now=self.now).routing, "disabled"
        )
        unknown = self.scope.model_copy(update={"check_name": "absent"})
        self.assertEqual(
            health_alert_drill(self.store, scope=unknown, now=self.now).routing, "unknown_check"
        )

    def test_private_check_uses_its_existing_managed_secret_scope_and_discord_transport(
        self,
    ) -> None:
        lane = self.profile.lanes[0]
        private = ProductLaneHealthCheck(
            name="private-runtime",
            kind="private_http",
            private_endpoint_key="existing-private-endpoint",
        )
        self.store.write_product_profile_record(
            self.profile.model_copy(
                update={
                    "lanes": (
                        lane.model_copy(
                            update={
                                "health_monitoring": lane.health_monitoring.model_copy(
                                    update={"checks": (*lane.health_monitoring.checks, private)}
                                )
                            }
                        ),
                    )
                }
            )
        )
        self.scope = self.scope.model_copy(update={"check_name": private.name})
        self.policy = self.policy.model_copy(
            update={"check_name": private.name, "check_kind": private.kind}
        )
        self.write_policy()
        with (
            patch(
                "control_plane.workflows.public_ingress_monitor.managed_notification_secret_value",
                return_value="https://discord.com/api/webhooks/test/private-token",
            ) as resolve,
            patch("control_plane.notifications.request_public_http") as transport,
        ):
            transport.return_value.status_code = 204
            result = health_alert_drill(self.store, scope=self.scope, now=self.now, send=True)
        assert result.receipt is not None
        self.assertEqual(result.receipt.status, "delivered")
        resolve.assert_called_once_with(
            self.store, "private-secret-id", self.scope.context, self.scope.instance
        )
        self.assertEqual(transport.call_args.kwargs["method"], "POST")
        body = json.loads(transport.call_args.kwargs["body"])
        self.assertIn("DRILL", body["embeds"][0]["title"])
        self.assertIn(private.name, body["embeds"][0]["description"])
        self.assertFalse(self.store.list_public_ingress_observation_records())
        public_scope = self.scope.model_copy(update={"check_name": "public-ingress"})
        other_check = health_alert_drill(
            self.store, scope=public_scope, now=self.now, send=True, drivers=self.drivers
        )
        self.assertTrue(other_check.rate_limited)
        self.assertIsNone(other_check.receipt)
        self.assertFalse(self.posts)

    def test_policy_change_between_destinations_stops_the_disabled_send(self) -> None:
        first = self.policy.destinations[0]
        second = first.model_copy(update={"destination_id": "second"})
        self.policy = self.policy.model_copy(update={"destinations": (first, second)})
        self.write_policy()

        def deliver(url: str, payload: dict[str, object]) -> None:
            self.posts.append((url, payload))
            self.store.write_public_ingress_notification_policy_record(
                self.policy.model_copy(
                    update={
                        "destinations": (first, second.model_copy(update={"status": "disabled"}))
                    }
                )
            )

        self.drivers.discord_sender = deliver
        result = health_alert_drill(
            self.store, scope=self.scope, now=self.now, send=True, drivers=self.drivers
        )
        assert result.receipt is not None
        self.assertEqual(result.receipt.status, "unavailable")
        self.assertEqual(result.receipt.unavailable_count, 1)
        self.assertEqual(len(self.posts), 1)

    def test_declared_but_ineffective_check_cannot_drill(self) -> None:
        lane = self.profile.lanes[0]
        private = ProductLaneHealthCheck(name="unconfigured-private", kind="private_http")
        self.store.write_product_profile_record(
            self.profile.model_copy(
                update={
                    "lanes": (
                        lane.model_copy(
                            update={
                                "health_monitoring": lane.health_monitoring.model_copy(
                                    update={"checks": (*lane.health_monitoring.checks, private)}
                                )
                            }
                        ),
                    )
                }
            )
        )
        self.policy = self.policy.model_copy(update={"check_name": ""})
        self.write_policy()
        result = health_alert_drill(
            self.store,
            scope=self.scope.model_copy(update={"check_name": private.name}),
            now=self.now,
            send=True,
            drivers=self.drivers,
        )
        self.assertEqual(result.routing, "disabled")
        self.assertIsNone(result.receipt)
        self.assertFalse(self.posts)

    def test_unresolvable_secret_is_unavailable_and_disabled_destination_sends_nothing(
        self,
    ) -> None:
        self.write_policy()
        driver = PublicIngressNotificationDriverSet(secret_resolver=lambda _: "")
        result = health_alert_drill(
            self.store, scope=self.scope, now=self.now, send=True, drivers=driver
        )
        assert result.receipt is not None
        self.assertEqual(result.receipt.status, "unavailable")
        self.policy = self.policy.model_copy(
            update={
                "destinations": tuple(
                    destination.model_copy(update={"status": "disabled"})
                    for destination in self.policy.destinations
                )
            }
        )
        self.write_policy()
        disabled = health_alert_drill(
            self.store,
            scope=self.scope,
            now=self.now + DRILL_WINDOW_SECONDS,
            send=True,
            drivers=self.drivers,
        )
        self.assertEqual(disabled.routing, "disabled")
        self.assertFalse(self.posts)

    def test_database_window_prevents_concurrent_and_restarted_resends(self) -> None:
        self.write_policy()
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(
                executor.map(
                    lambda _: health_alert_drill(
                        self.store, scope=self.scope, now=self.now, send=True, drivers=self.drivers
                    ),
                    range(4),
                )
            )
        self.assertTrue(all(result.rate_limited for result in results))
        self.assertEqual(len(self.posts), len(self.policy.destinations))
        # Independent store instances/processes share the persisted budget.
        other = PostgresRecordStore(database_url=self.store.database_url)
        self.addCleanup(other.close)
        health_alert_drill(other, scope=self.scope, now=self.now, send=True, drivers=self.drivers)
        self.assertEqual(len(self.posts), len(self.policy.destinations))
        later = self.now + DRILL_WINDOW_SECONDS
        health_alert_drill(other, scope=self.scope, now=later, send=True, drivers=self.drivers)
        self.assertEqual(len(self.posts), 2 * len(self.policy.destinations))
        historic = health_alert_drill(other, scope=self.scope, now=later, window=results[0].window)
        self.assertIsNotNone(historic.receipt)

    def test_ambiguous_send_is_never_repeated_and_read_failures_do_not_send(self) -> None:
        self.write_policy()
        with patch.object(
            self.store, "write_idempotency_record", side_effect=RuntimeError("storage lost")
        ):
            with self.assertRaises(RuntimeError):
                health_alert_drill(
                    self.store, scope=self.scope, now=self.now, send=True, drivers=self.drivers
                )
        readback = health_alert_drill(
            self.store, scope=self.scope, now=self.now, send=True, drivers=self.drivers
        )
        assert readback.receipt is not None
        self.assertEqual(readback.receipt.status, "unavailable")
        self.assertEqual(len(self.posts), len(self.policy.destinations))
        with patch.object(
            self.store,
            "list_public_ingress_notification_policy_records",
            side_effect=RuntimeError("private-token"),
        ):
            unavailable = health_alert_drill(
                self.store, scope=self.scope, now=self.now + DRILL_WINDOW_SECONDS, send=True
            )
        self.assertEqual(unavailable.routing, "unavailable")
        self.assertNotIn("private-token", unavailable.model_dump_json())


if __name__ == "__main__":
    unittest.main()
