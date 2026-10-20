"""Durable physical attempts for the testing lane's bounded execution fence."""

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
from typing import Any
from uuid import uuid4

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.testing_rebuild import REBUILD_ROUTE, REBUILD_SCOPE
from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable
from control_plane.dokploy.testing_route_execution import (
    TestingRouteExecutionCustody,
    TestingRouteExecutionIntent,
    TestingRouteFence,
    TestingRouteInitializer,
    TestingRouteLockIntent,
    TestingRouteWriterAttempt,
)
from control_plane.dokploy.testing_route_transport import (
    DokployTestingRouteTransport,
    TestingRouteWriteNotStarted,
)
from control_plane.provider_operations import (
    DurableProviderOperationResult,
    ProviderMutationOutcome,
    ProviderObservation,
    ProviderOperationLease,
    ProviderEvidenceLease,
    run_durable_provider_operation,
)
from control_plane.storage.postgres import PostgresRecordStore

ROUTE_LOCK_RECEIPT = REBUILD_ROUTE + ":route-lock"
ROUTE_INITIALIZER_RECEIPT = REBUILD_ROUTE + ":route-initializer"
ROUTE_WRITER_RECEIPT = REBUILD_ROUTE + ":route-writer"
ROUTE_PREPARING = "testing-route-preparing"
ROUTE_WRITING = "testing-route-writing"


@dataclass(frozen=True)
class TestingRouteFenceController:
    store: PostgresRecordStore
    transport: DokployTestingRouteTransport
    execution: TestingRouteExecutionIntent
    guard: Callable[[], None]
    checkpoint: Callable[[str], None]
    lease_owner: Callable[[], str]

    def dispatch(self, lease: ProviderOperationLease, phase: str, send: Callable[[], None]) -> None:
        self.guard()
        lease.assert_current()
        self.checkpoint(phase)
        lease.checkpoint_effect(phase)
        send()

    def lock(self, custody: TestingRouteExecutionCustody) -> str:
        """Creation-only allocation may repeat; a completed lock never changes."""
        proposed = TestingRouteLockIntent(
            server_id=self.execution.server_id,
            route_id=self.execution.route_id,
            custody=custody,
        )
        prior = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE, route_path=ROUTE_LOCK_RECEIPT, idempotency_key=proposed.key
        )
        if prior is not None:
            original = self.store.read_original_mutation_intent(prior)
            intent = TestingRouteLockIntent.model_validate(original.get("route_lock"))
            if intent.key != proposed.key:
                raise ValueError("The original permanent lock belongs to another route root.")
        else:
            intent = proposed
            state = self.transport.read_execution_fence(custody)
            if state["status"] not in {"missing", "partial"}:
                if (
                    state["status"] not in {"ready", "rebound"}
                    or self.known_initializer(
                        TestingRouteFence.model_validate(state["fence"])
                    ).custody.boot_id
                    == custody.boot_id
                ):
                    raise NativeSourceUnavailable(
                        "An unrecorded permanent route lock is not adopted."
                    )
            # No resource receipt exists for this directory in this boot. An
            # empty lock cannot have authorized an initializer or ordinary
            # writer here. Bind its actual FD before minting any nonce. A
            # same-boot historical resource follows the prior-receipt path
            # above and still refuses a lost/replaced lock.
        parent = self

        class Lock:
            def target_key(self) -> str:
                return "testing-route-lock:" + intent.key

            def reconciliation_key(self) -> str:
                return intent.custody.directory_sha256

            def observe(
                self,
                provider_operation_key: str,
                provider_effect_phase: str,
                reconciliation_key: str,
            ) -> ProviderObservation:
                parent.guard()
                try:
                    state = parent.transport.read_execution_fence(custody)
                except (NativeSourceUnavailable, ValueError):
                    return ProviderObservation("unknown")
                if state["status"] == "missing":
                    # A late allocation only creates an empty lock. It cannot
                    # mint a generation or authorize any ordinary writer.
                    return ProviderObservation("absent", retry_safe=True)
                if state["status"] not in {"partial", "ready", "rebound"}:
                    return ProviderObservation("unknown")
                return ProviderObservation(
                    "present", 200, {"lock_identity": state["lock_identity"]}
                )

            def apply(
                self, provider_operation_key: str, lease: ProviderOperationLease
            ) -> ProviderMutationOutcome:
                identity = parent.transport.allocate_execution_lock(
                    custody,
                    dispatch_guard=lambda send: parent.dispatch(lease, "route-lock", send),
                )
                return ProviderMutationOutcome(200, {"lock_identity": identity})

        self.guard()
        result = run_durable_provider_operation(
            store=self.store,
            scope=REBUILD_SCOPE,
            route_path=ROUTE_LOCK_RECEIPT,
            idempotency_key=intent.key,
            request_fingerprint=intent.key,
            lease_owner=self.lease_owner(),
            response_trace_id=self.execution.operation_id,
            adapter=Lock(),
            original_intent={"route_lock": intent.model_dump(mode="json")},
        )
        payload = self.completed(result)
        identity = str(payload["lock_identity"])
        if self.transport.observe_execution_lock(custody) != identity:
            raise NativeSourceUnavailable("The original permanent route lock was lost or replaced.")
        return identity

    def ensure(self, custody: TestingRouteExecutionCustody) -> TestingRouteFence:
        """Only a positively observed, single-invocation nonce can be used."""
        identity = self.lock(custody)
        state = self.transport.read_execution_fence(custody)
        if state["status"] in {"ready", "rebound"}:
            current = TestingRouteFence.model_validate(state["fence"])
            initializer = self.known_initializer(current)
            if initializer.custody.boot_id != custody.boot_id:
                return self.initialize(
                    TestingRouteInitializer(
                        operation_id=self.execution.operation_id,
                        server_id=self.execution.server_id,
                        route_id=self.execution.route_id,
                        custody=custody,
                        generation=hashlib.sha256(uuid4().bytes).hexdigest(),
                        lock_identity=identity,
                        previous=current,
                        retired_boot_id=initializer.custody.boot_id,
                    ),
                    allow_mutation=True,
                )
            if current.lock_identity != identity:
                raise NativeSourceUnavailable("The route fence changed its original lock.")
            if (
                initializer.custody.directory_sha256 != custody.directory_sha256
                or initializer.lock_identity != identity
            ):
                raise ValueError(
                    "The current route generation belongs to another original attempt."
                )
            self.initialize(initializer, allow_mutation=False)
            return self.transport.observe_initializer(initializer)
        if state["status"] != "partial" or state["lock_identity"] != identity:
            raise NativeSourceUnavailable("The original route fence requires observation.")
        # Every actual generation invocation has a different durable key. A
        # timed-out call is never resent with a nonce that ordinary writers use.
        initializer = TestingRouteInitializer(
            operation_id=self.execution.operation_id,
            server_id=self.execution.server_id,
            route_id=self.execution.route_id,
            custody=custody,
            generation=hashlib.sha256(uuid4().bytes).hexdigest(),
            lock_identity=identity,
        )
        return self.initialize(initializer, allow_mutation=True)

    def known_initializer(self, fence: TestingRouteFence) -> TestingRouteInitializer:
        prior = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=ROUTE_INITIALIZER_RECEIPT,
            idempotency_key=fence.generation,
        )
        if prior is None:
            raise NativeSourceUnavailable("The current route generation has no original receipt.")
        original = self.store.read_original_mutation_intent(prior)
        initializer = TestingRouteInitializer.model_validate(original.get("route_initializer"))
        if (
            initializer.server_id,
            initializer.route_id,
            initializer.generation,
            initializer.lock_identity,
        ) != (
            self.execution.server_id,
            self.execution.route_id,
            fence.generation,
            fence.lock_identity,
        ):
            raise ValueError("The current route generation belongs to another original attempt.")
        return initializer

    def initialize(
        self, initializer: TestingRouteInitializer, *, allow_mutation: bool
    ) -> TestingRouteFence:
        parent = self

        class Initializer:
            def target_key(self) -> str:
                return "testing-route-initializer:" + initializer.generation

            def reconciliation_key(self) -> str:
                return canonical_json_sha256(initializer.model_dump(mode="json"))

            def observe(
                self,
                provider_operation_key: str,
                provider_effect_phase: str,
                reconciliation_key: str,
            ) -> ProviderObservation:
                parent.guard()
                try:
                    fence = parent.transport.observe_initializer(initializer)
                except (NativeSourceUnavailable, ValueError):
                    return ProviderObservation("unknown")
                return ProviderObservation("present", 200, {"fence": fence.model_dump(mode="json")})

            def apply(
                self, provider_operation_key: str, lease: ProviderOperationLease
            ) -> ProviderMutationOutcome:
                try:
                    fence = parent.transport.initialize_execution(
                        initializer,
                        dispatch_guard=lambda send: parent.dispatch(
                            lease, "route-initialize", send
                        ),
                    )
                except TestingRouteWriteNotStarted:
                    return ProviderMutationOutcome(
                        200, {"initializer_not_started": True}, provider_effect_performed=False
                    )
                return ProviderMutationOutcome(200, {"fence": fence.model_dump(mode="json")})

        prior = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=ROUTE_INITIALIZER_RECEIPT,
            idempotency_key=initializer.generation,
        )
        self.guard()
        result = run_durable_provider_operation(
            store=self.store,
            scope=REBUILD_SCOPE,
            route_path=ROUTE_INITIALIZER_RECEIPT,
            idempotency_key=initializer.generation,
            request_fingerprint=canonical_json_sha256(initializer.model_dump(mode="json")),
            lease_owner=self.lease_owner(),
            response_trace_id=self.execution.operation_id,
            adapter=Initializer(),
            original_intent={"route_initializer": initializer.model_dump(mode="json")},
            allow_mutation=allow_mutation and prior is None,
        )
        payload = self.completed(result)
        if payload.get("initializer_not_started") is True:
            raise NativeSourceUnavailable(
                "The route initializer returned before creating its nonce."
            )
        fence = TestingRouteFence.model_validate(payload["fence"])
        if (
            fence.generation != initializer.generation
            or fence.lock_identity != initializer.lock_identity
        ):
            raise ValueError(
                "The original route initializer receipt changed its generation or lock."
            )
        return fence

    @staticmethod
    def completed(result: DurableProviderOperationResult) -> dict[str, Any]:
        if result.status not in {"completed", "adopted", "replayed"}:
            raise NativeSourceUnavailable("The original route child requires observation.")
        return result.response_payload


@dataclass(frozen=True)
class TestingRouteEffects:
    """One logical switch; immutable physical custody for every actual attempt."""

    controller: TestingRouteFenceController
    phase: str
    present: Callable[[], bool]
    publish: Callable[[TestingRouteWriterAttempt, Callable[[Callable[[], None]], None]], object]
    recovery_guard: Callable[[], None]
    inverse: bool

    def writer_key(self, attempt_number: int) -> str:
        return self.controller.execution.key(attempt=attempt_number)

    def original_writer(self, attempt_number: int) -> TestingRouteWriterAttempt | None:
        receipt = self.controller.store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=ROUTE_WRITER_RECEIPT,
            idempotency_key=self.writer_key(attempt_number),
        )
        if receipt is None:
            return None
        original = self.controller.store.read_original_mutation_intent(receipt)
        attempt = TestingRouteWriterAttempt.model_validate(original.get("route_writer"))
        if attempt.execution != self.controller.execution or attempt.key != self.writer_key(
            attempt_number
        ):
            raise ValueError("The physical route attempt changed its original intent.")
        return attempt

    def settle(self, attempt: TestingRouteWriterAttempt) -> str:
        def dispatch(send: Callable[[], None]) -> None:
            self.recovery_guard()
            self.controller.checkpoint("route-settle")
            send()

        return self.controller.transport.settle_attempt(attempt, dispatch_guard=dispatch)

    def writer_was_busy(self, attempt_number: int) -> bool:
        attempt = self.original_writer(attempt_number)
        if attempt is None:
            return False
        receipt = self.controller.store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=ROUTE_WRITER_RECEIPT,
            idempotency_key=attempt.key,
        )
        return (
            receipt is not None
            and receipt.state == "completed"
            and receipt.response_payload.get("writer_outcome") == "busy"
        )

    def observe(self, effect_phase: str, attempt_number: int) -> ProviderObservation:
        if effect_phase == ROUTE_WRITING:
            attempt = self.original_writer(attempt_number)
            if attempt is None:
                return ProviderObservation("unknown")
            if self.writer_was_busy(attempt_number):
                self.recovery_guard()
                # A returned busy result never entered the writer's critical
                # section. No old call remains to seal: retry under a fresh
                # lease/attempt after rechecking current admission and custody.
                return (
                    ProviderObservation("present", 200)
                    if self.present()
                    else ProviderObservation("absent", retry_safe=True)
                )
            receipt = self.controller.store.read_idempotency_record(
                scope=REBUILD_SCOPE,
                route_path=ROUTE_WRITER_RECEIPT,
                idempotency_key=attempt.key,
            )
            self.recovery_guard()
            outcome = (
                "excluded"
                if receipt is not None
                and receipt.state == "completed"
                and receipt.response_payload.get("writer_outcome") == "excluded"
                else self.settle(attempt)
            )
            if outcome in {"busy", "unknown"}:
                return ProviderObservation("unknown")
            if self.present():
                return ProviderObservation("present", 200)
            if outcome in {"excluded", "retired"}:
                return (
                    ProviderObservation("absent", retry_safe=True)
                    if self.inverse
                    else ProviderObservation("present", 200, {"route_write_not_started": True})
                )
            return ProviderObservation("unknown")
        if effect_phase not in {"", ROUTE_PREPARING}:
            # Older execution formats have no new-protocol dispatch proof.
            return ProviderObservation("unknown")
        if self.present():
            return ProviderObservation("present", 200)
        # A stale preparer cannot move its parent lease to ROUTE_WRITING after
        # this CAS advances the attempt. Its initializer cannot publish a route.
        return ProviderObservation("absent", retry_safe=True)

    def observe_for_abort(self, effect_phase: str, attempt_number: int) -> ProviderObservation:
        """Seal only the original forward attempt; never prepare or resend it."""
        if self.inverse:
            raise ValueError("Abort settlement belongs only to the original forward effect.")
        self.recovery_guard()
        attempt = self.original_writer(attempt_number)
        if attempt is None:
            if effect_phase not in {"", ROUTE_PREPARING}:
                return ProviderObservation("unknown")
            outcome = "excluded"
        elif self.writer_was_busy(attempt_number):
            # This exact call returned without entering its critical section.
            outcome = "excluded"
        else:
            outcome = self.settle(attempt)
        if outcome not in {"applied", "excluded", "retired"}:
            return ProviderObservation("unknown")
        return ProviderObservation(
            "present",
            200,
            {
                "abort_writer_outcome": outcome,
                **({"route_write_not_started": True} if outcome == "excluded" else {}),
            },
        )

    def apply(self, lease: ProviderOperationLease) -> ProviderMutationOutcome:
        controller = self.controller
        controller.guard()
        lease.assert_current()
        if self.present():
            return ProviderMutationOutcome(200, provider_effect_performed=False)
        lease.checkpoint_effect(ROUTE_PREPARING)
        assert isinstance(lease, ProviderEvidenceLease)
        attempt_number = lease.evidence_reservation().attempt
        key = self.writer_key(attempt_number)
        attempt = self.original_writer(attempt_number)
        if attempt is None:
            frame = controller.transport.read()
            custody = TestingRouteExecutionCustody.model_validate(frame["execution_custody"])
            fence = controller.ensure(custody)
            frame = controller.transport.read()
            if (
                TestingRouteExecutionCustody.model_validate(
                    frame["execution_custody"]
                ).physical_sha256
                != custody.physical_sha256
            ):
                raise NativeSourceUnavailable("The route root changed while preparing its writer.")
            attempt = TestingRouteWriterAttempt(
                execution=controller.execution,
                custody=custody,
                fence=fence,
                expected_file_sha256=frame["file_sha256"],
                key=key,
            )
        parent = self
        writer_attempt = attempt

        class Writer:
            def target_key(self) -> str:
                return "testing-route-writer:" + key

            def reconciliation_key(self) -> str:
                return canonical_json_sha256(writer_attempt.model_dump(mode="json"))

            def observe(
                self,
                provider_operation_key: str,
                provider_effect_phase: str,
                reconciliation_key: str,
            ) -> ProviderObservation:
                outcome = parent.settle(writer_attempt)
                if outcome not in {"applied", "excluded", "retired"}:
                    return ProviderObservation("unknown")
                return ProviderObservation("present", 200, {"writer_outcome": outcome})

            def apply(
                self, provider_operation_key: str, writer_lease: ProviderOperationLease
            ) -> ProviderMutationOutcome:
                def dispatch(send: Callable[[], None]) -> None:
                    controller.guard()
                    lease.assert_current()
                    writer_lease.assert_current()
                    controller.checkpoint(parent.phase)
                    lease.checkpoint_effect(ROUTE_WRITING)
                    writer_lease.checkpoint_effect(ROUTE_WRITING)
                    send()

                try:
                    outcome = parent.publish(writer_attempt, dispatch)
                except TestingRouteWriteNotStarted:
                    outcome = "excluded"
                return ProviderMutationOutcome(200, {"writer_outcome": outcome})

        # The original physical intent is durable before Writer.apply can send.
        assert isinstance(lease, ProviderEvidenceLease)
        result = run_durable_provider_operation(
            store=controller.store,
            scope=REBUILD_SCOPE,
            route_path=ROUTE_WRITER_RECEIPT,
            idempotency_key=key,
            request_fingerprint=canonical_json_sha256(attempt.model_dump(mode="json")),
            lease_owner=lease.evidence_reservation().lease_owner,
            response_trace_id=controller.execution.operation_id,
            adapter=Writer(),
            original_intent={"route_writer": attempt.model_dump(mode="json")},
        )
        payload = controller.completed(result)
        outcome = payload.get("writer_outcome")
        if outcome in {"excluded", "retired", "superseded"}:
            if self.inverse:
                raise NativeSourceUnavailable("The excluded inverse may retry on its current root.")
            return ProviderMutationOutcome(200, {"route_write_not_started": True})
        if outcome != "applied" or not self.present():
            raise NativeSourceUnavailable("The original route writer requires observation.")
        return ProviderMutationOutcome(200)
