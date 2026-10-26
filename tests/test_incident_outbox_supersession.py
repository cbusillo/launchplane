from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.storage.postgres import PostgresRecordStore, OutboxWithIdempotencyRequest
from control_plane.contracts.public_ingress_monitoring import PublicIngressNotificationDestination
from control_plane.workflows.public_ingress_monitor import (
    INCIDENT_RESOLVED_SUPERSESSION_ACTION,
    PublicIngressNotificationDriverSet,
    _reconcile_duplicate_incident,
    _public_ingress_notification_outbox_deliveries,
)
from tests.support.stores import _sqlite_database_url
from tests import test_public_ingress_direct_outbox as direct_outbox
from tests.test_public_ingress_monitor import _notification_policy, _profile


class IncidentOutboxSupersessionTests(unittest.TestCase):
    def test_duplicate_cleanup_does_not_send_endpoint_recovery(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=_sqlite_database_url(Path(directory) / "db"))
            self.addCleanup(store.close)
            store.ensure_schema()
            store.write_product_profile_record(_profile())
            destination = direct_outbox.DirectNotificationOutboxTests._destination("email")
            store.write_public_ingress_notification_policy_record(_notification_policy(destination))
            sent: list[object] = []
            drivers = PublicIngressNotificationDriverSet(
                email_sender=lambda _destination, message: sent.append(message),
                secret_resolver=lambda _name: "mock-credential",
            )
            direct_outbox.DirectNotificationOutboxTests._probe(store, 503, "2026-05-29T12:00:00Z")
            direct_outbox.DirectNotificationOutboxTests._work(
                store, drivers, "2026-05-29T12:00:01Z"
            )
            self.assertEqual(len(sent), 1)
            incident = store.list_public_ingress_incident_records()[0]
            resolved, event = _reconcile_duplicate_incident(
                incident=incident, reconciled_at="2026-05-29T12:01:00Z"
            )
            store.write_public_ingress_incident_record(resolved)
            observation = store.read_public_ingress_observation_record(
                resolved.resolved_observation_id
            )
            for delivery in _public_ingress_notification_outbox_deliveries(
                record_store=store,
                incident_records=(resolved,),
                incident_events=(event,),
                observation=observation,
            ):
                store.enqueue_outbox_delivery_with_idempotency(
                    OutboxWithIdempotencyRequest(delivery=delivery)
                )
            direct_outbox.DirectNotificationOutboxTests._work(
                store, drivers, "2026-05-29T12:02:00Z"
            )
            self.assertEqual(len(sent), 1)

    def test_resolved_incident_backlog_is_superseded_without_sending(self) -> None:
        for kind in ("email", "discord", "github_issue"):
            with self.subTest(kind=kind), TemporaryDirectory() as directory:
                store = PostgresRecordStore(
                    database_url=_sqlite_database_url(Path(directory) / "records.sqlite3")
                )
                self.addCleanup(store.close)
                store.ensure_schema()
                store.write_product_profile_record(_profile())
                destination = (
                    PublicIngressNotificationDestination(
                        destination_id="github-ops",
                        kind="github_issue",
                        github_repository="example/ops",
                        github_label="incident",
                    )
                    if kind == "github_issue"
                    else direct_outbox.DirectNotificationOutboxTests._destination(kind)
                )
                store.write_public_ingress_notification_policy_record(
                    _notification_policy(destination)
                )
                direct_outbox.DirectNotificationOutboxTests._probe(
                    store, 503, "2026-05-29T12:00:00Z"
                )
                direct_outbox.DirectNotificationOutboxTests._probe(
                    store, 200, "2026-05-29T12:01:00Z"
                )
                pending = store.list_outbox_delivery_records(states=("pending",))
                self.assertTrue(pending)
                drivers = PublicIngressNotificationDriverSet(
                    github_client=lambda *_args: self.fail("Resolved incident contacted GitHub"),
                    email_sender=lambda *_args: self.fail("Resolved incident sent email"),
                    discord_sender=lambda *_args: self.fail("Resolved incident sent Discord"),
                    secret_resolver=lambda _name: self.fail("Supersession resolved a secret"),
                )
                for delivery in pending:
                    result = direct_outbox.DirectNotificationOutboxTests._work(
                        store, drivers, "2026-05-29T12:02:00Z"
                    )
                    self.assertEqual(result.status, "delivered")
                    completed = store.read_outbox_delivery_record(result.delivery_id)
                    self.assertEqual(completed.action, INCIDENT_RESOLVED_SUPERSESSION_ACTION)
                    self.assertFalse(completed.provider_operation_key)
                    self.assertEqual(completed.attempt, delivery.attempt + 1)
                self.assertEqual(
                    direct_outbox.DirectNotificationOutboxTests._work(
                        store, drivers, "2026-05-29T12:03:00Z"
                    ).status,
                    "idle",
                )
                attempts = store.list_public_ingress_notification_attempt_records()
                self.assertTrue(attempts)
                self.assertTrue(all(attempt.delivery_status == "skipped" for attempt in attempts))
                self.assertTrue(
                    all(
                        attempt.action == INCIDENT_RESOLVED_SUPERSESSION_ACTION
                        for attempt in attempts
                    )
                )
