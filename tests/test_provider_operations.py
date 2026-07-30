from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from control_plane.provider_operations import (
    DurableProviderOperationResult,
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    ProviderTargetSupersession,
    run_durable_provider_operation,
)
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.storage.postgres import PostgresRecordStore


_SCOPE = "github-actions:provider-operation-test"
_ROUTE = "/v1/test/provider-operation"
_KEY = "provider-op:test:1"
_FINGERPRINT = "provider-op-fingerprint-a"
_RECONCILIATION_KEY = "dokploy-compose:preview-abc"


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


class _FakeAdapter:
    def __init__(
        self,
        *,
        reconciliation_key: str = _RECONCILIATION_KEY,
        apply_outcome: ProviderMutationOutcome | None = None,
        apply_error: BaseException | None = None,
        observation: ProviderObservation | None = None,
        apply_delay_seconds: float = 0,
        apply_started: Event | None = None,
        apply_release: Event | None = None,
        checkpoint_before_rejection: bool = False,
    ) -> None:
        self._reconciliation_key = reconciliation_key
        self._apply_outcome = apply_outcome or ProviderMutationOutcome(
            response_status_code=202,
            response_payload={"trace_id": "provider-op-effect", "status": "accepted"},
        )
        self._apply_error = apply_error
        self._observation = observation or ProviderObservation(outcome="unknown")
        self._apply_delay_seconds = apply_delay_seconds
        self._apply_started = apply_started
        self._apply_release = apply_release
        self._checkpoint_before_rejection = checkpoint_before_rejection
        self.apply_calls = 0
        self.observe_calls = 0
        self.provider_operation_keys: list[str] = []
        self.observed_effect_phases: list[str] = []
        self.observed_reconciliation_keys: list[str] = []

    def reconciliation_key(self) -> str:
        return self._reconciliation_key

    def target_key(self) -> str:
        return f"provider-target:{self._reconciliation_key}"

    def observe(
        self,
        provider_operation_key: str,
        provider_effect_phase: str,
        reconciliation_key: str,
    ) -> ProviderObservation:
        self.provider_operation_keys.append(provider_operation_key)
        self.observed_effect_phases.append(provider_effect_phase)
        self.observed_reconciliation_keys.append(reconciliation_key)
        self.observe_calls += 1
        return self._observation

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        self.provider_operation_keys.append(provider_operation_key)
        self.apply_calls += 1
        if (
            not isinstance(self._apply_error, ProviderMutationRejectedError)
            or self._checkpoint_before_rejection
        ) and (self._apply_error is not None or self._apply_outcome.provider_effect_performed):
            lease.checkpoint_effect("test_effect")
        if self._apply_started is not None:
            self._apply_started.set()
        if self._apply_release is not None:
            self._apply_release.wait(timeout=5)
        if self._apply_delay_seconds:
            time.sleep(self._apply_delay_seconds)
        if self._apply_error is not None:
            raise self._apply_error
        return self._apply_outcome


class _StoreFixture:
    def __init__(self, directory: str) -> None:
        self.store = PostgresRecordStore(
            database_url=_sqlite_database_url(Path(directory) / "launchplane.sqlite3")
        )
        self.store.ensure_schema()

    def run(
        self,
        adapter: _FakeAdapter,
        *,
        lease_owner: str = "instance-a",
        response_trace_id: str = "provider-op-trace-a",
        lease_seconds: int = 300,
        heartbeat_interval_seconds: float | None = None,
        idempotency_key: str = _KEY,
        request_fingerprint: str = _FINGERPRINT,
        target_supersession: ProviderTargetSupersession | None = None,
    ) -> DurableProviderOperationResult:
        return run_durable_provider_operation(
            store=self.store,
            scope=_SCOPE,
            route_path=_ROUTE,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            lease_owner=lease_owner,
            response_trace_id=response_trace_id,
            adapter=adapter,
            lease_seconds=lease_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            target_supersession=target_supersession,
        )

    def maybe_stored(self) -> LaunchplaneIdempotencyRecord | None:
        return self.store.read_idempotency_record(
            scope=_SCOPE,
            route_path=_ROUTE,
            idempotency_key=_KEY,
        )

    def stored(self) -> LaunchplaneIdempotencyRecord:
        record = self.maybe_stored()
        assert record is not None
        return record


class DurableProviderOperationRunnerTests(unittest.TestCase):
    def test_fresh_acquire_applies_once_and_completes(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _StoreFixture(directory)
            adapter = _FakeAdapter()

            result = fixture.run(adapter)

            self.assertEqual(result.status, "completed")
            self.assertEqual(adapter.apply_calls, 1)
            self.assertEqual(adapter.observe_calls, 0)
            stored = fixture.stored()
            self.assertEqual(getattr(stored, "state"), "completed")
            self.assertEqual(getattr(stored, "reconciliation_key"), _RECONCILIATION_KEY)
            self.assertEqual(
                getattr(stored, "response_payload"),
                {"trace_id": "provider-op-effect", "status": "accepted"},
            )
            self.assertEqual(len(adapter.provider_operation_keys), 1)
            self.assertTrue(adapter.provider_operation_keys[0].startswith("provider-operation:"))

    def test_long_apply_renews_lease_and_completes_with_latest_reservation(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _StoreFixture(directory)
            adapter = _FakeAdapter(apply_delay_seconds=0.05)
            with patch.object(
                fixture.store,
                "renew_mutation_reservation",
                wraps=fixture.store.renew_mutation_reservation,
            ) as renew:
                result = fixture.run(
                    adapter,
                    lease_seconds=2,
                    heartbeat_interval_seconds=0.01,
                )

            self.assertEqual(result.status, "completed")
            self.assertGreaterEqual(renew.call_count, 1)

    def test_successful_outcome_survives_transient_heartbeat_failure(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _StoreFixture(directory)
            adapter = _FakeAdapter(apply_delay_seconds=0.03)
            with patch.object(
                fixture.store,
                "renew_mutation_reservation",
                side_effect=RuntimeError("temporary database outage"),
            ):
                result = fixture.run(
                    adapter,
                    lease_seconds=2,
                    heartbeat_interval_seconds=0.01,
                )

            self.assertEqual(result.status, "completed")
            self.assertEqual(fixture.stored().state, "completed")

    def test_active_target_fence_blocks_different_idempotency_key(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _StoreFixture(directory)
            adapter = _FakeAdapter()
            fixture.store.reserve_mutation(
                scope=_SCOPE,
                route_path=_ROUTE,
                idempotency_key="provider-op:other-key",
                request_fingerprint="provider-op-fingerprint-b",
                lease_owner="instance-a",
                lease_seconds=300,
                reconciliation_key=_RECONCILIATION_KEY,
                provider_target_key=adapter.target_key(),
            )

            result = fixture.run(adapter, lease_owner="instance-b")

            self.assertEqual(result.status, "target_busy")
            self.assertEqual(adapter.apply_calls, 0)

    def test_destroy_supersedes_expired_reconcile_required_target(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _StoreFixture(directory)
            adapter = _FakeAdapter()
            clock = {"now": "2026-07-30T15:00:00Z"}
            with patch.object(
                fixture.store,
                "_database_mutation_timestamp",
                side_effect=lambda _session: clock["now"],
            ):
                stale = fixture.store.reserve_mutation(
                    scope=_SCOPE,
                    route_path=_ROUTE,
                    idempotency_key="provider-op:refresh:stale",
                    request_fingerprint="provider-op-fingerprint-stale",
                    lease_owner="instance-a",
                    lease_seconds=60,
                    reconciliation_key=_RECONCILIATION_KEY,
                    provider_target_key=adapter.target_key(),
                )
                marked = fixture.store.mark_mutation_reconcile_required(
                    reservation=stale.record,
                    reconciliation_key=_RECONCILIATION_KEY,
                )
                clock["now"] = "2026-07-30T15:17:00Z"
                result = fixture.run(
                    adapter,
                    lease_owner="instance-b",
                    response_trace_id="destroy-trace",
                    idempotency_key="provider-op:destroy:new",
                    request_fingerprint="provider-op-fingerprint-destroy",
                    target_supersession=ProviderTargetSupersession(
                        response_status_code=409,
                        response_payload={"status": "superseded"},
                        minimum_expired_seconds=900,
                        quiescence_check=lambda _reservation: True,
                    ),
                )
            stored_stale = fixture.store.read_idempotency_record(
                scope=_SCOPE,
                route_path=_ROUTE,
                idempotency_key="provider-op:refresh:stale",
            )

            self.assertEqual(marked.status, "updated")
            self.assertEqual(result.status, "completed")
            self.assertEqual(adapter.apply_calls, 1)
            self.assertIsNotNone(stored_stale)
            assert stored_stale is not None
            self.assertEqual(stored_stale.state, "completed")
            self.assertEqual(stored_stale.response_status_code, 409)
            self.assertEqual(stored_stale.response_payload, {"status": "superseded"})

    def test_destroy_does_not_supersede_reconciled_target_with_active_lease(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _StoreFixture(directory)
            adapter = _FakeAdapter()
            clock = {"now": "2026-07-30T15:00:00Z"}
            with patch.object(
                fixture.store,
                "_database_mutation_timestamp",
                side_effect=lambda _session: clock["now"],
            ):
                stale = fixture.store.reserve_mutation(
                    scope=_SCOPE,
                    route_path=_ROUTE,
                    idempotency_key="provider-op:refresh:settling",
                    request_fingerprint="provider-op-fingerprint-settling",
                    lease_owner="instance-a",
                    lease_seconds=300,
                    reconciliation_key=_RECONCILIATION_KEY,
                    provider_target_key=adapter.target_key(),
                )
                fixture.store.mark_mutation_reconcile_required(
                    reservation=stale.record,
                    reconciliation_key=_RECONCILIATION_KEY,
                )
                result = fixture.run(
                    adapter,
                    lease_owner="instance-b",
                    response_trace_id="destroy-trace",
                    idempotency_key="provider-op:destroy:new",
                    request_fingerprint="provider-op-fingerprint-destroy",
                    target_supersession=ProviderTargetSupersession(
                        response_status_code=409,
                        response_payload={"status": "superseded"},
                        quiescence_check=lambda _reservation: True,
                    ),
                )

            self.assertEqual(result.status, "target_busy")
            self.assertEqual(adapter.apply_calls, 0)

    def test_destroy_does_not_supersede_without_provider_quiescence(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _StoreFixture(directory)
            adapter = _FakeAdapter()
            clock = {"now": "2026-07-30T15:00:00Z"}
            with patch.object(
                fixture.store,
                "_database_mutation_timestamp",
                side_effect=lambda _session: clock["now"],
            ):
                stale = fixture.store.reserve_mutation(
                    scope=_SCOPE,
                    route_path=_ROUTE,
                    idempotency_key="provider-op:refresh:not-quiescent",
                    request_fingerprint="provider-op-fingerprint-not-quiescent",
                    lease_owner="instance-a",
                    lease_seconds=60,
                    reconciliation_key=_RECONCILIATION_KEY,
                    provider_target_key=adapter.target_key(),
                )
                fixture.store.mark_mutation_reconcile_required(
                    reservation=stale.record,
                    reconciliation_key=_RECONCILIATION_KEY,
                )
                clock["now"] = "2026-07-30T15:17:00Z"
                result = fixture.run(
                    adapter,
                    lease_owner="instance-b",
                    response_trace_id="destroy-trace",
                    idempotency_key="provider-op:destroy:new",
                    request_fingerprint="provider-op-fingerprint-destroy",
                    target_supersession=ProviderTargetSupersession(
                        response_status_code=409,
                        response_payload={"status": "superseded"},
                        minimum_expired_seconds=900,
                        quiescence_check=lambda _reservation: False,
                    ),
                )

            self.assertEqual(result.status, "target_busy")
            self.assertEqual(adapter.apply_calls, 0)

    def test_replays_completed_operation_without_reapplying(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _StoreFixture(directory)
            first = _FakeAdapter()
            fixture.run(first)

            second = _FakeAdapter()
            result = fixture.run(
                second,
                lease_owner="instance-b",
                response_trace_id="provider-op-trace-b",
            )

            self.assertEqual(result.status, "replayed")
            self.assertEqual(second.apply_calls, 0)
            self.assertEqual(result.original_trace_id, "provider-op-trace-a")
            self.assertEqual(
                result.response_payload,
                {"trace_id": "provider-op-effect", "status": "accepted"},
            )

    def test_second_instance_sees_in_progress_and_does_not_apply(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _StoreFixture(directory)
            fixture.store.reserve_mutation(
                scope=_SCOPE,
                route_path=_ROUTE,
                idempotency_key=_KEY,
                request_fingerprint=_FINGERPRINT,
                lease_owner="instance-a",
                lease_seconds=300,
                reconciliation_key=_RECONCILIATION_KEY,
            )

            second = _FakeAdapter()
            result = fixture.run(
                second,
                lease_owner="instance-b",
                response_trace_id="provider-op-trace-b",
            )

            self.assertEqual(result.status, "in_progress")
            self.assertEqual(second.apply_calls, 0)

    def test_crash_then_reconcile_adopts_observed_effect_without_reapplying(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _StoreFixture(directory)
            crashed = _FakeAdapter(apply_error=RuntimeError("process died mid-apply"))
            with self.assertRaises(RuntimeError):
                fixture.run(crashed)
            stored = fixture.stored()
            fixture.store.mark_mutation_reconcile_required(
                reservation=stored,
                reconciliation_key=_RECONCILIATION_KEY,
            )

            recovery = _FakeAdapter(
                observation=ProviderObservation(
                    outcome="present",
                    response_status_code=202,
                    response_payload={"trace_id": "adopted", "status": "accepted"},
                )
            )
            result = fixture.run(
                recovery,
                lease_owner="instance-b",
                response_trace_id="provider-op-trace-b",
            )

            self.assertEqual(result.status, "adopted")
            self.assertEqual(recovery.apply_calls, 0)
            self.assertEqual(recovery.observe_calls, 1)
            self.assertEqual(getattr(fixture.stored(), "state"), "completed")

    def test_reconcile_uses_stored_target_key_after_adapter_authority_changes(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _StoreFixture(directory)
            reservation = fixture.store.reserve_mutation(
                scope=_SCOPE,
                route_path=_ROUTE,
                idempotency_key=_KEY,
                request_fingerprint=_FINGERPRINT,
                lease_owner="instance-a",
                lease_seconds=300,
                reconciliation_key=_RECONCILIATION_KEY,
            ).record
            fixture.store.mark_mutation_reconcile_required(
                reservation=reservation,
                reconciliation_key=_RECONCILIATION_KEY,
            )
            recovery = _FakeAdapter(
                reconciliation_key="dokploy-compose:replacement-target",
                observation=ProviderObservation(
                    outcome="present",
                    response_status_code=202,
                    response_payload={"trace_id": "adopted", "status": "accepted"},
                ),
            )

            result = fixture.run(
                recovery,
                lease_owner="instance-b",
                response_trace_id="provider-op-trace-b",
            )

            self.assertEqual(result.status, "adopted")
            self.assertEqual(recovery.apply_calls, 0)
            self.assertEqual(recovery.observed_reconciliation_keys, [_RECONCILIATION_KEY])

    def test_reconcile_stays_fail_closed_when_effect_unknown(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _StoreFixture(directory)
            crashed = _FakeAdapter(apply_error=RuntimeError("process died mid-apply"))
            with self.assertRaises(RuntimeError):
                fixture.run(crashed)
            fixture.store.mark_mutation_reconcile_required(
                reservation=fixture.stored(),
                reconciliation_key=_RECONCILIATION_KEY,
            )

            recovery = _FakeAdapter(observation=ProviderObservation(outcome="unknown"))
            result = fixture.run(
                recovery,
                lease_owner="instance-b",
                response_trace_id="provider-op-trace-b",
            )

            self.assertEqual(result.status, "reconcile_required")
            self.assertEqual(recovery.apply_calls, 0)
            self.assertEqual(getattr(fixture.stored(), "state"), "reconcile_required")

    def test_reconcile_retries_once_when_adapter_proves_effect_absent(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _StoreFixture(directory)
            reservation = fixture.store.reserve_mutation(
                scope=_SCOPE,
                route_path=_ROUTE,
                idempotency_key=_KEY,
                request_fingerprint=_FINGERPRINT,
                lease_owner="instance-a",
                lease_seconds=300,
                reconciliation_key=_RECONCILIATION_KEY,
            ).record
            fixture.store.mark_mutation_reconcile_required(
                reservation=reservation,
                reconciliation_key=_RECONCILIATION_KEY,
            )
            adapter = _FakeAdapter(observation=ProviderObservation(outcome="absent"))

            result = fixture.run(adapter, lease_owner="instance-b")

            self.assertEqual(result.status, "completed")
            self.assertEqual(adapter.observe_calls, 1)
            self.assertEqual(adapter.apply_calls, 1)
            self.assertEqual(fixture.stored().state, "completed")

    def test_effect_checkpoint_blocks_absent_retry_while_stale_worker_can_finish(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            holder_fixture = _StoreFixture(directory)
            recovery_fixture = _StoreFixture(directory)
            apply_started = Event()
            release_apply = Event()
            holder = _FakeAdapter(
                apply_started=apply_started,
                apply_release=release_apply,
            )
            holder_result: dict[str, DurableProviderOperationResult] = {}

            def run_holder() -> None:
                holder_result["result"] = holder_fixture.run(
                    holder,
                    lease_seconds=1,
                    heartbeat_interval_seconds=0.1,
                )

            with patch.object(
                holder_fixture.store,
                "renew_mutation_reservation",
                side_effect=RuntimeError("database unavailable"),
            ):
                holder_thread = Thread(target=run_holder)
                holder_thread.start()
                self.assertTrue(apply_started.wait(timeout=2))
                time.sleep(1.1)

                recovery = _FakeAdapter(observation=ProviderObservation(outcome="absent"))
                recovery_result = recovery_fixture.run(
                    recovery,
                    lease_owner="instance-b",
                    response_trace_id="provider-op-trace-b",
                    lease_seconds=1,
                    heartbeat_interval_seconds=0.1,
                )

                self.assertEqual(recovery_result.status, "reconcile_required")
                self.assertEqual(recovery.apply_calls, 0)
                self.assertEqual(recovery.observed_effect_phases, ["test_effect"])
                release_apply.set()
                holder_thread.join(timeout=5)

            self.assertFalse(holder_thread.is_alive())
            self.assertIn(holder_result["result"].status, {"adopted", "replayed"})
            self.assertEqual(holder.apply_calls, 1)

    def test_unknown_outcome_marks_reconcile_required(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _StoreFixture(directory)
            adapter = _FakeAdapter(
                apply_error=ProviderMutationUnknownError("timed out after dispatch"),
            )

            result = fixture.run(adapter)

            self.assertEqual(result.status, "reconcile_required")
            self.assertEqual(adapter.apply_calls, 1)
            stored = fixture.stored()
            self.assertEqual(getattr(stored, "state"), "reconcile_required")
            self.assertEqual(getattr(stored, "reconciliation_key"), _RECONCILIATION_KEY)

    def test_cancellation_leaves_running_reservation_intact(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _StoreFixture(directory)
            adapter = _FakeAdapter(apply_error=KeyboardInterrupt())

            with self.assertRaises(KeyboardInterrupt):
                fixture.run(adapter)

            stored = fixture.stored()
            self.assertIsNotNone(stored)
            self.assertEqual(getattr(stored, "state"), "reconcile_required")
            self.assertEqual(getattr(stored, "reconciliation_key"), _RECONCILIATION_KEY)
            self.assertIsNone(getattr(stored, "response_status_code"))

    def test_rejected_effect_releases_reservation_and_reraises(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _StoreFixture(directory)
            cause = ValueError("request failed local validation before any effect")
            adapter = _FakeAdapter(apply_error=ProviderMutationRejectedError(cause))

            with self.assertRaises(ValueError):
                fixture.run(adapter)

            self.assertIsNone(fixture.maybe_stored())

    def test_rejected_effect_after_checkpoint_requires_reconciliation(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _StoreFixture(directory)
            cause = ValueError("provider response could not be parsed after dispatch")
            adapter = _FakeAdapter(
                apply_error=ProviderMutationRejectedError(cause),
                checkpoint_before_rejection=True,
            )

            result = fixture.run(adapter)

            self.assertEqual(result.status, "reconcile_required")
            stored = fixture.stored()
            self.assertEqual(stored.state, "reconcile_required")
            self.assertEqual(stored.provider_effect_phase, "test_effect")

    def test_non_durable_outcome_after_checkpoint_requires_reconciliation(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _StoreFixture(directory)
            adapter = _FakeAdapter(
                apply_outcome=ProviderMutationOutcome(
                    response_status_code=409,
                    response_payload={"status": "blocked"},
                    durable=False,
                    provider_effect_performed=True,
                )
            )

            result = fixture.run(adapter)

            self.assertEqual(result.status, "reconcile_required")
            self.assertEqual(fixture.stored().state, "reconcile_required")

    def test_conflicting_fingerprint_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _StoreFixture(directory)
            with self.assertRaises(RuntimeError):
                fixture.run(_FakeAdapter(apply_error=RuntimeError("holds the lease")))

            conflict = run_durable_provider_operation(
                store=fixture.store,
                scope=_SCOPE,
                route_path=_ROUTE,
                idempotency_key=_KEY,
                request_fingerprint="a-different-payload-fingerprint",
                lease_owner="instance-b",
                response_trace_id="provider-op-trace-b",
                adapter=_FakeAdapter(),
            )

            self.assertEqual(conflict.status, "conflict")


class MutationReservationAdoptionStorageTests(unittest.TestCase):
    def _reconcile_required_store(
        self, directory: str
    ) -> tuple[PostgresRecordStore, LaunchplaneIdempotencyRecord]:
        store = PostgresRecordStore(
            database_url=_sqlite_database_url(Path(directory) / "launchplane.sqlite3")
        )
        store.ensure_schema()
        reservation = store.reserve_mutation(
            scope=_SCOPE,
            route_path=_ROUTE,
            idempotency_key=_KEY,
            request_fingerprint=_FINGERPRINT,
            lease_owner="instance-a",
            lease_seconds=300,
            reconciliation_key=_RECONCILIATION_KEY,
        ).record
        marked = store.mark_mutation_reconcile_required(
            reservation=reservation,
            reconciliation_key=_RECONCILIATION_KEY,
        )
        assert marked.record is not None
        return store, marked.record

    def test_adopts_reconcile_required_with_matching_key(self) -> None:
        with TemporaryDirectory() as directory:
            store, reservation = self._reconcile_required_store(directory)

            adoption = store.adopt_reconciled_mutation(
                reservation=reservation,
                response_status_code=202,
                response_trace_id="adopting-instance",
                response_payload={"status": "accepted"},
            )

            self.assertEqual(adoption.status, "adopted")
            self.assertEqual(getattr(adoption.record, "state"), "completed")

    def test_adoption_rejects_reservation_mismatch(self) -> None:
        with TemporaryDirectory() as directory:
            store, reservation = self._reconcile_required_store(directory)
            mismatched_reservation = LaunchplaneIdempotencyRecord.model_validate(
                {
                    **reservation.model_dump(mode="json"),
                    "reconciliation_key": "dokploy-compose:some-other-target",
                }
            )

            adoption = store.adopt_reconciled_mutation(
                reservation=mismatched_reservation,
                response_status_code=202,
                response_trace_id="adopting-instance",
                response_payload={"status": "accepted"},
            )

            self.assertEqual(adoption.status, "reservation_mismatch")
            self.assertEqual(
                getattr(
                    store.read_idempotency_record(
                        scope=_SCOPE,
                        route_path=_ROUTE,
                        idempotency_key=_KEY,
                    ),
                    "state",
                ),
                "reconcile_required",
            )

    def test_adoption_conflicts_on_fingerprint_mismatch(self) -> None:
        with TemporaryDirectory() as directory:
            store, reservation = self._reconcile_required_store(directory)
            mismatched_reservation = LaunchplaneIdempotencyRecord.model_validate(
                {
                    **reservation.model_dump(mode="json"),
                    "request_fingerprint": "mismatched-fingerprint",
                }
            )

            adoption = store.adopt_reconciled_mutation(
                reservation=mismatched_reservation,
                response_status_code=202,
                response_trace_id="adopting-instance",
                response_payload={"status": "accepted"},
            )

            self.assertEqual(adoption.status, "conflict")

    def test_stale_observation_cannot_adopt_newer_reconcile_required_attempt(self) -> None:
        with TemporaryDirectory() as directory:
            store, first_reconcile = self._reconcile_required_store(directory)
            retry = store.retry_reconciled_mutation(
                reservation=first_reconcile,
                lease_owner="instance-b",
                lease_seconds=300,
            )
            assert retry.record is not None
            second_reconcile = store.mark_mutation_reconcile_required(
                reservation=retry.record,
                reconciliation_key=retry.record.reconciliation_key,
            )

            adoption = store.adopt_reconciled_mutation(
                reservation=first_reconcile,
                response_status_code=202,
                response_trace_id="stale-observer",
                response_payload={"status": "accepted"},
            )

            self.assertEqual(second_reconcile.status, "updated")
            self.assertEqual(adoption.status, "reservation_mismatch")
            assert adoption.record is not None
            self.assertEqual(adoption.record.attempt, first_reconcile.attempt + 1)
            self.assertEqual(adoption.record.state, "reconcile_required")

    def test_release_removes_own_running_reservation(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            reservation = store.reserve_mutation(
                scope=_SCOPE,
                route_path=_ROUTE,
                idempotency_key=_KEY,
                request_fingerprint=_FINGERPRINT,
                lease_owner="instance-a",
                lease_seconds=300,
                reconciliation_key=_RECONCILIATION_KEY,
            ).record

            release = store.release_reserved_mutation(reservation=reservation)

            self.assertEqual(release.status, "released")
            self.assertIsNone(
                store.read_idempotency_record(scope=_SCOPE, route_path=_ROUTE, idempotency_key=_KEY)
            )

    def test_release_refuses_foreign_owner(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            reservation = store.reserve_mutation(
                scope=_SCOPE,
                route_path=_ROUTE,
                idempotency_key=_KEY,
                request_fingerprint=_FINGERPRINT,
                lease_owner="instance-a",
                lease_seconds=300,
                reconciliation_key=_RECONCILIATION_KEY,
            ).record
            foreign = reservation.model_copy(update={"lease_owner": "instance-b"})

            release = store.release_reserved_mutation(reservation=foreign)

            self.assertEqual(release.status, "owner_mismatch")
            self.assertIsNotNone(
                store.read_idempotency_record(scope=_SCOPE, route_path=_ROUTE, idempotency_key=_KEY)
            )


if __name__ == "__main__":
    unittest.main()
