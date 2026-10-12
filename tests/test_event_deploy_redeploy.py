"""Admitted event effects keep their drain fence until a terminal commit."""

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.idempotency_record import (
    complete_launchplane_mutation_reservation,
    parse_launchplane_mutation_timestamp,
)
from control_plane.product_reconcile import reconcile_reservation_scope
from control_plane.service_deploy_drain import ServiceDeployDraining, confirm_startup
from control_plane.storage.postgres import MutationReservationResult, PostgresRecordStore
from tests.test_client_release_redeploy import _prepare, _replacement_env


class EventDeployRedeployTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(directory.name) / 'records.db'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()

    def reserve(self, key: str = "event") -> MutationReservationResult:
        return self.store.reserve_mutation(
            scope=reconcile_reservation_scope("example-product"),
            route_path="/isolated/event-deploy",
            idempotency_key=key,
            request_fingerprint=key,
            lease_owner="event-worker",
            reconciliation_key="original-request-and-target",
            provider_target_key="testing-target",
        )

    def test_admitted_event_finishes_heartbeat_and_terminal_write_while_draining(self) -> None:
        reservation = self.reserve().record
        assert reservation is not None
        _, blockers, dispatch = _prepare(self.store)
        self.assertEqual(blockers, (reservation.record_id,))
        self.assertFalse(dispatch)
        renewed = self.store.renew_mutation_reservation(reservation=reservation)
        self.assertEqual(renewed.status, "updated")
        assert renewed.record is not None
        completion = self.store.complete_mutation_reservation(
            completion=complete_launchplane_mutation_reservation(
                renewed.record,
                response_status_code=502,
                response_trace_id="proved-failure",
                response_payload={"result": {"deploy_status": "fail"}},
                completed_at=renewed.record.updated_at,
            )
        )
        self.assertEqual(completion.status, "completed")
        _, blockers, dispatch = _prepare(self.store)
        self.assertFalse(blockers)
        self.assertTrue(dispatch)

    def test_expired_effect_still_blocks_after_reconciliation_transition(self) -> None:
        reservation = self.reserve().record
        assert reservation is not None
        future = (
            parse_launchplane_mutation_timestamp(
                reservation.lease_expires_at, field_name="lease_expires_at"
            )
            + timedelta(seconds=1)
        ).isoformat()
        with patch.object(self.store, "_database_mutation_timestamp", return_value=future):
            self.assertEqual(self.reserve("new-event").status, "target_busy")
            _, blockers, dispatch = _prepare(self.store)
        self.assertEqual(blockers, (reservation.record_id,))
        self.assertFalse(dispatch)

    def test_confirmed_startup_rejects_old_worker_but_admits_matching_generation(self) -> None:
        _, _, dispatch = _prepare(self.store)
        self.assertTrue(dispatch)
        with patch.dict("os.environ", _replacement_env()):
            confirm_startup(self.store)
        with self.assertRaises(ServiceDeployDraining):
            self.reserve()
        with patch.dict("os.environ", _replacement_env()):
            self.assertEqual(self.reserve().status, "acquired")

    def test_drain_keeps_observation_but_refuses_retried_effect_admission(self) -> None:
        reservation = self.reserve().record
        assert reservation is not None
        held = self.store.mark_mutation_reconcile_required(
            reservation=reservation, reconciliation_key=reservation.reconciliation_key
        ).record
        assert held is not None
        _prepare(self.store)
        self.assertEqual(self.reserve().status, "reconcile_required")
        with self.assertRaises(ServiceDeployDraining):
            self.store.retry_reconciled_mutation(reservation=held, lease_owner="new-worker")
        self.assertEqual(self.reserve().record, held)
