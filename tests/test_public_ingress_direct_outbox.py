from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import cast
from unittest.mock import patch

from control_plane.contracts.public_ingress_monitoring import PublicIngressNotificationDestination
from control_plane.contracts.secret_record import SecretRecord
from control_plane.discord_incident_summary import SUMMARY_AGGREGATE_TYPE
from control_plane.notifications import DiscordNotificationError, DISCORD_CRITICAL_ALERT_COLOR
from control_plane.outbox_worker import (
    OutboxWorkerStepResult,
    run_outbox_worker_once,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.public_ingress_monitor import (
    INCIDENT_RESOLVED_SUPERSESSION_ACTION,
    HttpObservation,
    PublicIngressMonitorResult,
    PublicIngressNotificationDriverSet,
    run_public_ingress_monitor_once,
)
from tests.support.stores import _sqlite_database_url
from tests.test_public_ingress_monitor import _notification_policy, _profile


def _is_red(payload: dict[str, object]) -> bool:
    embeds = payload.get("embeds", [])
    return isinstance(embeds, list) and any(
        isinstance(embed, dict) and embed.get("color") == DISCORD_CRITICAL_ALERT_COLOR
        for embed in embeds
    )


class DirectNotificationOutboxTests(unittest.TestCase):
    def test_recovery_waits_for_inflight_failure_without_consuming_its_budget(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=_sqlite_database_url(Path(directory) / "db"))
            self.addCleanup(store.close)
            store.ensure_schema()
            store.write_product_profile_record(_profile())
            store.write_public_ingress_notification_policy_record(
                _notification_policy(self._destination("email"))
            )
            sent: list[object] = []

            def send(_destination: object, message: object) -> None:
                if not sent:
                    self._probe(store, 200, "2026-05-29T12:00:02Z")
                    result = self._work(store, drivers, "2026-05-29T12:00:03Z")
                    self.assertEqual(result.status, "idle")
                    pending = store.list_outbox_delivery_records(states=("pending",))
                    self.assertTrue(pending)
                    self.assertTrue(all(row.attempt == 0 for row in pending))
                sent.append(message)

            drivers = PublicIngressNotificationDriverSet(
                email_sender=send, secret_resolver=lambda _name: "mock-credential"
            )
            self._probe(store, 503, "2026-05-29T12:00:00Z")
            self._work(store, drivers, "2026-05-29T12:00:01Z")
            self._work(store, drivers, "2026-05-29T12:00:04Z")
            self.assertEqual(len(sent), 2)

    def test_startup_keeps_critical_alert_and_deduplicates_unchanged_reminder_summary(self) -> None:
        store = self._backlog_store()
        self._probe(store, 503, "2026-05-29T12:00:00Z")
        self._probe(store, 503, "2026-05-29T18:01:00Z")
        self._probe(store, 503, "2026-05-30T00:02:00Z")
        self._probe(store, 503, "2026-05-30T00:03:00Z")
        queued = store.list_outbox_delivery_records(states=("pending",))
        self.assertGreater(len(queued), 1)
        store.write_outbox_delivery_record(
            queued[0].model_copy(
                update={
                    "state": "running",
                    "lease_owner": "lost-worker",
                    "lease_expires_at": "2026-05-29T12:05:00Z",
                    "attempt": 1,
                }
            )
        )
        incident = store.list_public_ingress_incident_records()[0]
        sent: list[dict[str, object]] = []
        drivers = self._capturing_discord(sent)
        self._work(store, drivers, "2026-05-30T00:04:00Z")
        database_url = store.database_url
        store.close()
        store = PostgresRecordStore(database_url=database_url)
        self.addCleanup(store.close)

        self._drain_loop(store, drivers, "2026-05-30T00:10:00Z")

        self.assertEqual(len(sent), 2)
        self.assertIn(incident.incident_id, str(sent[0]))
        self.assertTrue(_is_red(sent[0]))
        attempts = store.list_public_ingress_notification_attempt_records()
        self.assertEqual(sum(attempt.delivery_status == "delivered" for attempt in attempts), 1)
        self.assertFalse(_is_red(sent[1]))
        self._probe(store, 503, "2026-05-30T06:03:00Z")
        self._probe(store, 503, "2026-05-30T12:04:00Z")
        new_deliveries = store.list_outbox_delivery_records(states=("pending",))
        self._drain_loop(store, drivers, "2026-05-30T12:05:00Z")
        self.assertTrue(new_deliveries)
        self.assertEqual(len(sent), 2)

    def test_startup_keeps_each_incident_and_destination_separate(self) -> None:
        store = self._backlog_store()
        profile = _profile()
        second = profile.model_copy(
            update={
                "product": "other-site",
                "lanes": tuple(
                    lane.model_copy(update={"context": "other-site"}) for lane in profile.lanes
                ),
            }
        )
        store.write_product_profile_record(second)
        destinations = (
            self._destination("discord"),
            self._destination("discord").model_copy(update={"destination_id": "second-channel"}),
        )
        store.write_public_ingress_notification_policy_record(_notification_policy(*destinations))
        store.write_public_ingress_notification_policy_record(
            _notification_policy(*destinations).model_copy(
                update={
                    "policy_id": "other-policy",
                    "product": second.product,
                    "context": "other-site",
                }
            )
        )
        self._probe(store, 503, "2026-05-29T12:00:00Z")
        self._probe(store, 503, "2026-05-29T18:01:00Z")
        sent: list[dict[str, object]] = []

        self._drain_loop(store, self._capturing_discord(sent), "2026-05-29T18:10:00Z")

        incidents = store.list_public_ingress_incident_records()
        urgent = [payload for payload in sent if _is_red(payload)]
        quiet = [payload for payload in sent if not _is_red(payload)]
        self.assertEqual(len(urgent), len(incidents) * len(destinations))
        self.assertEqual(len(quiet), len(incidents) * len(destinations))
        for incident in incidents:
            self.assertEqual(
                sum(incident.incident_id in str(payload) for payload in urgent), len(destinations)
            )

    def test_empty_activation_does_not_collapse_future_alerts_after_restart(self) -> None:
        store = self._backlog_store()
        sent: list[dict[str, object]] = []
        drivers = self._capturing_discord(sent)
        self.assertEqual(self._work(store, drivers, "2026-05-29T11:59:00Z").status, "idle")
        self._probe(store, 503, "2026-05-29T12:00:00Z")
        self._probe(store, 503, "2026-05-29T18:01:00Z")
        queued = store.list_outbox_delivery_records(states=("pending",))

        self._drain_loop(store, drivers, "2026-05-29T18:10:00Z")

        self.assertEqual(len(sent), len(queued))
        self.assertEqual(sum(_is_red(payload) for payload in sent), 1)
        self.assertEqual(sum(not _is_red(payload) for payload in sent), 1)

    def test_startup_never_sends_resolved_incident_backlog(self) -> None:
        store = self._backlog_store()
        self._probe(store, 503, "2026-05-29T12:00:00Z")
        self._probe(store, 503, "2026-05-29T18:01:00Z")
        self._probe(store, 200, "2026-05-29T18:02:00Z")
        queued = store.list_outbox_delivery_records(states=("pending",))
        sent: list[dict[str, object]] = []

        self._drain_loop(store, self._capturing_discord(sent), "2026-05-29T18:10:00Z")

        self.assertFalse(sent)
        self.assertTrue(queued)
        for delivery in queued:
            completed = store.read_outbox_delivery_record(delivery.delivery_id)
            self.assertEqual(completed.state, "delivered")
            self.assertFalse(completed.provider_operation_key)
        attempts = store.list_public_ingress_notification_attempt_records()
        self.assertEqual(
            len(attempts),
            sum(delivery.aggregate_type != SUMMARY_AGGREGATE_TYPE for delivery in queued),
        )
        self.assertTrue(all(attempt.delivery_status == "skipped" for attempt in attempts))

    def _backlog_store(self) -> PostgresRecordStore:
        directory = self.enterContext(TemporaryDirectory())
        store = PostgresRecordStore(
            database_url=_sqlite_database_url(Path(directory) / "records.sqlite3")
        )
        self.addCleanup(store.close)
        store.ensure_schema()
        store.write_product_profile_record(_profile())
        store.write_public_ingress_notification_policy_record(
            _notification_policy(self._destination("discord"))
        )
        store.write_secret_record(
            SecretRecord(
                secret_id="discord-webhook",
                scope="global",
                integration="discord",
                name="monitor",
                current_version_id="test-version",
                created_at="2026-05-29T12:00:00Z",
                updated_at="2026-05-29T12:00:00Z",
            )
        )
        return store

    @staticmethod
    def _capturing_discord(sent: list[dict[str, object]]) -> PublicIngressNotificationDriverSet:
        return PublicIngressNotificationDriverSet(
            discord_sender=lambda _url, payload: sent.append(payload),
            secret_resolver=lambda _name: "https://discord.com/api/webhooks/test/webhook",
        )

    @staticmethod
    def _drain_loop(
        store: PostgresRecordStore, drivers: PublicIngressNotificationDriverSet, now: str
    ) -> None:
        for _ in range(2 * len(store.list_outbox_delivery_records(states=("pending",))) + 1):
            result = DirectNotificationOutboxTests._work(store, drivers, now)
            if result.status == "idle":
                return
        raise AssertionError("Fixture outbox did not settle")

    def test_open_incident_survives_restart_and_transient_failure_without_duplicate_send(
        self,
    ) -> None:
        for kind in ("email", "discord"):
            with self.subTest(kind=kind), TemporaryDirectory() as directory:
                url = _sqlite_database_url(Path(directory) / "records.sqlite3")
                store = PostgresRecordStore(database_url=url)
                store.ensure_schema()
                store.write_product_profile_record(_profile())
                destination = self._destination(kind)
                store.write_public_ingress_notification_policy_record(
                    _notification_policy(destination)
                )
                sent: list[str] = []
                fail = False

                def send(body: str) -> None:
                    if fail:
                        if kind == "discord":
                            raise DiscordNotificationError(429)
                        raise TimeoutError("temporary destination failure with private detail")
                    sent.append(body)

                drivers = PublicIngressNotificationDriverSet(
                    email_sender=lambda _destination, message: send(message.get_content()),
                    discord_sender=lambda _url, payload: send(str(payload)),
                    secret_resolver=lambda name: (
                        "https://discord.com/api/webhooks/test/webhook"
                        if name == "discord-webhook"
                        else "mock-credential"
                    ),
                )
                self._probe(store, 503, "2026-05-29T12:00:00Z")
                store.close()
                store = PostgresRecordStore(database_url=url)
                queued = store.list_outbox_delivery_records(states=("pending",))
                self.assertEqual(len(queued), 1)
                delivery_id = queued[0].delivery_id
                event_id = cast(dict[str, object], queued[0].payload["attempt"])[
                    "incident_event_id"
                ]
                self.assertNotIn("mock-credential", str(queued[0].payload))
                for field, reference in destination.model_dump().items():
                    if "secret" in field and reference:
                        self.assertNotIn(reference, str(queued[0].payload))
                fail = True
                self.assertEqual(
                    self._work(store, drivers, "2026-05-29T12:01:01Z").status, "pending"
                )
                failed = store.read_outbox_delivery_record(delivery_id)
                self.assertEqual(failed.error_code, "provider_error")
                self.assertNotIn("private detail", str(failed.payload))
                unchanged = self._probe(store, 503, "2026-05-29T12:02:00Z")
                self.assertEqual(unchanged.incident_events, ())
                fail = False
                self.assertEqual(
                    self._work(store, drivers, "2026-05-29T12:02:01Z").status, "delivered"
                )
                self.assertEqual(self._work(store, drivers, "2026-05-29T12:03:01Z").status, "idle")
                delivered = store.read_outbox_delivery_record(delivery_id)
                self.assertEqual(
                    cast(dict[str, object], delivered.payload["attempt"])["incident_event_id"],
                    event_id,
                )
                self.assertEqual(len(sent), 1)
                attempts = store.list_public_ingress_notification_attempt_records(event="opened")
                self.assertEqual(len(attempts), 1)
                self.assertEqual(attempts[0].delivery_status, "delivered")
                self.assertEqual(store.list_public_ingress_incident_records()[0].status, "open")
                incident = store.list_public_ingress_incident_records()[0]
                store.write_public_ingress_incident_record(
                    incident.model_copy(update={"notification_state": "acknowledged"})
                )
                self._probe(store, 200, "2026-05-29T12:04:00Z")
                self.assertEqual(
                    self._work(store, drivers, "2026-05-29T12:04:01Z").status, "delivered"
                )
                self.assertEqual(len(sent), 2)
                resolved = store.list_public_ingress_notification_attempt_records(event="resolved")
                self.assertEqual(len(resolved), 1)
                self.assertEqual(resolved[0].delivery_status, "delivered")
                self.assertEqual(self._work(store, drivers, "2026-05-29T12:05:01Z").status, "idle")
                store.close()

    def test_interrupted_send_is_explicitly_uncertain_and_not_resent(self) -> None:
        with TemporaryDirectory() as directory:
            url = _sqlite_database_url(Path(directory) / "records.sqlite3")
            store = PostgresRecordStore(database_url=url)
            store.ensure_schema()
            store.write_product_profile_record(_profile())
            store.write_public_ingress_notification_policy_record(
                _notification_policy(self._destination("discord"))
            )
            self._probe(store, 503, "2026-05-29T12:00:00Z")
            sends: list[object] = []

            def interrupted(_url: str, payload: dict[str, object]) -> None:
                sends.append(payload)
                raise KeyboardInterrupt()

            drivers = PublicIngressNotificationDriverSet(
                discord_sender=interrupted,
                secret_resolver=lambda _name: "https://discord.com/api/webhooks/test/webhook",
            )
            with self.assertRaises(KeyboardInterrupt):
                self._work(store, drivers, "2026-05-29T12:00:01Z")
            self._probe(store, 200, "2026-05-29T12:01:00Z")
            store.close()
            store = PostgresRecordStore(database_url=url)
            result = self._work(store, drivers, "2026-05-29T12:06:01Z")
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error_code, "delivery_outcome_unknown")
            self.assertEqual(len(sends), 1)
            superseded = self._work(store, drivers, "2026-05-29T12:07:01Z")
            self.assertEqual(
                store.read_outbox_delivery_record(superseded.delivery_id).action,
                INCIDENT_RESOLVED_SUPERSESSION_ACTION,
            )
            self.assertEqual(len(sends), 1)
            self.assertEqual(self._work(store, drivers, "2026-05-29T12:08:01Z").status, "idle")
            store.close()

    def test_retry_budget_exhaustion_is_visible(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "records.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(_profile())
            store.write_public_ingress_notification_policy_record(
                _notification_policy(self._destination("email"))
            )
            self._probe(store, 503, "2026-05-29T12:00:00Z")
            queued = store.list_outbox_delivery_records(states=("pending",))[0]
            drivers = PublicIngressNotificationDriverSet(secret_resolver=lambda _name: "")
            for minute in range(queued.max_attempts):
                result = self._work(store, drivers, f"2026-05-29T12:{minute * 6:02d}:01Z")
                self.assertEqual(result.status, "pending")
            self._work(store, drivers, "2026-05-29T13:00:00Z")
            exhausted = store.read_outbox_delivery_record(queued.delivery_id)
            self.assertEqual(exhausted.state, "failed")
            self.assertEqual(exhausted.error_code, "max_attempts_exhausted")
            store.close()

    @staticmethod
    def _destination(kind: str) -> PublicIngressNotificationDestination:
        if kind == "email":
            return PublicIngressNotificationDestination(
                destination_id="email-ops",
                kind="email",
                email_to=("ops@example.test",),
                email_from="launchplane@example.test",
                smtp_host="smtp.example.test",
                smtp_username_secret="smtp-username",
                smtp_password_secret="smtp-password",
            )
        return PublicIngressNotificationDestination(
            destination_id="discord-ops",
            kind="discord",
            discord_webhook_secret="discord-webhook",
        )

    @staticmethod
    def _probe(
        store: PostgresRecordStore, status: int, checked_at: str
    ) -> PublicIngressMonitorResult:
        with patch.object(store, "_database_mutation_timestamp", return_value=checked_at):
            return run_public_ingress_monitor_once(
                record_store=store,
                checked_at=checked_at,
                http_get=lambda _url, _timeout: HttpObservation(
                    status_code=status,
                    final_url="https://example.test",
                    redirect_count=0,
                ),
            )

    @staticmethod
    def _work(
        store: PostgresRecordStore, drivers: PublicIngressNotificationDriverSet, now: str
    ) -> OutboxWorkerStepResult:
        with patch.object(store, "_database_mutation_timestamp", return_value=now):
            return run_outbox_worker_once(
                record_store=store,
                control_plane_root=Path("."),
                lease_owner="worker",
                notification_drivers=drivers,
            )
