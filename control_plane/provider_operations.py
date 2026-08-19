"""Shared durable provider-operation runner.

Provider mutations (Dokploy compose applies, GitHub workflow dispatches, ingress
applies) are non-repeatable effects. This module moves them behind the durable
mutation-reservation model from ``control_plane.contracts.idempotency_record`` so
that a process restart, a second instance, or a cancelled request can never
produce a blind duplicate effect and can never erase operation state.

The runner reserves a bounded lease and binds a deterministic provider
reconciliation key before any effect begins. After the effect, it records
terminal evidence. A crash or ambiguous provider outcome after key binding is an
unknown outcome that transitions the reservation to ``reconcile_required`` rather
than repeating the effect; provider-specific reconciliation stays behind the
:class:`DurableProviderMutationAdapter` boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
from threading import Event, Lock, Thread
from typing import Callable, Literal, NamedTuple, Protocol, runtime_checkable

from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    complete_launchplane_mutation_reservation,
)
from control_plane.storage.postgres import (
    MutationReconciliationSupersessionResult,
    MutationReservationAdoptionResult,
    MutationReservationCompletionResult,
    MutationReconciliationRetryResult,
    MutationReservationReleaseResult,
    MutationReservationResult,
    MutationReservationUpdateResult,
)


ProviderObservationOutcome = Literal["present", "absent", "unknown"]

DurableProviderOperationStatus = Literal[
    "completed",
    "unstored",
    "adopted",
    "replayed",
    "conflict",
    "target_busy",
    "in_progress",
    "reconcile_required",
]


def provider_operation_response_payload(
    *,
    trace_id: str,
    records: Mapping[str, object],
    result: dict[str, object],
) -> dict[str, object]:
    return {
        "status": "accepted",
        "trace_id": trace_id,
        "records": dict(records),
        "result": result,
    }


class ProviderMutationRejectedError(Exception):
    """Raised by an adapter when the provider effect definitely did not happen.

    The adapter guarantees no provider state changed (for example a request that
    failed local validation before any provider call). The runner releases the
    fresh reservation and re-raises the underlying cause so the caller can map it
    to a client error without leaving a poisoned reservation behind.
    """

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


class ProviderMutationUnknownError(Exception):
    """Raised by an adapter when the provider outcome is ambiguous.

    The effect may or may not have taken hold (for example a timeout after
    dispatch). The runner marks the reservation ``reconcile_required`` so the
    effect is never blindly repeated.
    """


@dataclass(frozen=True)
class ProviderMutationOutcome:
    response_status_code: int
    response_payload: dict[str, object] = field(default_factory=dict)
    durable: bool = True
    provider_effect_performed: bool = True


@dataclass(frozen=True)
class ProviderObservation:
    outcome: ProviderObservationOutcome
    response_status_code: int = 202
    response_payload: dict[str, object] = field(default_factory=dict)
    retry_safe: bool = False


@dataclass(frozen=True)
class ProviderTargetSupersession:
    response_status_code: int
    response_payload: dict[str, object] = field(default_factory=dict)
    minimum_expired_seconds: int = 0
    quiescence_check: Callable[[LaunchplaneIdempotencyRecord], bool] = field(
        default=lambda _reservation: False
    )


class ProviderOperationLease(Protocol):
    def assert_current(self) -> None:
        """Renew and verify the current reservation before provider work continues."""

    def checkpoint_effect(self, phase: str) -> None:
        """Fence and persist a provider mutation phase immediately before dispatch."""


@runtime_checkable
class DurableProviderMutationAdapter(Protocol):
    def target_key(self) -> str:
        """Return the stable key used to fence concurrent mutations of one target."""

    def reconciliation_key(self) -> str:
        """Return the deterministic key identifying the provider mutation target."""

    def observe(
        self,
        provider_operation_key: str,
        provider_effect_phase: str,
        reconciliation_key: str,
    ) -> ProviderObservation:
        """Report whether the bound provider effect is already present."""

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        """Perform the provider mutation and return its terminal evidence."""


@runtime_checkable
class ProviderEffectTimestampObserver(Protocol):
    def observe_with_effect_started_at(
        self,
        provider_operation_key: str,
        provider_effect_phase: str,
        reconciliation_key: str,
        provider_effect_started_at: str,
    ) -> ProviderObservation:
        """Observe a provider effect using its authoritative checkpoint time."""


class DurableProviderOperationStore(Protocol):
    def reserve_mutation(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
        request_fingerprint: str,
        lease_owner: str,
        lease_seconds: int = ...,
        reconciliation_key: str = ...,
        provider_target_key: str = ...,
    ) -> MutationReservationResult: ...

    def complete_mutation_reservation(
        self,
        *,
        completion: LaunchplaneIdempotencyRecord,
    ) -> MutationReservationCompletionResult: ...

    def renew_mutation_reservation(
        self,
        *,
        reservation: LaunchplaneIdempotencyRecord,
        lease_seconds: int = ...,
    ) -> MutationReservationUpdateResult: ...

    def checkpoint_mutation_provider_effect(
        self,
        *,
        reservation: LaunchplaneIdempotencyRecord,
        effect_phase: str,
        lease_seconds: int = ...,
    ) -> MutationReservationUpdateResult: ...

    def mark_mutation_reconcile_required(
        self,
        *,
        reservation: LaunchplaneIdempotencyRecord,
        reconciliation_key: str,
    ) -> MutationReservationUpdateResult: ...

    def adopt_reconciled_mutation(
        self,
        *,
        reservation: LaunchplaneIdempotencyRecord,
        response_status_code: int,
        response_trace_id: str,
        response_payload: dict[str, object],
    ) -> MutationReservationAdoptionResult: ...

    def supersede_expired_reconciled_mutation_and_reserve(
        self,
        *,
        reservation: LaunchplaneIdempotencyRecord,
        response_status_code: int,
        response_trace_id: str,
        response_payload: dict[str, object],
        scope: str,
        route_path: str,
        idempotency_key: str,
        request_fingerprint: str,
        lease_owner: str,
        lease_seconds: int,
        minimum_expired_seconds: int,
        reconciliation_key: str,
        provider_target_key: str,
    ) -> MutationReconciliationSupersessionResult: ...

    def release_reserved_mutation(
        self,
        *,
        reservation: LaunchplaneIdempotencyRecord,
    ) -> MutationReservationReleaseResult: ...

    def retry_reconciled_mutation(
        self,
        *,
        reservation: LaunchplaneIdempotencyRecord,
        lease_owner: str,
        lease_seconds: int = ...,
    ) -> MutationReconciliationRetryResult: ...


class DurableProviderOperationResult(NamedTuple):
    status: DurableProviderOperationStatus
    record: LaunchplaneIdempotencyRecord | None
    response_status_code: int
    response_payload: dict[str, object]
    original_trace_id: str = ""


def build_provider_operation_key(
    *,
    scope: str,
    route_path: str,
    idempotency_key: str,
    request_fingerprint: str,
    reconciliation_key: str,
) -> str:
    identity = "\x1f".join(
        (
            scope.strip(),
            route_path.strip(),
            idempotency_key.strip(),
            request_fingerprint.strip(),
            reconciliation_key.strip(),
        )
    )
    if not all(identity.split("\x1f")):
        raise ValueError("Provider operation keys require complete durable identity.")
    return f"provider-operation:{hashlib.sha256(identity.encode()).hexdigest()}"


def provider_operation_title(provider_operation_key: str) -> str:
    normalized_key = provider_operation_key.strip()
    if not normalized_key:
        raise ValueError("Provider operation titles require an operation key.")
    digest = hashlib.sha256(normalized_key.encode()).hexdigest()[:24]
    return f"Launchplane operation {digest}"


class _ProviderOperationLeaseLostError(RuntimeError):
    pass


class _ReservationHeartbeat:
    def __init__(
        self,
        *,
        store: DurableProviderOperationStore,
        reservation: LaunchplaneIdempotencyRecord,
        lease_seconds: int,
        interval_seconds: float,
    ) -> None:
        self._store = store
        self._reservation = reservation
        self._lease_seconds = lease_seconds
        self._interval_seconds = interval_seconds
        self._stop_event = Event()
        self._lock = Lock()
        self._failure_status = ""
        self._thread = Thread(
            target=self._run,
            name=f"provider-operation-heartbeat:{reservation.record_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> tuple[LaunchplaneIdempotencyRecord, str]:
        self._stop_event.set()
        self._thread.join()
        with self._lock:
            return self._reservation, self._failure_status

    def assert_current(self) -> None:
        with self._lock:
            self._renew_locked()

    def checkpoint_effect(self, phase: str) -> None:
        normalized_phase = phase.strip()
        if not normalized_phase:
            raise ValueError("Provider effect checkpoints require a phase.")
        with self._lock:
            try:
                checkpoint = self._store.checkpoint_mutation_provider_effect(
                    reservation=self._reservation,
                    effect_phase=normalized_phase,
                    lease_seconds=self._lease_seconds,
                )
            except Exception as error:
                self._failure_status = "error"
                raise _ProviderOperationLeaseLostError(
                    "Provider operation lease checkpoint failed."
                ) from error
            if checkpoint.status != "updated" or checkpoint.record is None:
                self._failure_status = checkpoint.status
                raise _ProviderOperationLeaseLostError(
                    f"Provider operation lease checkpoint failed: {checkpoint.status}."
                )
            self._reservation = checkpoint.record
            self._failure_status = ""

    def _renew_locked(self) -> None:
        try:
            renewal = self._store.renew_mutation_reservation(
                reservation=self._reservation,
                lease_seconds=self._lease_seconds,
            )
        except Exception as error:
            self._failure_status = "error"
            raise _ProviderOperationLeaseLostError(
                "Provider operation lease renewal failed."
            ) from error
        if renewal.status != "updated" or renewal.record is None:
            self._failure_status = renewal.status
            raise _ProviderOperationLeaseLostError(
                f"Provider operation lease renewal failed: {renewal.status}."
            )
        self._reservation = renewal.record
        self._failure_status = ""

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            try:
                with self._lock:
                    self._renew_locked()
            except _ProviderOperationLeaseLostError:
                with self._lock:
                    if self._failure_status != "error":
                        return
                continue


def _resolve_heartbeat_interval(
    *,
    lease_seconds: int,
    heartbeat_interval_seconds: float | None,
) -> float:
    if lease_seconds < 1:
        raise ValueError("Durable provider operation leases must be positive.")
    resolved_interval = heartbeat_interval_seconds
    if resolved_interval is None:
        resolved_interval = max(0.1, min(30.0, lease_seconds / 3))
    if resolved_interval <= 0:
        raise ValueError("Durable provider operation heartbeat intervals must be positive.")
    if resolved_interval >= lease_seconds:
        raise ValueError("Durable provider operation heartbeats must run before lease expiry.")
    return resolved_interval


def run_durable_provider_operation(
    *,
    store: DurableProviderOperationStore,
    scope: str,
    route_path: str,
    idempotency_key: str,
    request_fingerprint: str,
    lease_owner: str,
    response_trace_id: str,
    adapter: DurableProviderMutationAdapter,
    lease_seconds: int = 300,
    heartbeat_interval_seconds: float | None = None,
    target_supersession: ProviderTargetSupersession | None = None,
) -> DurableProviderOperationResult:
    resolved_heartbeat_interval = _resolve_heartbeat_interval(
        lease_seconds=lease_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
    )
    if target_supersession is not None and target_supersession.minimum_expired_seconds < 0:
        raise ValueError("Provider target supersession delays cannot be negative.")
    provider_target_key = adapter.target_key().strip()
    if not provider_target_key:
        raise ValueError("Durable provider operations require a provider target key.")
    reconciliation_key = adapter.reconciliation_key().strip()
    if not reconciliation_key:
        raise ValueError("Durable provider operations require a reconciliation key.")
    normalized_response_trace_id = response_trace_id.strip()
    if not normalized_response_trace_id:
        raise ValueError("Durable provider operations require a response trace id.")

    def reserve() -> MutationReservationResult:
        return store.reserve_mutation(
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
            reconciliation_key=reconciliation_key,
            provider_target_key=provider_target_key,
        )

    reservation_result = reserve()
    decision = reservation_result.status
    reservation = reservation_result.record

    if (
        decision == "target_busy"
        and reservation.state == "reconcile_required"
        and target_supersession is not None
        and target_supersession.quiescence_check(reservation)
    ):
        supersession = store.supersede_expired_reconciled_mutation_and_reserve(
            reservation=reservation,
            response_status_code=target_supersession.response_status_code,
            response_trace_id=normalized_response_trace_id,
            response_payload=target_supersession.response_payload,
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
            minimum_expired_seconds=target_supersession.minimum_expired_seconds,
            reconciliation_key=reconciliation_key,
            provider_target_key=provider_target_key,
        )
        if supersession.status == "acquired" and supersession.record is not None:
            decision = "acquired"
            reservation = supersession.record
        elif supersession.status == "retry":
            reservation_result = reserve()
            decision = reservation_result.status
            reservation = reservation_result.record

    if decision == "replayed":
        return _replayed_result(reservation)
    if decision == "conflict":
        return DurableProviderOperationResult("conflict", reservation, 409, {})
    if decision == "target_busy":
        return DurableProviderOperationResult("target_busy", reservation, 409, {})
    if decision == "in_progress":
        return DurableProviderOperationResult("in_progress", reservation, 409, {})
    if decision == "reconcile_required":
        return _reconcile(
            store=store,
            adapter=adapter,
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            reconciliation_key=reconciliation_key,
            response_trace_id=normalized_response_trace_id,
            fallback_record=reservation,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
            heartbeat_interval_seconds=resolved_heartbeat_interval,
        )
    if decision != "acquired":
        raise RuntimeError(f"Unsupported mutation reservation decision: {decision}")

    provider_operation_key = build_provider_operation_key(
        scope=scope,
        route_path=route_path,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        reconciliation_key=reconciliation_key,
    )

    return _apply_acquired(
        store=store,
        adapter=adapter,
        reservation=reservation,
        reconciliation_key=reconciliation_key,
        provider_operation_key=provider_operation_key,
        response_trace_id=normalized_response_trace_id,
        lease_seconds=lease_seconds,
        heartbeat_interval_seconds=resolved_heartbeat_interval,
    )


def resume_acquired_provider_operation(
    *,
    store: DurableProviderOperationStore,
    reservation: LaunchplaneIdempotencyRecord,
    response_trace_id: str,
    adapter: DurableProviderMutationAdapter,
    lease_seconds: int = 300,
    heartbeat_interval_seconds: float | None = None,
) -> DurableProviderOperationResult:
    if reservation.state != "running":
        raise ValueError("Provider operation resume requires a running reservation.")
    if not reservation.reconciliation_key:
        raise ValueError("Provider operation resume requires a reconciliation key.")
    if adapter.reconciliation_key().strip() != reservation.reconciliation_key:
        raise ValueError("Provider operation resume reconciliation identity does not match.")
    if adapter.target_key().strip() != reservation.provider_target_key:
        raise ValueError("Provider operation resume target identity does not match.")
    normalized_response_trace_id = response_trace_id.strip()
    if not normalized_response_trace_id:
        raise ValueError("Provider operation resume requires a response trace id.")
    resolved_heartbeat_interval = _resolve_heartbeat_interval(
        lease_seconds=lease_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
    )
    provider_operation_key = build_provider_operation_key(
        scope=reservation.scope,
        route_path=reservation.route_path,
        idempotency_key=reservation.idempotency_key,
        request_fingerprint=reservation.request_fingerprint,
        reconciliation_key=reservation.reconciliation_key,
    )
    return _apply_acquired(
        store=store,
        adapter=adapter,
        reservation=reservation,
        reconciliation_key=reservation.reconciliation_key,
        provider_operation_key=provider_operation_key,
        response_trace_id=normalized_response_trace_id,
        lease_seconds=lease_seconds,
        heartbeat_interval_seconds=resolved_heartbeat_interval,
        release_pre_effect_failures=False,
    )


def _apply_acquired(
    *,
    store: DurableProviderOperationStore,
    adapter: DurableProviderMutationAdapter,
    reservation: LaunchplaneIdempotencyRecord,
    reconciliation_key: str,
    provider_operation_key: str,
    response_trace_id: str,
    lease_seconds: int,
    heartbeat_interval_seconds: float,
    release_pre_effect_failures: bool = True,
) -> DurableProviderOperationResult:
    heartbeat = _ReservationHeartbeat(
        store=store,
        reservation=reservation,
        lease_seconds=lease_seconds,
        interval_seconds=heartbeat_interval_seconds,
    )
    heartbeat.start()
    try:
        outcome = adapter.apply(provider_operation_key, heartbeat)
    except _ProviderOperationLeaseLostError:
        current_reservation, _ = heartbeat.stop()
        return _mark_reconcile_required(
            store=store,
            reservation=current_reservation,
            reconciliation_key=reconciliation_key,
        )
    except ProviderMutationRejectedError as rejection:
        current_reservation, _ = heartbeat.stop()
        if current_reservation.provider_effect_started_at:
            return _mark_reconcile_required(
                store=store,
                reservation=current_reservation,
                reconciliation_key=reconciliation_key,
            )
        if not release_pre_effect_failures:
            return _mark_reconcile_required(
                store=store,
                reservation=current_reservation,
                reconciliation_key=reconciliation_key,
            )
        release = store.release_reserved_mutation(reservation=current_reservation)
        if release.status == "released":
            raise rejection.cause
        return _mark_reconcile_required(
            store=store,
            reservation=current_reservation,
            reconciliation_key=reconciliation_key,
        )
    except ProviderMutationUnknownError:
        current_reservation, _ = heartbeat.stop()
        return _mark_reconcile_required(
            store=store,
            reservation=current_reservation,
            reconciliation_key=reconciliation_key,
        )
    except BaseException:
        current_reservation, _ = heartbeat.stop()
        _mark_reconcile_required(
            store=store,
            reservation=current_reservation,
            reconciliation_key=reconciliation_key,
        )
        raise

    current_reservation, _ = heartbeat.stop()

    if outcome.provider_effect_performed and not current_reservation.provider_effect_started_at:
        return _mark_reconcile_required(
            store=store,
            reservation=current_reservation,
            reconciliation_key=reconciliation_key,
        )

    if not outcome.durable:
        if current_reservation.provider_effect_started_at:
            return _mark_reconcile_required(
                store=store,
                reservation=current_reservation,
                reconciliation_key=reconciliation_key,
            )
        if not release_pre_effect_failures:
            return _mark_reconcile_required(
                store=store,
                reservation=current_reservation,
                reconciliation_key=reconciliation_key,
            )
        release = store.release_reserved_mutation(reservation=current_reservation)
        if release.status != "released":
            return _mark_reconcile_required(
                store=store,
                reservation=current_reservation,
                reconciliation_key=reconciliation_key,
            )
        return DurableProviderOperationResult(
            "unstored",
            None,
            outcome.response_status_code,
            outcome.response_payload,
        )

    return _complete_or_adopt_outcome(
        store=store,
        reservation=current_reservation,
        response_trace_id=response_trace_id,
        outcome=outcome,
    )


def _adopt_reconciled_result(
    *,
    store: DurableProviderOperationStore,
    reservation: LaunchplaneIdempotencyRecord,
    response_status_code: int,
    response_trace_id: str,
    response_payload: dict[str, object],
) -> tuple[DurableProviderOperationResult | None, LaunchplaneIdempotencyRecord | None]:
    adoption = store.adopt_reconciled_mutation(
        reservation=reservation,
        response_status_code=response_status_code,
        response_trace_id=response_trace_id,
        response_payload=response_payload,
    )
    if adoption.status == "adopted":
        return (
            DurableProviderOperationResult(
                "adopted",
                adoption.record,
                response_status_code,
                response_payload,
            ),
            adoption.record,
        )
    if adoption.status == "replayed":
        return _replayed_result(adoption.record), adoption.record
    return None, adoption.record


def _complete_or_adopt_outcome(
    *,
    store: DurableProviderOperationStore,
    reservation: LaunchplaneIdempotencyRecord,
    response_trace_id: str,
    outcome: ProviderMutationOutcome,
) -> DurableProviderOperationResult:
    completion = complete_launchplane_mutation_reservation(
        reservation,
        response_status_code=outcome.response_status_code,
        response_trace_id=response_trace_id,
        completed_at=reservation.updated_at,
        response_payload=outcome.response_payload,
    )
    completion_result = store.complete_mutation_reservation(completion=completion)
    if completion_result.status == "completed":
        return DurableProviderOperationResult(
            "completed",
            completion_result.record,
            outcome.response_status_code,
            outcome.response_payload,
        )
    if completion_result.status == "replayed":
        return _replayed_result(completion_result.record)
    if completion_result.status == "reconcile_required":
        adoption_result, _ = _adopt_reconciled_result(
            store=store,
            reservation=reservation,
            response_status_code=outcome.response_status_code,
            response_trace_id=response_trace_id,
            response_payload=outcome.response_payload,
        )
        if adoption_result is not None:
            return adoption_result
    return DurableProviderOperationResult(
        "reconcile_required",
        completion_result.record,
        409,
        {},
    )


def _reconcile(
    *,
    store: DurableProviderOperationStore,
    adapter: DurableProviderMutationAdapter,
    scope: str,
    route_path: str,
    idempotency_key: str,
    request_fingerprint: str,
    reconciliation_key: str,
    response_trace_id: str,
    fallback_record: LaunchplaneIdempotencyRecord | None,
    lease_owner: str,
    lease_seconds: int,
    heartbeat_interval_seconds: float,
) -> DurableProviderOperationResult:
    bound_reconciliation_key = (
        fallback_record.reconciliation_key
        if fallback_record is not None and fallback_record.reconciliation_key
        else reconciliation_key
    )
    provider_operation_key = build_provider_operation_key(
        scope=scope,
        route_path=route_path,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        reconciliation_key=bound_reconciliation_key,
    )
    provider_effect_phase = (
        fallback_record.provider_effect_phase if fallback_record is not None else ""
    )
    provider_effect_started_at = (
        fallback_record.provider_effect_started_at if fallback_record is not None else ""
    )
    if isinstance(adapter, ProviderEffectTimestampObserver):
        observation = adapter.observe_with_effect_started_at(
            provider_operation_key,
            provider_effect_phase,
            bound_reconciliation_key,
            provider_effect_started_at,
        )
    else:
        observation = adapter.observe(
            provider_operation_key,
            provider_effect_phase,
            bound_reconciliation_key,
        )
    provider_effect_started = bool(
        fallback_record is not None and fallback_record.provider_effect_started_at
    )
    if observation.outcome == "absent" and (not provider_effect_started or observation.retry_safe):
        if fallback_record is None:
            return DurableProviderOperationResult(
                "reconcile_required",
                None,
                409,
                {},
            )
        retry = store.retry_reconciled_mutation(
            reservation=fallback_record,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
        )
        if retry.status == "replayed":
            return _replayed_result(retry.record)
        if retry.status == "acquired" and retry.record is not None:
            return _apply_acquired(
                store=store,
                adapter=adapter,
                reservation=retry.record,
                reconciliation_key=bound_reconciliation_key,
                provider_operation_key=provider_operation_key,
                response_trace_id=response_trace_id,
                lease_seconds=lease_seconds,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
            )
    if observation.outcome != "present":
        return DurableProviderOperationResult(
            "reconcile_required",
            fallback_record,
            409,
            {},
        )
    if fallback_record is None:
        return DurableProviderOperationResult(
            "reconcile_required",
            None,
            409,
            {},
        )
    adoption_result, adoption_record = _adopt_reconciled_result(
        store=store,
        reservation=fallback_record,
        response_status_code=observation.response_status_code,
        response_trace_id=response_trace_id,
        response_payload=observation.response_payload,
    )
    if adoption_result is not None:
        return adoption_result
    return DurableProviderOperationResult(
        "reconcile_required",
        adoption_record or fallback_record,
        409,
        {},
    )


def _mark_reconcile_required(
    *,
    store: DurableProviderOperationStore,
    reservation: LaunchplaneIdempotencyRecord,
    reconciliation_key: str,
) -> DurableProviderOperationResult:
    transition = store.mark_mutation_reconcile_required(
        reservation=reservation,
        reconciliation_key=reconciliation_key,
    )
    if transition.record is not None and transition.record.state == "completed":
        return _replayed_result(transition.record)
    if transition.status == "missing":
        raise RuntimeError("Provider operation state disappeared during reconciliation.")
    return DurableProviderOperationResult(
        "reconcile_required",
        transition.record or reservation,
        409,
        {},
    )


def _replayed_result(
    record: LaunchplaneIdempotencyRecord | None,
) -> DurableProviderOperationResult:
    if record is None:
        raise RuntimeError("Replayed mutation reservation requires a stored record.")
    return DurableProviderOperationResult(
        "replayed",
        record,
        record.response_status_code or 202,
        dict(record.response_payload),
        original_trace_id=record.response_trace_id,
    )
