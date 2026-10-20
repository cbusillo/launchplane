"""Repair only a fresh testing generation's recorded certificate intent."""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
from typing import Literal

import click
from pydantic import BaseModel, ConfigDict

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.dokploy import api as dokploy_api
from control_plane.durable_operation_authorization import DurableOperationAuthorizationDeniedError
from control_plane.generic_web_testing_exclusion import _target_reservations
from control_plane.odoo_testing_restore import FreshOdooTestingGeneration, generation_binding
from control_plane.provider_operations import (
    _ProviderOperationLeaseLostError,
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
)
from control_plane.testing_event_deploy_recovery import testing_event_execution_lock_name
from control_plane.testing_initial_generation import require_testing_generation_usage
from control_plane.testing_replacement_bootstrap import (
    TestingReplacementBootstrapAdapter,
    bootstrap_request_sha256,
)
from control_plane.workflows.generic_web_deploy_provider import (
    dokploy_generic_web_provider_target_key,
)


class TestingBootstrapRouteRepairPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    instance: Literal["testing"] = "testing"
    operation_id: str
    generation_binding_sha256: str
    certificate_type: Literal["none", "letsencrypt"]
    plan_sha256: str
    applied: bool = False
    route_verified: bool = False
    generation_preserved: Literal[True] = True
    copy_preserved: Literal[True] = True
    queue_preserved: Literal[True] = True
    runtime_verified: Literal[False] = False


@dataclass
class TestingBootstrapRouteRepair:
    bootstrap: TestingReplacementBootstrapAdapter

    def target_key(self) -> str:
        return self.bootstrap.target_key()

    def reconciliation_key(self) -> str:
        # Immutable across provider progress and later same-key receipt replay.
        return "route-repair:" + canonical_json_sha256(
            {
                "bootstrap": self.bootstrap.reconciliation_key(),
                "review": self.bootstrap.request.reviewed_plan_sha256,
            }
        )

    @contextmanager
    def locked(self) -> Iterator[Callable[[], None]]:
        base = self.bootstrap
        store, request = base.store, base.request
        provider = store.read_provider_target_record(
            context_name=request.context, instance_name="testing"
        )
        target_key = dokploy_generic_web_provider_target_key(provider)
        with ExitStack() as stack:
            original = stack.enter_context(
                store.testing_replacement_execution_lock(request.operation_id)
            )
            event = stack.enter_context(
                store.testing_replacement_execution_lock(
                    testing_event_execution_lock_name(target_key)
                )
            )
            if original is None or event is None:
                raise ValueError("The original testing executor is active.")
            lane = stack.enter_context(
                store.testing_verification_lane_guard(product=base.product, context=request.context)
            )

            def check() -> None:
                original()
                event()
                lane()
                current = store.read_provider_target_record(
                    context_name=request.context, instance_name="testing"
                )
                if dokploy_generic_web_provider_target_key(current) != target_key:
                    raise ValueError("The locked testing target changed.")
                if _target_reservations(
                    store,
                    product=base.product,
                    context=request.context,
                    target_key=target_key,
                    target_id=provider.target_id,
                ):
                    raise ValueError(
                        "A provider operation still holds testing; preserve its unknown effects."
                    )

            check()
            yield check
            check()

    def snapshot(
        self,
    ) -> tuple[FreshOdooTestingGeneration, dokploy_api.JsonObject, TestingBootstrapRouteRepairPlan]:
        base = self.bootstrap
        with base.store.serialize_disposable_testing_resources():
            generation = base.readback_generation()
        if generation.completed_at or generation.non_use_invalidated:
            raise ValueError(
                "Route repair requires the original unfinished, unused testing generation."
            )
        target = base.store.read_dokploy_target_record(
            context_name=generation.context, instance_name="testing"
        )
        target_id = base.store.read_dokploy_target_id_record(
            context_name=generation.context, instance_name="testing"
        )
        require_testing_generation_usage(
            base.root, base.store, base.product, generation.context, target, target_id
        )
        # Permit certificate drift only. All fields the provider writer would touch
        # must still describe this exact existing generation route.
        from control_plane.dokploy import source
        from control_plane.dokploy_target_setup_http import (
            fetch_dokploy_compose_domains_for_target_setup,
        )

        host, token = source.read_dokploy_config(control_plane_root=base.root)
        routes = fetch_dokploy_compose_domains_for_target_setup(
            host=host, token=token, compose_id=generation.target_id
        )
        if len(routes) != 1 or routes[0].get("certificateType") not in {"none", "letsencrypt"}:
            raise ValueError("Certificate repair requires one existing testing route.")
        route = dict(routes[0])
        observed = base._domains(
            generation.model_copy(update={"certificate_type": route["certificateType"]})
        )
        if (
            observed != routes
            or any(
                route.get(key) is not None
                for key in ("applicationId", "previewDeploymentId", "customCertResolver")
            )
            or route.get("domainType") != "compose"
        ):
            raise ValueError(
                "Other provider route fields changed; do not repair them as certificate drift."
            )
        digest = canonical_json_sha256(
            {
                "generation": generation.model_dump(mode="json"),
                "route": route,
                "request": base.request.model_dump(
                    mode="json", exclude={"mode", "reviewed_plan_sha256"}
                ),
            }
        )
        plan = TestingBootstrapRouteRepairPlan(
            product=base.product,
            context=generation.context,
            operation_id=generation.operation_id,
            generation_binding_sha256=generation_binding(generation),
            certificate_type=generation.certificate_type,
            plan_sha256=digest,
        )
        return generation, route, plan

    def plan(self) -> dict[str, object]:
        with self.locked():
            generation, route, plan = self.snapshot()
            if route["certificateType"] == generation.certificate_type:
                raise ValueError("The certificate already agrees; use original-bootstrap readback.")
            return plan.model_dump(mode="json")

    def readback(self) -> dict[str, object]:
        with self.locked():
            _generation, _route, plan = self.snapshot()
            self.bootstrap.readback()
            return plan.model_copy(update={"applied": True, "route_verified": True}).model_dump(
                mode="json"
            )

    def _response(self, plan: TestingBootstrapRouteRepairPlan) -> dict[str, object]:
        return provider_operation_response_payload(
            trace_id=self.bootstrap.trace_id,
            records={"operation": plan.operation_id},
            result=plan.model_copy(update={"applied": True, "route_verified": True}).model_dump(
                mode="json"
            ),
        )

    @staticmethod
    def effect_binding(generation: FreshOdooTestingGeneration, route: Mapping[str, object]) -> str:
        return "repair-certificate:" + canonical_json_sha256(
            {
                "generation": generation_binding(generation),
                "route": {
                    key: route.get(key)
                    for key in (
                        "domainId",
                        "host",
                        "path",
                        "internalPath",
                        "port",
                        "https",
                        "applicationId",
                        "customCertResolver",
                        "composeId",
                        "serviceName",
                        "domainType",
                        "previewDeploymentId",
                        "stripPath",
                    )
                },
            }
        )

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        if not provider_effect_phase:
            # The runner also checks that no effect timestamp was committed.
            return ProviderObservation("absent")
        try:
            base = self.bootstrap
            generation = base.store.read_fresh_odoo_testing_generation(
                base.product, base.request.context
            )
            if generation is None or (
                generation.operation_id != base.request.operation_id
                or generation.domain_host != base.request.domain_host
                or generation.request_sha256 != bootstrap_request_sha256(base.request)
                or base.store.read_dokploy_target_id_record(
                    context_name=base.request.context, instance_name="testing"
                ).target_id
                != generation.target_id
            ):
                return ProviderObservation("unknown")
            # Adoption proves the dispatched route effect. Later use or copy
            # completion is not permission to write and does not erase proof.
            routes = base._domains(generation)
            if len(routes) != 1 or provider_effect_phase != self.effect_binding(
                generation, routes[0]
            ):
                return ProviderObservation("unknown")
            plan = TestingBootstrapRouteRepairPlan(
                product=base.product,
                context=generation.context,
                operation_id=generation.operation_id,
                generation_binding_sha256=generation_binding(generation),
                certificate_type=generation.certificate_type,
                plan_sha256=base.request.reviewed_plan_sha256,
            )
            return ProviderObservation("present", response_payload=self._response(plan))
        except DurableOperationAuthorizationDeniedError:
            raise
        except (ValueError, FileNotFoundError, RuntimeError, OSError, click.ClickException):
            return ProviderObservation("unknown")

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        dispatched = False
        try:
            with self.locked() as check:
                generation, route, plan = self.snapshot()
                if plan.plan_sha256 != self.bootstrap.request.reviewed_plan_sha256:
                    raise ValueError("The reviewed generation or provider route changed.")
                if route["certificateType"] == generation.certificate_type:
                    raise ValueError("The certificate already agrees; preserve its current route.")
                lease.assert_current()
                check()
                from control_plane.dokploy import source

                host, token = source.read_dokploy_config(control_plane_root=self.bootstrap.root)
                # Update the reviewed existing row only. Never fall through to
                # a create operation if it disappears between provider reads.
                payload = {
                    key: route.get(key)
                    for key in (
                        "domainId",
                        "host",
                        "path",
                        "internalPath",
                        "port",
                        "https",
                        "applicationId",
                        "customCertResolver",
                        "composeId",
                        "serviceName",
                        "domainType",
                        "previewDeploymentId",
                        "stripPath",
                    )
                }
                payload["certificateType"] = generation.certificate_type
                with self.bootstrap.store.serialize_disposable_testing_resources():
                    if self.bootstrap.readback_generation() != generation:
                        raise ValueError("The qualified generation changed before repair.")
                lease.checkpoint_effect(self.effect_binding(generation, route))
                dispatched = True
                dokploy_api.dokploy_request(
                    host=host,
                    token=token,
                    path="/api/domain.update",
                    method="POST",
                    payload=payload,
                )
                lease.assert_current()
                check()
                self.bootstrap.readback()
                if self.bootstrap.readback_generation() != generation:
                    raise ValueError("Original generation or route identity changed during repair.")
                return ProviderMutationOutcome(202, self._response(plan))
        except _ProviderOperationLeaseLostError:
            raise
        except Exception as error:
            if not dispatched:
                # Preserve authorization refusal types so the HTTP route still
                # reports 403, while releasing this known effect-free reservation.
                raise ProviderMutationRejectedError(error) from error
            raise
