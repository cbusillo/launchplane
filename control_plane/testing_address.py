from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import cached_property
import json
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.provider_target_origin import ProviderTargetOrigin
from control_plane.contracts.testing_address import TestingAddressPlan, ADDRESS_SCOPE
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.dokploy import source, api
from control_plane.dokploy.native_copy_stream import (
    NativeSourceUnavailable,
    NativeSourceAuthorizationDenied,
)
from control_plane.dokploy.testing_route import observe_testing_route, route_document
from control_plane.dokploy.testing_route_transport import DokployTestingRouteTransport
from control_plane.dokploy.testing_route_execution import (
    TestingRouteWriterAttempt,
    TestingRouteExecutionIntent,
)
from control_plane.provider_operations import (
    ProviderEvidenceLease,
    ProviderMutationOutcome,
    ProviderObservation,
    ProviderOperationLease,
    ProviderMutationRejectedError,
    run_durable_provider_operation,
    provider_operation_response_payload,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_address_qualification import (
    address_transport,
    exclude_address_executors,
    qualify_address_domains,
    qualify_retained_address_domains,
)
from control_plane.testing_address_storage import (
    commit_testing_address,
    read_testing_address,
    verify_testing_address_sources,
    verify_testing_address_retained_custody,
)
from control_plane.testing_route_effects import TestingRouteEffects, TestingRouteFenceController


ROUTE = "/v1/products/{product}/testing-address"
ADDRESS_EFFECT_ROUTE = "service:testing-address:route"


class TestingAddressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    context: str = Field(min_length=1)
    instance: Literal["testing"] = "testing"
    retirement_operation_id: str = Field(min_length=1)
    reservation_operation_id: str = Field(min_length=1)
    hostname: str = Field(pattern=r"^[a-z0-9.-]+$")
    mode: Literal["dry-run", "apply", "readback"] = "dry-run"
    operation: Literal["amend", "cancel"] = "amend"
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    reason: str = Field(min_length=1)


def address_plan_result(plan: TestingAddressPlan, *, applied: bool) -> dict[str, object]:
    return {
        "plan_sha256": plan.digest,
        "product": plan.product,
        "context": plan.context,
        "instance": "testing",
        "hostname": plan.route_after.host,
        "retained_hostname": plan.route_before.host,
        "target_id": plan.before.target_id.target_id,
        "applied": applied,
        "retained_operation_fences_unchanged": True,
        "original_copy_preserved": True,
    }


class TestingAddressAdapter:
    def __init__(
        self,
        root: Path,
        store: PostgresRecordStore,
        plan: TestingAddressPlan,
        trace_id: str,
        *,
        cancel: bool = False,
    ) -> None:
        self.root, self.store, self.plan, self.trace_id = root, store, plan, trace_id
        self.cancel = cancel

    @cached_property
    def transport(self) -> DokployTestingRouteTransport:
        return address_transport(self.root, self.store, self.plan, recovery=self.cancel)

    def provider_target_origin(self) -> ProviderTargetOrigin:
        return ProviderTargetOrigin(
            product=self.plan.product,
            context=self.plan.context,
            instance="testing",
            provider_id="dokploy",
            target_type="compose",
            target_id=self.plan.before.target_id.target_id,
        )

    def target_key(self) -> str:
        return self.provider_target_origin().target_key()

    def reconciliation_key(self) -> str:
        return self.plan.digest

    def provider_guard(self, *, full_census: bool = False) -> None:
        host, token = source.read_dokploy_config(
            control_plane_root=self.root, database_url=self.store.database_url
        )
        qualify = qualify_address_domains if full_census else qualify_retained_address_domains
        try:
            digest, projects = qualify(
                host=host,
                token=token,
                hostname=self.plan.route_after.host,
                retained_ids=frozenset(str(item.target_id) for item in self.plan.reservations),
                server_id=self.plan.server_id,
            )
        except api.DokployRequestFailed as error:
            if error.status_code in {401, 403}:
                raise NativeSourceAuthorizationDenied(
                    "The existing provider identity denied the retained hostname recipient read."
                ) from error
            raise
        if digest != self.plan.domain_inventory_sha256 or projects != self.plan.retained_projects:
            raise ValueError("The hostname provider inventory changed since review.")

    def present(self) -> bool:
        frame = self.transport.read()
        owned = json.loads(frame["owned_file"]) if frame["owned_file"] is not None else None
        if owned == route_document(self.plan.route_before, self.plan.route_before.new_backend):
            return False
        return (
            observe_testing_route(self.plan.route_after, frame["raw"], owned)
            == self.plan.route_after.new_backend
        )

    def public_health(self) -> tuple[HealthcheckEvidence, ...]:
        from control_plane.testing_rebuild_service import read_public_testing_health
        from control_plane.workflows.odoo_verification import DEFAULT_ODOO_RUNTIME_HEALTH_PATH

        identity = (
            self.plan.before.inventory.runtime_identity if self.plan.before.inventory else None
        )
        if identity is None or not self.present():
            raise ValueError("The amended testing route has not converged.")
        started = datetime.now(UTC)
        health = tuple(
            read_public_testing_health(
                "https://" + host, DEFAULT_ODOO_RUNTIME_HEALTH_PATH, identity
            )
            for host in (self.plan.route_after.host, self.plan.route_before.host)
        )
        if datetime.now(UTC) - started > timedelta(minutes=2) or any(
            not item.verified or item.status != "pass" or item.runtime_identity_status != "match"
            for item in health
        ):
            raise NativeSourceUnavailable(
                "Both testing hostnames require verified TLS and the original runtime identity."
            )
        return health

    def readback(self) -> dict[str, object]:
        from control_plane.testing_serving_copy import published_testing_copy

        record = read_testing_address(self.store, self.plan.publication_key)
        if record is None or record.state != "applied" or record.plan != self.plan:
            raise ValueError("The original address amendment has no applied receipt.")
        if (
            published_testing_copy(self.store, product=self.plan.product, context=self.plan.context)
            is None
        ):
            raise ValueError("The address amendment lost its native copy custody.")
        with exclude_address_executors(self.store, tuple(self.plan.operation_sha256)):
            verify_testing_address_retained_custody(self.store, self.plan)
            self.public_health()
        return address_plan_result(self.plan, applied=True)

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        if reconciliation_key != self.plan.digest:
            return ProviderObservation("unknown")
        with exclude_address_executors(self.store, tuple(self.plan.operation_sha256)):
            return ProviderObservation("absent", retry_safe=True, preserve_effect_checkpoint=True)

    def cancel_readback(self) -> dict[str, object]:
        from control_plane.testing_address_cancellation import read_cancelled_address

        return read_cancelled_address(self)

    def cancel_original(self, lease: ProviderOperationLease) -> ProviderMutationOutcome:
        from control_plane.testing_address_cancellation import cancel_testing_address

        return cancel_testing_address(self, lease)

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        with exclude_address_executors(self.store, tuple(self.plan.operation_sha256)):
            if self.cancel:
                return self.cancel_original(lease)
            record = read_testing_address(self.store, self.plan.publication_key)
            if record is not None:
                if record.state != "applied" or record.plan != self.plan:
                    raise ValueError(
                        "The original address operation has another publication receipt."
                    )
            else:
                self.apply_original(lease)
            result = address_plan_result(self.plan, applied=True)
        return ProviderMutationOutcome(
            202,
            provider_operation_response_payload(
                trace_id=self.trace_id, records={"testing_address": self.plan.digest}, result=result
            ),
            provider_effect_performed=record is None,
        )

    def apply_original(self, operation_lease: ProviderOperationLease) -> None:
        def guard() -> None:
            operation_lease.assert_current()
            verify_testing_address_sources(self.store, self.plan)

        original_effect = self.store.read_idempotency_record(
            scope=ADDRESS_SCOPE, route_path=ADDRESS_EFFECT_ROUTE, idempotency_key=self.plan.digest
        )
        try:
            guard()
            if original_effect is None:
                self.provider_guard(full_census=True)
        except (ValueError, FileNotFoundError) as error:
            if original_effect is None:
                raise ProviderMutationRejectedError(error) from error
            raise
        execution = (
            TestingRouteExecutionIntent.model_validate(
                self.store.read_original_mutation_intent(original_effect).get("address_execution")
            )
            if original_effect is not None
            else self.transport.execution_intent(
                self.plan.route_after,
                self.plan.route_after.new_backend,
                operation_id="testing-address:" + self.plan.digest,
            )
        )
        assert isinstance(operation_lease, ProviderEvidenceLease)
        evidence_lease = cast(ProviderEvidenceLease, operation_lease)
        controller = TestingRouteFenceController(
            store=self.store,
            transport=self.transport,
            execution=execution,
            guard=guard,
            checkpoint=operation_lease.checkpoint_effect,
            lease_owner=lambda: evidence_lease.evidence_reservation().lease_owner,
        )

        def publish(
            attempt: TestingRouteWriterAttempt, dispatch: Callable[[Callable[[], None]], None]
        ) -> object:
            def checked_dispatch(send: Callable[[], None]) -> None:
                self.provider_guard()
                dispatch(send)

            assert self.plan.before.inventory is not None
            return self.transport.publish(
                self.plan.route_after,
                self.plan.route_after.new_backend,
                expected_backend=self.plan.route_after.old_backend,
                dispatch_guard=checked_dispatch,
                desired_runtime_identity=self.plan.before.inventory.runtime_identity,
                execution=attempt.execution,
                execution_key=attempt.key,
                writer_attempt=attempt,
                address_preimage=self.plan.route_before,
            )

        effects = TestingRouteEffects(
            controller=controller,
            phase="testing-address-routing",
            present=self.present,
            publish=publish,
            recovery_guard=guard,
            inverse=False,
        )
        parent = self

        class RouteEffect:
            @staticmethod
            def target_key() -> str:
                return "testing-address-route:" + parent.plan.route_after.route_id

            @staticmethod
            def reconciliation_key() -> str:
                return execution.route_digest

            @staticmethod
            def observe(
                provider_operation_key: str,
                provider_effect_phase: str,
                reconciliation_key: str,
            ) -> ProviderObservation:
                prior = parent.store.read_idempotency_record(
                    scope=ADDRESS_SCOPE,
                    route_path=ADDRESS_EFFECT_ROUTE,
                    idempotency_key=parent.plan.digest,
                )
                if prior is None or reconciliation_key != execution.route_digest:
                    return ProviderObservation("unknown")
                return effects.observe(provider_effect_phase, prior.attempt)

            @staticmethod
            def apply(
                provider_operation_key: str, lease: ProviderOperationLease
            ) -> ProviderMutationOutcome:
                return effects.apply(lease)

        result = run_durable_provider_operation(
            store=self.store,
            scope=ADDRESS_SCOPE,
            route_path=ADDRESS_EFFECT_ROUTE,
            idempotency_key=self.plan.digest,
            request_fingerprint=execution.route_digest,
            lease_owner=evidence_lease.evidence_reservation().lease_owner,
            response_trace_id=self.trace_id,
            adapter=RouteEffect(),
            original_intent={"address_execution": execution.model_dump(mode="json")},
        )
        payload = controller.completed(result)
        if payload.get("route_write_not_started") or not self.present():
            raise NativeSourceUnavailable("The original address writer needs reconciliation.")
        health = self.public_health()
        guard()
        commit_testing_address(
            self.store, self.plan, evidence_lease.evidence_reservation(), datetime.now(UTC), health
        )
