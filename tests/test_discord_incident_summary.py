from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
import unittest

from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressIncidentRecord,
    PublicIngressNotificationDestination,
    PublicIngressObservationRecord,
    PublicIngressTargetObservation,
    PublicIngressFailureCode,
)
from control_plane.contracts.outbox_delivery import OutboxDeliveryRecord
from control_plane.discord_incident_summary import build_discord_incident_summary_delivery
from control_plane.notifications import DiscordNotificationError
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.public_ingress_monitor import (
    PublicIngressNotificationDriverSet,
    _store_monitor_observation,
    deliver_public_ingress_notification_outbox_delivery,
)
from tests.support.stores import _sqlite_database_url
from tests.test_public_ingress_monitor import _notification_policy


class DiscordIncidentSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        self.store = PostgresRecordStore(
            database_url=_sqlite_database_url(Path(directory) / "records.sqlite3")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.destination = PublicIngressNotificationDestination(
            destination_id="discord", kind="discord", discord_webhook_secret="monitor-webhook"
        )
        self.policy = _notification_policy(self.destination).model_copy(
            update={"product": "demo", "context": "demo", "instance": ""}
        )
        self.store.write_public_ingress_notification_policy_record(self.policy)
        self.sent: list[dict[str, object]] = []
        self.markers: list[str] = []
        self.drivers = PublicIngressNotificationDriverSet(
            secret_resolver=lambda _name: "https://discord.com/api/webhooks/test/test",
            discord_sender=lambda _url, payload: self.sent.append(payload),
        )

    def observe(
        self,
        instance: str,
        *,
        status: Literal["pass", "fail"] = "fail",
        critical: bool = False,
        time: str = "12:00:00",
        code: PublicIngressFailureCode | None = None,
    ) -> None:
        failure_code = (
            (code or ("http_error" if critical else "deploy_fence_held"))
            if status == "fail"
            else None
        )
        record = PublicIngressObservationRecord(
            record_id=f"{instance}-{time}",
            product="demo",
            context="demo",
            instance=instance,
            check_kind="public_http" if critical else "provider",
            check_name="health" if critical else "launchplane-deploy-fence",
            observed_at=f"2026-10-25T{time}Z",
            status=status,
            failure_code=failure_code,
            targets=(
                PublicIngressTargetObservation(
                    target="health_url",
                    url="https://example.test/health",
                    status=status,
                    failure_code=failure_code,
                    summary="Measured health",
                ),
            ),
            summary="Measured failure" if status == "fail" else "Measured recovery",
        )
        _store_monitor_observation(
            record_store=self.store,
            record=record,
            incident_eligible=True,
            recovery_observation_threshold=1,
            expected_profile_sha256="",
            expected_private_endpoint_key="",
            expected_private_endpoint_sha256="",
            expected_route_binding_sha256="",
            expected_provider_hold_snapshot=None,
            notify=True,
            notification_drivers=self.drivers,
        )

    def deliver(self, record: OutboxDeliveryRecord) -> OutboxDeliveryRecord:
        return deliver_public_ingress_notification_outbox_delivery(
            record=record,
            record_store=self.store,
            drivers=self.drivers,
            mark_provider_started=lambda key, _provider: self.markers.append(key),
        )

    def test_warning_fleet_coalesces_and_repeated_conditions_do_not_repost(self) -> None:
        self.observe("preview-one")
        first = self.store.list_outbox_delivery_records()[0]
        self.observe("preview-two", time="12:01:00")
        self.deliver(first)
        summaries = self.store.list_outbox_delivery_records(
            aggregate_type="public_ingress_discord_summary"
        )
        current = summaries[-1]
        self.store.write_outbox_delivery_record(self.deliver(current))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("2", str(self.sent[0]))
        self.assertEqual(self.sent[0]["allowed_mentions"], {"parse": []})
        self.assertNotIn("embeds", self.sent[0])
        self.observe("preview-one", time="19:00:00")
        self.assertEqual(len(self.store.list_outbox_delivery_records()), len(summaries))
        self.assertEqual(
            self.store.read_outbox_delivery_record(current.delivery_id).state, "delivered"
        )
        self.assertTrue(
            all(
                incident.status == "open"
                for incident in self.store.list_public_ingress_incident_records()
            )
        )

    def test_cleared_warnings_do_not_send_or_claim_recovery_for_another_lane(self) -> None:
        self.observe("preview-one")
        queued = self.store.list_outbox_delivery_records()[0]
        self.observe("preview-one", status="pass", time="12:01:00")
        self.deliver(queued)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.markers, [])

    def test_new_critical_failure_pages_and_unchanged_reminder_is_quiet(self) -> None:
        self.observe("prod", critical=True)
        first = self.store.list_outbox_delivery_records()[0]
        self.deliver(first)
        self.assertIn("embeds", self.sent[0])
        self.observe("prod", critical=True, time="19:00:00")
        summaries = self.store.list_outbox_delivery_records(
            aggregate_type="public_ingress_discord_summary"
        )
        self.assertEqual(len(summaries), 1)
        self.deliver(summaries[0])
        self.assertNotIn("embeds", self.sent[1])
        self.assertEqual(self.store.list_public_ingress_incident_records()[0].severity, "critical")

    def test_summary_rechecks_policy_and_does_not_replay_unknown_send(self) -> None:
        self.observe("preview-one")
        queued = self.store.list_outbox_delivery_records()[0]
        started = queued.model_copy(update={"provider_operation_key": queued.delivery_id})
        result = self.deliver(started)
        self.assertEqual(getattr(result, "error_code"), "delivery_outcome_unknown")
        self.assertEqual(self.sent, [])
        self.store.write_public_ingress_notification_policy_record(
            self.policy.model_copy(update={"status": "disabled"})
        )
        self.deliver(queued)
        self.assertEqual(self.sent, [])

    def test_summary_rate_limit_can_retry_but_lost_response_cannot(self) -> None:
        self.observe("preview-one")
        queued = self.store.list_outbox_delivery_records()[0]
        for error in (DiscordNotificationError(429), OSError("response lost")):
            with self.subTest(error=type(error).__name__):
                self.drivers.discord_sender = lambda _url, _payload: (_ for _ in ()).throw(error)
                result = self.deliver(queued)
                if isinstance(error, DiscordNotificationError):
                    self.assertEqual(result.state, "pending")
                    self.assertEqual(result.provider_operation_key, "")
                else:
                    self.assertEqual(result.state, "failed")
                    self.assertTrue(result.provider_operation_key)
                    self.markers.clear()
                    self.deliver(result)
                    self.assertEqual(self.markers, [])

    def test_superseded_critical_material_state_does_not_send_old_red(self) -> None:
        self.observe("prod", critical=True)
        queued = self.store.list_outbox_delivery_records()[0]
        self.observe("prod", critical=True, time="12:01:00", code="dns_failure")
        result = self.deliver(queued)
        self.assertEqual(result.action, "skipped_material_state_changed")
        self.assertEqual(self.sent, [])
        self.deliver(self.store.list_outbox_delivery_records()[-1])
        self.assertIn("dns_failure", str(self.sent[0]))

    def test_critical_discord_response_loss_cannot_repeat_a_red_notice(self) -> None:
        self.observe("prod", critical=True)
        queued = self.store.list_outbox_delivery_records()[0]

        def lose_response(_url: str, payload: dict[str, object]) -> None:
            self.sent.append(payload)
            raise TimeoutError("response lost")

        self.drivers.discord_sender = lose_response
        failed = self.deliver(queued)
        self.assertEqual(failed.error_code, "delivery_outcome_unknown")
        self.assertEqual(failed.state, "failed")
        self.deliver(failed)
        self.assertEqual(len(self.sent), 1)

    def test_summary_identity_ignores_probe_times_but_changes_with_material_state(self) -> None:
        self.observe("preview-one")
        incident = self.store.list_public_ingress_incident_records()[0]

        def build(value: PublicIngressIncidentRecord) -> object:
            return build_discord_incident_summary_delivery(
                record_store=self.store,
                policy=self.policy,
                destination=self.destination,
                incidents=(value,),
                created_at="2026-10-25T12:00:00Z",
            )

        first = build(incident)
        self.assertEqual(
            first, build(incident.model_copy(update={"latest_observed_at": "2026-10-25T13:00:00Z"}))
        )
        self.assertNotEqual(
            first, build(incident.model_copy(update={"material_fingerprint_sha256": "a" * 64}))
        )
