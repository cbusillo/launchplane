from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import cast
from unittest.mock import patch

from control_plane.contracts.public_ingress_monitoring import PublicIngressNotificationDestination
from control_plane.outbox_worker import OutboxWorkerStepResult, run_outbox_worker_once
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.public_ingress_monitor import (
    HttpObservation,
    PublicIngressMonitorResult,
    PublicIngressNotificationDriverSet,
    run_public_ingress_monitor_once,
)
from tests.support.stores import _sqlite_database_url
from tests.test_public_ingress_monitor import _notification_policy, _profile


class DirectNotificationOutboxTests(unittest.TestCase):
    def test_recovery_survives_restart_and_transient_failure_without_new_event(self) -> None:
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
                self.assertEqual(
                    self._work(store, drivers, "2026-05-29T12:00:01Z").status, "delivered"
                )
                recovery = self._probe(store, 200, "2026-05-29T12:01:00Z")
                self.assertEqual(recovery.resolved_incident_count, 1)
                # Simulate restart after the event/outbox commit and before any send.
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
                healthy = self._probe(store, 200, "2026-05-29T12:02:00Z")
                self.assertEqual(healthy.incident_events, ())
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
                self.assertEqual(len(sent), 2)
                attempts = store.list_public_ingress_notification_attempt_records(event="resolved")
                self.assertEqual(len(attempts), 1)
                self.assertEqual(attempts[0].delivery_status, "delivered")
                self.assertEqual(store.list_public_ingress_incident_records()[0].status, "resolved")
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
            store.close()
            store = PostgresRecordStore(database_url=url)
            result = self._work(store, drivers, "2026-05-29T12:06:01Z")
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error_code, "delivery_outcome_unknown")
            self.assertEqual(len(sends), 1)
            self.assertEqual(self._work(store, drivers, "2026-05-29T12:07:01Z").status, "idle")
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
