"""Admitted event effects keep their drain fence until a terminal commit."""

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import cast
from unittest.mock import Mock, patch

from control_plane.contracts.idempotency_record import (
    complete_launchplane_mutation_reservation,
    parse_launchplane_mutation_timestamp,
)
from control_plane.product_reconcile import reconcile_reservation_scope
from control_plane.generic_web_deploy_http import GENERIC_WEB_DEPLOY_ROUTE
from control_plane.testing_event_deploy_recovery import testing_event_execution_lock_name
from control_plane.service_deploy_drain import (
    SERVICE_DEPLOY_DRAIN_TIMEOUT_SECONDS,
    ServiceDeployDraining,
    ServiceDeployDrainTimeout,
    confirm_startup,
    read_status,
)
from control_plane.storage.postgres import MutationReservationResult, PostgresRecordStore
from tests.test_client_release_redeploy import _prepare, _replacement_env


class EventDeployRedeployTests(unittest.TestCase):
    def test_agent_provider_effects_drain_without_losing_original_custody(self) -> None:
        from control_plane.contracts.odoo_upgrade_rehearsal import ODOO_UPGRADE_REHEARSAL_ROUTE
        from control_plane.odoo_incomplete_rehearsal_retirement import IncompleteRehearsalRetirement
        from control_plane.odoo_upgrade_rehearsal import OdooRehearsalAdapter

        adapter = Mock(spec=OdooRehearsalAdapter)
        adapter.target_key.return_value = "isolated-rehearsal"
        retirement = IncompleteRehearsalRetirement(adapter, "original-execute")
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for index, (route, target) in enumerate(
            (
                (ODOO_UPGRADE_REHEARSAL_ROUTE.format(product="example"), adapter.target_key()),
                (ODOO_UPGRADE_REHEARSAL_ROUTE.format(product="example"), retirement.target_key()),
                ("/v1/another-provider-operation", "another-target"),
            )
        ):
            with self.subTest(route=route, target=target):
                store = PostgresRecordStore(
                    database_url=f"sqlite+pysqlite:///{Path(directory.name) / f'{index}.db'}"
                )
                self.addCleanup(store.close)
                store.ensure_schema()
                intent = dict(
                    scope="local-operator|example-agent",
                    route_path=route,
                    idempotency_key="original-execute",
                    request_fingerprint="reviewed-execute",
                    lease_owner="original-worker",
                    reconciliation_key="original-intent",
                    provider_target_key=target,
                )
                reservation = store.reserve_mutation(**intent).record
                assert reservation is not None
                _, blockers, dispatch = _prepare(store)
                self.assertEqual(blockers, (reservation.record_id,))
                self.assertFalse(dispatch)
                with self.assertRaises(ServiceDeployDraining):
                    store.reserve_mutation(**{**intent, "idempotency_key": "fresh"})
                renewed = store.renew_mutation_reservation(reservation=reservation).record
                assert renewed is not None
                expired = parse_launchplane_mutation_timestamp(
                    renewed.lease_expires_at, field_name="lease_expires_at"
                ) + timedelta(seconds=1)
                with patch.object(
                    store, "_database_mutation_timestamp", return_value=expired.isoformat()
                ):
                    with store.testing_replacement_execution_lock("isolated-rehearsal") as check:
                        assert check is not None
                        _, blockers, dispatch = _prepare(store)
                        self.assertEqual(blockers, (reservation.record_id,))
                        self.assertFalse(dispatch)
                    _, blockers, dispatch = _prepare(store)
                    self.assertEqual(dispatch, route != "/v1/another-provider-operation")
                    self.assertEqual(bool(blockers), not dispatch)
                    held = store.reserve_mutation(**intent).record
                    assert held is not None
                    self.assertEqual(held.record_id, reservation.record_id)
                    self.assertEqual(held.state, "reconcile_required")
                    with self.assertRaises(ServiceDeployDraining):
                        store.retry_reconciled_mutation(reservation=held, lease_owner="retry")

    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(directory.name) / 'records.db'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()

    def reserve(self, key: str = "event", *, lease_seconds: int = 300) -> MutationReservationResult:
        return self.store.reserve_mutation(
            scope=reconcile_reservation_scope("example-product"),
            route_path=GENERIC_WEB_DEPLOY_ROUTE,
            idempotency_key=key,
            request_fingerprint=key,
            lease_owner="event-worker",
            reconciliation_key="original-request-and-target",
            provider_target_key="testing-target",
            lease_seconds=lease_seconds,
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

    def test_expired_reconciliation_hold_survives_replacement_without_blocking_it(self) -> None:
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
        self.assertFalse(blockers)
        self.assertTrue(dispatch)
        held = self.store.lookup_existing_mutation_reservation(
            route_path=reservation.route_path,
            idempotency_key=reservation.idempotency_key,
            request_fingerprint=reservation.request_fingerprint,
        ).record
        assert held is not None
        self.assertEqual(held.state, "reconcile_required")
        self.assertEqual(held.record_id, reservation.record_id)
        self.assertEqual(
            self.store.renew_mutation_reservation(reservation=reservation).status, "not_running"
        )
        self.assertEqual(
            self.store.checkpoint_mutation_provider_effect(
                reservation=reservation, effect_phase="next-provider-write"
            ).status,
            "not_running",
        )
        with patch.dict("os.environ", _replacement_env()):
            confirm_startup(self.store)
            self.assertEqual(self.reserve("another-request").status, "target_busy")

    def test_confirmed_startup_rejects_old_worker_but_admits_matching_generation(self) -> None:
        _, _, dispatch = _prepare(self.store)
        self.assertTrue(dispatch)
        with patch.dict("os.environ", _replacement_env()):
            confirm_startup(self.store)
        with self.assertRaises(ServiceDeployDraining):
            self.reserve()
        with patch.dict("os.environ", _replacement_env()):
            self.assertEqual(self.reserve().status, "acquired")

    def test_self_deploy_proceeds_or_fails_loudly_by_its_absolute_drain_deadline(self) -> None:
        reservation = self.reserve(lease_seconds=SERVICE_DEPLOY_DRAIN_TIMEOUT_SECONDS * 2).record
        assert reservation is not None
        record, _, _ = _prepare(self.store)
        began = parse_launchplane_mutation_timestamp(record.updated_at, field_name="updated_at")
        deadline = began + timedelta(seconds=SERVICE_DEPLOY_DRAIN_TIMEOUT_SECONDS)
        original_deadline = read_status(self.store)["drain_deadline_at"]
        restarted = PostgresRecordStore(database_url=self.store.database_url)
        self.addCleanup(restarted.close)
        _, restarted_blockers, restarted_dispatch = _prepare(restarted)
        self.assertEqual(restarted_blockers, (reservation.record_id,))
        self.assertFalse(restarted_dispatch)
        self.assertEqual(read_status(restarted)["drain_deadline_at"], original_deadline)
        # Repeated successful polls may renew admission, never the total budget.
        for seconds in (
            1,
            SERVICE_DEPLOY_DRAIN_TIMEOUT_SECONDS // 2,
            SERVICE_DEPLOY_DRAIN_TIMEOUT_SECONDS - 1,
        ):
            with patch.object(
                self.store,
                "_database_mutation_timestamp",
                return_value=(began + timedelta(seconds=seconds)).isoformat(),
            ):
                _, blockers, dispatch = _prepare(self.store)
                self.assertFalse(dispatch)
                self.assertEqual(blockers, (reservation.record_id,))
                self.assertEqual(read_status(self.store)["drain_deadline_at"], original_deadline)
        with patch.object(
            self.store, "_database_mutation_timestamp", return_value=deadline.isoformat()
        ):
            with self.assertRaises(ServiceDeployDrainTimeout) as timeout:
                _prepare(self.store)
            self.assertEqual(timeout.exception.blockers, (reservation.record_id,))
            status = read_status(self.store)
            self.assertTrue(status["drain_timed_out"])
            self.assertFalse(status["admission_paused"])
            with self.assertRaises(ServiceDeployDrainTimeout):
                _prepare(self.store)
        # The timeout does not change, retire or fabricate the product outcome.
        observed = self.store.lookup_existing_mutation_reservation(
            route_path=reservation.route_path,
            idempotency_key=reservation.idempotency_key,
            request_fingerprint=reservation.request_fingerprint,
        ).record
        self.assertEqual(observed, reservation)
        restarted.close()
        restarted = PostgresRecordStore(database_url=self.store.database_url)
        self.addCleanup(restarted.close)
        clock = patch.object(
            restarted, "_database_mutation_timestamp", return_value=deadline.isoformat()
        )
        clock.start()
        self.addCleanup(clock.stop)
        self.assertFalse(read_status(restarted)["admission_paused"])
        renewed = restarted.renew_mutation_reservation(reservation=reservation).record
        assert renewed is not None
        self.assertEqual(
            restarted.complete_mutation_reservation(
                completion=complete_launchplane_mutation_reservation(
                    renewed,
                    response_status_code=200,
                    response_trace_id="finished-without-kill",
                    response_payload={"result": {"deploy_status": "pass"}},
                    completed_at=renewed.updated_at,
                )
            ).status,
            "completed",
        )
        # A timed-out intent never dispatches on replay. A fresh normal deploy
        # (fresh key AND marker) is the supported continuation after the alert.
        with self.assertRaises(ServiceDeployDrainTimeout):
            _prepare(restarted)
        from control_plane.service_deploy_drain import prepare

        _, blockers, dispatch = prepare(
            restarted,
            request_fingerprint="fresh-after-timeout",
            target_type=record.target_type,
            target_id=record.target_id,
            image_reference=record.image_reference,
            deployment_marker=record.deployment_marker + "-retry",
        )
        self.assertFalse(blockers)
        self.assertTrue(dispatch)

    def test_active_reconciliation_tail_blocks_but_expired_hold_is_diagnostic_only(self) -> None:
        reservation = self.reserve().record
        assert reservation is not None
        held = self.store.mark_mutation_reconcile_required(
            reservation=reservation, reconciliation_key=reservation.reconciliation_key
        ).record
        assert held is not None
        _, blockers, dispatch = _prepare(self.store)
        self.assertEqual(blockers, (held.record_id,))
        self.assertFalse(dispatch)
        details = cast(tuple[dict[str, object], ...], read_status(self.store)["operation_details"])
        self.assertEqual(details[0]["lease_owner"], held.lease_owner)
        self.assertEqual(details[0]["product"], "example-product")
        self.assertTrue(details[0]["blocks_drain"])
        expired = parse_launchplane_mutation_timestamp(
            held.lease_expires_at, field_name="lease_expires_at"
        ) + timedelta(seconds=1)
        with patch.object(
            self.store, "_database_mutation_timestamp", return_value=expired.isoformat()
        ):
            with self.store.testing_replacement_execution_lock(
                testing_event_execution_lock_name(held.provider_target_key)
            ) as check:
                assert check is not None
                _, blockers, dispatch = _prepare(self.store)
                self.assertEqual(blockers, (held.record_id,))
                self.assertFalse(dispatch)
            _, blockers, dispatch = _prepare(self.store)
            self.assertFalse(blockers)
            self.assertTrue(dispatch)
            details = cast(
                tuple[dict[str, object], ...], read_status(self.store)["operation_details"]
            )
            self.assertEqual(details[0]["state"], "reconcile_required")
            self.assertFalse(details[0]["blocks_drain"])

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
        with self.assertRaises(ServiceDeployDraining):
            self.store.supersede_expired_reconciled_mutation_and_reserve(
                reservation=held,
                response_status_code=502,
                response_trace_id="terminal-proof",
                response_payload={"result": {"deploy_status": "fail"}},
                scope=held.scope,
                route_path=held.route_path,
                idempotency_key="fresh-effect",
                request_fingerprint="fresh-effect",
                lease_owner="new-worker",
                lease_seconds=300,
                minimum_expired_seconds=0,
                reconciliation_key=held.reconciliation_key,
                provider_target_key=held.provider_target_key,
            )
        self.assertEqual(self.reserve().record, held)
