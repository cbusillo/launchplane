from pathlib import Path
from tempfile import TemporaryDirectory
import signal
from typing import Any
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.outbox_worker import OutboxWorkerStepResult, run_outbox_worker_once
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.public_ingress_monitor import PublicIngressNotificationDriverSet
from tests import test_public_ingress_direct_outbox as direct_outbox
from tests.support.stores import _sqlite_database_url
from tests.test_public_ingress_monitor import _notification_policy, _profile


class OutboxWorkerShutdownTests(unittest.TestCase):
    def test_sigterm_finishes_the_inflight_delivery_and_stops_before_another_claim(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "records.sqlite3")
            )
            self.addCleanup(store.close)
            store.ensure_schema()
            store.write_product_profile_record(_profile())
            store.write_public_ingress_notification_policy_record(
                _notification_policy(
                    direct_outbox.DirectNotificationOutboxTests._destination("discord")
                )
            )
            direct_outbox.DirectNotificationOutboxTests._probe(store, 503, "2026-05-29T12:00:00Z")
            queued = store.list_outbox_delivery_records(states=("pending",))[0]
            sends: list[object] = []
            unhandled_signals: list[int] = []
            steps = 0

            def fallback(signum: int, _frame: object) -> None:
                unhandled_signals.append(signum)

            previous_sigterm = signal.signal(signal.SIGTERM, fallback)
            self.addCleanup(signal.signal, signal.SIGTERM, previous_sigterm)

            def send(_url: str, payload: dict[str, object]) -> None:
                sends.append(payload)
                signal.raise_signal(signal.SIGTERM)

            def step(**kwargs: Any) -> OutboxWorkerStepResult:
                nonlocal steps
                steps += 1
                if steps > 1:
                    raise KeyboardInterrupt()
                return run_outbox_worker_once(**kwargs)

            drivers = PublicIngressNotificationDriverSet(
                discord_sender=send,
                secret_resolver=lambda _name: "https://discord.com/api/webhooks/test/webhook",
            )
            with (
                patch("control_plane.cli_service._store", return_value=store),
                patch(
                    "control_plane.cli_service.public_ingress_notification_drivers",
                    return_value=drivers,
                ),
                patch("control_plane.outbox_worker.run_outbox_worker_once", side_effect=step),
                patch.object(store, "_database_mutation_timestamp", return_value=queued.created_at),
            ):
                result = CliRunner().invoke(
                    main,
                    [
                        "service",
                        "outbox-workers",
                        "run",
                        "--database-url",
                        store.database_url,
                        "--state-dir",
                        directory,
                    ],
                )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertFalse(unhandled_signals)
            self.assertEqual(len(sends), 1)
            self.assertEqual(steps, 1)
            self.assertEqual(
                store.read_outbox_delivery_record(queued.delivery_id).state, "delivered"
            )
            attempts = store.list_public_ingress_notification_attempt_records()
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0].delivery_status, "delivered")
