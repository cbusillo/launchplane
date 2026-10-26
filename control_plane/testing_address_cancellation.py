from __future__ import annotations

from collections.abc import Callable
import json
from typing import TYPE_CHECKING

from control_plane.contracts.testing_address import ADDRESS_SCOPE
from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable
from control_plane.dokploy.testing_route import observe_testing_route, route_document
from control_plane.dokploy.testing_route_execution import (
    TestingRouteExecutionIntent,
    TestingRouteWriterAttempt,
)
from control_plane.provider_operations import (
    ProviderEvidenceLease,
    ProviderMutationOutcome,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
    run_durable_provider_operation,
)
from control_plane.testing_address_storage import (
    settled_testing_address,
    read_testing_address_recovery_copy,
)
from control_plane.testing_route_effects import TestingRouteEffects, TestingRouteFenceController

if TYPE_CHECKING:
    from control_plane.testing_address import TestingAddressAdapter

INVERSE_ROUTE = "service:testing-address:inverse"


def address_preimage_present(adapter: TestingAddressAdapter) -> bool:
    frame = adapter.transport.read()
    owned = json.loads(frame["owned_file"]) if frame["owned_file"] is not None else None
    before = adapter.plan.route_before
    if owned == route_document(adapter.plan.route_after, before.new_backend):
        return False
    return observe_testing_route(before, frame["raw"], owned) == before.new_backend


def cancelled_address_result(adapter: TestingAddressAdapter) -> dict[str, object]:
    from control_plane.testing_address_preparation import pending_address_preparations

    return {
        "plan_sha256": adapter.plan.digest,
        "product": adapter.plan.product,
        "context": adapter.plan.context,
        "instance": "testing",
        "applied": False,
        "cancelled": True,
        "preimage_restored": True,
        "retained_preparation_fences_preserved": pending_address_preparations(adapter),
    }


def read_cancelled_address(adapter: TestingAddressAdapter) -> dict[str, object]:
    read_testing_address_recovery_copy(adapter.store, adapter.plan)
    if settled_testing_address(
        adapter.store, adapter.plan
    ) is not None or not address_preimage_present(adapter):
        raise ValueError("The original testing address preimage has not been restored.")
    return cancelled_address_result(adapter)


def cancel_testing_address(
    adapter: TestingAddressAdapter, lease: ProviderOperationLease
) -> ProviderMutationOutcome:
    from control_plane.testing_address import ADDRESS_EFFECT_ROUTE

    assert isinstance(lease, ProviderEvidenceLease)
    if settled_testing_address(adapter.store, adapter.plan) is not None:
        raise ValueError(
            "Canonical metadata already committed; retrieve the original amend result."
        )

    def guard() -> None:
        lease.assert_current()
        read_testing_address_recovery_copy(adapter.store, adapter.plan)

    guard()
    original = adapter.store.read_idempotency_record(
        scope=ADDRESS_SCOPE, route_path=ADDRESS_EFFECT_ROUTE, idempotency_key=adapter.plan.digest
    )
    if original is not None:
        forward_original = original
        forward = TestingRouteExecutionIntent.model_validate(
            adapter.store.read_original_mutation_intent(original).get("address_execution")
        )
        controller = TestingRouteFenceController(
            store=adapter.store,
            transport=adapter.transport,
            execution=forward,
            guard=guard,
            checkpoint=lease.checkpoint_effect,
            lease_owner=lambda: lease.evidence_reservation().lease_owner,
        )

        def never_publish(_attempt: object, _dispatch: object) -> object:
            raise NativeSourceUnavailable("Cancellation cannot send another forward writer.")

        effects = TestingRouteEffects(
            controller=controller,
            phase="testing-address-cancel-settle",
            present=lambda: False,
            publish=never_publish,
            recovery_guard=guard,
            inverse=False,
        )

        class SettleForward:
            @staticmethod
            def target_key() -> str:
                return "testing-address-route:" + adapter.plan.route_after.route_id

            @staticmethod
            def reconciliation_key() -> str:
                return forward.route_digest

            @staticmethod
            def observe(
                provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
            ) -> ProviderObservation:
                return effects.observe_for_abort(provider_effect_phase, forward_original.attempt)

            @staticmethod
            def apply(
                provider_operation_key: str, lease: ProviderOperationLease
            ) -> ProviderMutationOutcome:
                raise NativeSourceUnavailable(
                    "The original forward must settle before cancellation."
                )

        settled = run_durable_provider_operation(
            store=adapter.store,
            scope=ADDRESS_SCOPE,
            route_path=ADDRESS_EFFECT_ROUTE,
            idempotency_key=adapter.plan.digest,
            request_fingerprint=forward.route_digest,
            lease_owner=lease.evidence_reservation().lease_owner,
            response_trace_id=adapter.trace_id,
            adapter=SettleForward(),
            original_intent={"address_execution": forward.model_dump(mode="json")},
        )
        controller.completed(settled)
        observation = effects.observe_for_abort(original.provider_effect_phase, original.attempt)
        if observation.outcome != "present":
            raise NativeSourceUnavailable("The original forward writer is not excluded.")
    elif not address_preimage_present(adapter):
        raise NativeSourceUnavailable("The address operation has no original forward custody.")

    performed = not address_preimage_present(adapter)
    restore_address_preimage(adapter, lease, guard)
    guard()
    result = read_cancelled_address(adapter)
    return ProviderMutationOutcome(
        202,
        provider_operation_response_payload(
            trace_id=adapter.trace_id,
            records={"testing_address": adapter.plan.digest},
            result=result,
        ),
        provider_effect_performed=performed,
    )


def restore_address_preimage(
    adapter: TestingAddressAdapter, lease: ProviderOperationLease, guard: Callable[[], None]
) -> None:
    assert isinstance(lease, ProviderEvidenceLease)
    original = adapter.store.read_idempotency_record(
        scope=ADDRESS_SCOPE, route_path=INVERSE_ROUTE, idempotency_key=adapter.plan.digest
    )
    execution = (
        TestingRouteExecutionIntent.model_validate(
            adapter.store.read_original_mutation_intent(original).get("address_execution")
        )
        if original is not None
        else adapter.transport.execution_intent(
            adapter.plan.route_after,
            adapter.plan.route_after.new_backend,
            operation_id="testing-address-cancel:" + adapter.plan.digest,
            address_preimage=adapter.plan.route_before,
            address_inverse=True,
        )
    )
    controller = TestingRouteFenceController(
        store=adapter.store,
        transport=adapter.transport,
        execution=execution,
        guard=guard,
        checkpoint=lease.checkpoint_effect,
        lease_owner=lambda: lease.evidence_reservation().lease_owner,
    )

    def publish(
        attempt: TestingRouteWriterAttempt, dispatch: Callable[[Callable[[], None]], None]
    ) -> object:
        return adapter.transport.publish(
            adapter.plan.route_after,
            adapter.plan.route_after.new_backend,
            expected_backend=adapter.plan.route_after.new_backend,
            dispatch_guard=dispatch,
            desired_runtime_identity=adapter.transport.old_runtime_identity,
            execution=attempt.execution,
            execution_key=attempt.key,
            writer_attempt=attempt,
            address_preimage=adapter.plan.route_before,
            address_inverse=True,
        )

    effects = TestingRouteEffects(
        controller=controller,
        phase="testing-address-inverse",
        present=lambda: address_preimage_present(adapter),
        publish=publish,
        recovery_guard=guard,
        inverse=True,
    )

    class Inverse:
        @staticmethod
        def target_key() -> str:
            return "testing-address-inverse:" + adapter.plan.route_after.route_id

        @staticmethod
        def reconciliation_key() -> str:
            return execution.route_digest

        @staticmethod
        def observe(
            provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
        ) -> ProviderObservation:
            current = adapter.store.read_idempotency_record(
                scope=ADDRESS_SCOPE, route_path=INVERSE_ROUTE, idempotency_key=adapter.plan.digest
            )
            return (
                effects.observe(provider_effect_phase, current.attempt)
                if current
                else ProviderObservation("unknown")
            )

        @staticmethod
        def apply(
            provider_operation_key: str, lease: ProviderOperationLease
        ) -> ProviderMutationOutcome:
            return effects.apply(lease)

    result = run_durable_provider_operation(
        store=adapter.store,
        scope=ADDRESS_SCOPE,
        route_path=INVERSE_ROUTE,
        idempotency_key=adapter.plan.digest,
        request_fingerprint=execution.route_digest,
        lease_owner=lease.evidence_reservation().lease_owner,
        response_trace_id=adapter.trace_id,
        adapter=Inverse(),
        original_intent={"address_execution": execution.model_dump(mode="json")},
    )
    controller.completed(result)
