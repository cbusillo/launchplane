from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal
import unittest
from unittest.mock import patch

from control_plane.contracts.outbox_delivery import OutboxDeliveryRecord
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressFailureCode,
    PublicIngressNotificationDestination,
    PublicIngressObservationRecord,
    PublicIngressTargetObservation,
)
from control_plane.contracts.secret_record import SecretRecord
from control_plane.notifications import DiscordNotificationError, DiscordNotificationNotSentError
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows import public_ingress_monitor as monitor
from control_plane.workflows.public_ingress_monitor import (
    PublicIngressNotificationDriverSet,
    _store_monitor_observation,
)
from tests.support.stores import _sqlite_database_url
from tests.test_public_ingress_monitor import _notification_policy
from tests.test_public_ingress_direct_outbox import _is_red
from tests import test_public_ingress_direct_outbox as direct_outbox


class DiscordIncidentAlertTests(unittest.TestCase):
    def database_url(self) -> str:
        directory = self.enterContext(TemporaryDirectory())
        return _sqlite_database_url(Path(directory) / "records.sqlite3")

    def setUp(self) -> None:
        self.store = PostgresRecordStore(database_url=self.database_url())
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.destination = PublicIngressNotificationDestination(
            destination_id="discord", kind="discord", discord_webhook_secret="monitor-webhook"
        )
        self.policy = _notification_policy(self.destination).model_copy(
            update={"product": "demo", "context": "demo", "instance": ""}
        )
        self.store.write_public_ingress_notification_policy_record(self.policy)
        self.secret = SecretRecord(
            secret_id="monitor-webhook",
            scope="global",
            integration="discord",
            name="monitor",
            current_version_id="test-version",
            created_at="2026-10-25T12:00:00Z",
            updated_at="2026-10-25T12:00:00Z",
        )
        self.store.write_secret_record(self.secret)
        self.sent: list[dict[str, object]] = []
        self.drivers = PublicIngressNotificationDriverSet(
            secret_resolver=lambda _name: "https://discord.com/api/webhooks/test/test",
            discord_sender=lambda _url, payload: self.sent.append(payload),
        )

    def observe(
        self,
        instance: str,
        *,
        time: str = "12:00:00",
        status: Literal["pass", "fail"] = "fail",
        critical: bool = False,
        code: PublicIngressFailureCode | None = None,
    ) -> None:
        failure = (
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
            failure_code=failure,
            targets=(
                PublicIngressTargetObservation(
                    target="health_url",
                    url="https://example.test/health",
                    status=status,
                    failure_code=failure,
                    summary="Measured health",
                ),
            ),
            summary="Measured observation",
        )
        with patch.object(
            self.store,
            "_database_mutation_timestamp",
            return_value=record.observed_at,
        ):
            _store_monitor_observation(
                record_store=self.store,
                record=record,
                incident_eligible=True,
                expected_profile_sha256="",
                recovery_observation_threshold=1,
                notify=True,
                notification_drivers=self.drivers,
            )

    def drain(self, time: str) -> None:
        self.drain_at(f"2026-10-25T{time}Z")

    def drain_at(self, now: str) -> None:
        for _ in range(100):
            result = direct_outbox.DirectNotificationOutboxTests._work(
                self.store, self.drivers, now
            )
            if result.status == "idle":
                return
        self.fail("Outbox did not settle within its fixture inventory")

    def test_critical_failure_and_material_change_page_but_reminder_does_not(self) -> None:
        self.observe("prod", critical=True)
        self.drain("12:00:01")
        self.observe("prod", critical=True, time="12:01:00", code="dns_failure")
        self.drain("12:01:01")
        self.assertEqual(sum(_is_red(value) for value in self.sent), 2)
        self.observe("prod", critical=True, time="19:00:00", code="dns_failure")
        self.drain("19:05:00")
        self.assertEqual(sum(_is_red(value) for value in self.sent), 2)
        self.assertEqual(len(self.sent), 2)

    def test_preconnect_and_rate_limit_retry_critical_alert(self) -> None:
        self.observe("prod", critical=True)
        for error, time in (
            (DiscordNotificationNotSentError("not sent"), "12:00:01"),
            (DiscordNotificationError(429), "12:01:00"),
        ):
            self.drivers.discord_sender = lambda _url, _payload: (_ for _ in ()).throw(error)
            self.drain(time)
            self.assertEqual(self.store.list_outbox_delivery_records()[0].state, "pending")
        self.drivers.discord_sender = lambda _url, payload: self.sent.append(payload)
        self.drain("12:02:00")
        self.assertEqual(len(self.sent), 1)

    def test_critical_http_failures_do_not_replay_uncertain_sends(self) -> None:
        for index, (critical, status) in enumerate(((True, 503), (True, None))):
            with self.subTest(critical=critical, status=status):
                instance = f"lane-{critical}-{status}"
                self.observe(instance, critical=critical, time=f"{12 + index}:00:00")

                def fail(_url: str, payload: dict[str, object]) -> None:
                    self.sent.append(payload)
                    if status is None:
                        raise TimeoutError("response lost")
                    raise DiscordNotificationError(status)

                self.drivers.discord_sender = fail
                before = len(self.sent)
                self.drain(f"{12 + index}:05:00")
                self.drain(f"{12 + index}:10:00")
                self.assertEqual(len(self.sent), before + 1)
                row = max(self.store.list_outbox_delivery_records(), key=lambda v: v.updated_at)
                self.assertEqual(row.state, "failed")
                self.assertEqual(bool(row.provider_operation_key), status != 404)
                self.observe(instance, critical=critical, time=f"{12 + index}:11:00", status="pass")
                self.drain(f"{12 + index}:15:00")

    def test_delayed_critical_page_respects_silence_and_acknowledgement(self) -> None:
        for state in ("silenced", "acknowledged"):
            self.observe(state, critical=True)
            incident = next(
                v for v in self.store.list_public_ingress_incident_records() if v.instance == state
            )
            self.store.write_public_ingress_incident_record(
                incident.model_copy(
                    update={
                        "notification_state": state,
                        "silenced_until": "2026-10-25T20:00:00Z" if state == "silenced" else "",
                    }
                )
            )
        self.drain("12:00:01")
        self.assertEqual(self.sent, [])
        self.assertTrue(
            all(v.status == "open" for v in self.store.list_public_ingress_incident_records())
        )

    def test_critical_pre_send_resolver_failure_does_not_claim_unknown_effect(self) -> None:
        self.observe("prod", critical=True)

        def unavailable(_name: str) -> str:
            raise RuntimeError("unavailable")

        self.drivers.secret_resolver = unavailable
        self.drain("12:00:01")
        row = self.store.list_outbox_delivery_records()[0]
        self.assertEqual(row.state, "pending")
        self.assertFalse(row.provider_operation_key)
        self.assertEqual(self.sent, [])

    def test_critical_message_stays_bound_to_its_rendered_material_state(self) -> None:
        self.observe("prod", critical=True)
        original = monitor._deliver_direct_notification_outbox
        changed = False

        def transition_before_final_read(**kwargs: Any) -> OutboxDeliveryRecord:
            nonlocal changed
            if not changed:
                changed = True
                self.observe("prod", critical=True, time="12:01:00", code="dns_failure")
            return original(**kwargs)

        with patch.object(
            monitor, "_deliver_direct_notification_outbox", side_effect=transition_before_final_read
        ):
            self.drain("12:01:01")
        self.assertEqual(sum(_is_red(value) for value in self.sent), 1)
        attempts = self.store.list_public_ingress_notification_attempt_records()
        self.assertEqual(sum(value.delivery_status == "skipped" for value in attempts), 1)

    def test_changes_between_render_and_admission_never_send_stale_conditions(self) -> None:
        marker = self.store.mark_outbox_delivery_provider_started
        for index, change in enumerate(("resolved", "silenced", "material", "return")):
            critical = True
            with self.subTest(change=change, critical=critical):
                instance = f"race-{index}-{critical}"
                hour = 12 + 2 * index + int(critical)
                self.observe(instance, critical=critical, time=f"{hour}:00:00")
                before = len(self.sent)
                changed = False

                def admit(**kwargs: Any) -> object:
                    nonlocal changed
                    record = kwargs["record"]
                    assert isinstance(record, OutboxDeliveryRecord)
                    if not changed:
                        incident = next(
                            v
                            for v in self.store.list_public_ingress_incident_records()
                            if v.instance == instance
                        )
                        updates: dict[str, object] = {
                            "notification_state": "silenced",
                            "silenced_until": "2026-10-25T23:00:00Z",
                        }
                        if change == "resolved":
                            updates = {
                                "status": "resolved",
                                "resolved_at": f"2026-10-25T{hour}:04:00Z",
                                "resolved_observation_id": incident.latest_observation_id,
                            }
                        elif change in {"material", "return"}:
                            self.observe(
                                instance,
                                critical=critical,
                                time=f"{hour}:04:00",
                                code="dns_failure" if critical else "testing_delivery_failed",
                            )
                            if change == "return":
                                self.observe(instance, critical=critical, time=f"{hour}:04:01")
                            changed = True
                            return marker(**kwargs)
                        self.store.write_public_ingress_incident_record(
                            incident.model_copy(update=updates)
                        )
                        changed = True
                    return marker(**kwargs)

                with patch.object(
                    self.store, "mark_outbox_delivery_provider_started", side_effect=admit
                ):
                    self.drain(f"{hour}:05:00")
                if change not in {"material", "return"}:
                    self.assertEqual(len(self.sent), before)
                else:
                    self.assertLessEqual(
                        len(self.sent), before + int(critical or change == "return")
                    )
                self.drain(f"{hour}:10:00")
                if change in {"material", "return"}:
                    self.assertEqual(len(self.sent), before + 1)
                    self.observe(instance, critical=critical, time=f"{hour}:11:00", status="pass")
                    self.drain(f"{hour}:15:00")

    def test_warnings_and_reminders_remain_recorded_without_discord_work(self) -> None:
        self.observe("preview")
        self.observe("preview", time="12:01:00", code="testing_delivery_failed")
        self.observe("preview", time="19:00:00", code="testing_delivery_failed")
        self.drain("19:05:00")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.list_outbox_delivery_records(), ())
        incident = self.store.list_public_ingress_incident_records()[0]
        self.assertEqual(incident.status, "open")
        self.assertEqual(incident.severity, "warning")
        events = self.store.list_public_ingress_incident_event_records(
            incident_id=incident.incident_id
        )
        self.assertIn("reminder", {event.event for event in events})
        self.observe("preview", time="19:06:00", status="pass")
        self.drain("19:07:00")
        self.assertEqual(self.sent, [])

    def test_queued_warning_and_reminder_are_skipped(self) -> None:
        for warning in (False, True):
            with self.subTest(warning=warning):
                self.observe(str(warning), critical=True)
                queued = self.store.list_outbox_delivery_records(states=("pending",))[0]
                if warning:
                    incident = self.store.read_public_ingress_incident_record(queued.aggregate_id)
                    self.store.write_public_ingress_incident_record(
                        incident.model_copy(
                            update={
                                "schema_version": 1,
                                "severity": "warning",
                                "material_fingerprint": None,
                                "material_fingerprint_sha256": "",
                            }
                        )
                    )
                else:
                    attempt_payload = queued.payload["attempt"]
                    assert isinstance(attempt_payload, dict)
                    attempt = dict(attempt_payload)
                    attempt["event"] = "reminder"
                    self.store.write_outbox_delivery_record(
                        queued.model_copy(
                            update={"payload": {**queued.payload, "attempt": attempt}}
                        )
                    )
                self.drain("12:01:00")
                self.assertEqual(self.sent, [])
                completed = self.store.read_outbox_delivery_record(queued.delivery_id)
                self.assertEqual(completed.state, "delivered")
                self.assertFalse(completed.provider_operation_key)

    def test_retained_summary_never_sends_or_loses_unknown_effect(self) -> None:
        for started in (False, True):
            with self.subTest(started=started):
                record = OutboxDeliveryRecord(
                    delivery_id=f"old-summary-{started}",
                    kind="public_ingress_notification",
                    aggregate_type="public_ingress_discord_summary",
                    aggregate_id=self.policy.policy_id,
                    dedupe_key=f"old-summary-{started}",
                    created_at="2026-10-25T12:00:00Z",
                    updated_at="2026-10-25T12:00:00Z",
                    next_attempt_at="2026-10-25T12:05:00Z",
                    provider_operation_key="uncertain-send" if started else "",
                    provider_id="discord" if started else "",
                    payload={
                        "policy_id": self.policy.policy_id,
                        "destination_id": "discord",
                        "send_window": 1,
                    },
                )
                self.store.write_outbox_delivery_record(record)
                self.drain("12:05:00")
                completed = self.store.read_outbox_delivery_record(record.delivery_id)
                self.assertEqual(completed.state, "failed" if started else "delivered")
                self.assertEqual(completed.provider_operation_key, record.provider_operation_key)
                self.assertEqual(self.sent, [])
