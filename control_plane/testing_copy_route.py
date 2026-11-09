from collections.abc import Callable
from datetime import UTC, datetime
import json
from pathlib import Path
from uuid import uuid4

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.testing_rebuild import REBUILD_SCOPE
from control_plane.dokploy import api, source
from control_plane.dokploy.native_copy_stream import (
    require_registered_remote_server,
    NativeSourceUnavailable,
)
from control_plane.dokploy.testing_route import (
    TestingLaneRoute,
    maintain_testing_route,
    observe_testing_route,
)
from control_plane.dokploy.testing_route_execution import (
    TestingRouteExecutionIntent,
    TestingRouteWriterAttempt,
)
from control_plane.dokploy.testing_route_transport import DokployTestingRouteTransport
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderObservation,
    ProviderOperationLease,
    run_durable_provider_operation,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_address_storage import effective_testing_route
from control_plane.testing_route_effects import TestingRouteEffects, TestingRouteFenceController
from control_plane.testing_serving_copy import published_testing_copy

COPY_ROUTE_EFFECT = "worker:odoo-stable-deploy:testing-route"


class TestingCopyRoute:
    def __init__(
        self,
        *,
        root: Path,
        store: PostgresRecordStore,
        identity: RuntimeIdentity,
        checkpoint: Callable[[str], None],
    ) -> None:
        if identity.instance != "testing" or not identity.product:
            raise ValueError("Route maintenance requires the current product testing identity.")
        self.root, self.store, self.identity, self.checkpoint = root, store, identity, checkpoint
        self.key = identity.deployment_record_id
        self.lease_owner = uuid4().hex
        self.publication = published_testing_copy(
            store, product=identity.product, context=identity.context
        )
        if self.publication is None:
            raise ValueError("Testing route maintenance requires its applied native publication.")
        self.before = effective_testing_route(store, self.publication)
        candidate = self.publication.plan.candidate
        host, token = source.read_dokploy_config(
            control_plane_root=root, database_url=store.database_url
        )
        actual = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=candidate.compose_id
        )
        if (actual.get("composeId"), actual.get("name"), actual.get("serverId")) != (
            candidate.compose_id,
            candidate.namespace,
            candidate.server_id,
        ) or not actual.get("appName"):
            raise ValueError("The maintained testing target changed.")
        self.transport = DokployTestingRouteTransport(
            host=host,
            token=token,
            server_id=require_registered_remote_server(candidate.server_id),
            old_project=str(actual["appName"]),
            product=identity.product,
            context=identity.context,
            public_host=self.before.host,
            public_aliases=self.before.aliases,
            entrypoints=self.before.public_entrypoints,
            old_runtime_identity=identity,
        )
        frame = self.transport.read()
        workers = frame.get("old_workers")
        if not isinstance(workers, str) or not workers.isdigit():
            raise ValueError("The current testing Odoo worker mode is unavailable.")
        self.after = maintain_testing_route(self.before, workers=int(workers))

    def guard(self) -> None:
        if (
            published_testing_copy(
                self.store, product=self.identity.product, context=self.identity.context
            )
            != self.publication
        ):
            raise ValueError("The maintained testing publication changed.")
        self.checkpoint("testing_route_maintenance")

    def present(self, *, inverse: bool) -> bool:
        frame = self.transport.read()
        workers = frame.get("old_workers")
        if not inverse and (
            not isinstance(workers, str)
            or not workers.isdigit()
            or bool(int(workers))
            != (self.after.route_id + "-websocket" in self.after.template["http"]["services"])
        ):
            raise ValueError("The current testing Odoo worker mode changed.")
        owned = json.loads(frame["owned_file"]) if frame["owned_file"] is not None else None
        desired, other = (self.before, self.after) if inverse else (self.after, self.before)
        try:
            return observe_testing_route(desired, frame["raw"], owned) == desired.new_backend
        except ValueError:
            if observe_testing_route(other, frame["raw"], owned) != other.new_backend:
                raise ValueError("The current testing route selected another copy.") from None
            return False

    def run(self, *, inverse: bool = False) -> str:
        route_path = COPY_ROUTE_EFFECT + (":inverse" if inverse else "")
        prior = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE, route_path=route_path, idempotency_key=self.key
        )
        if prior is not None:
            original = self.store.read_original_mutation_intent(prior)
            before = TestingLaneRoute.model_validate(original.get("before"))
            after = TestingLaneRoute.model_validate(original.get("after"))
            if after != self.after or before.new_backend != self.before.new_backend:
                raise ValueError("The original deployment route intent changed.")
            self.before, self.after = before, after
            execution = TestingRouteExecutionIntent.model_validate(original.get("execution"))
        else:
            self.guard()
            if inverse:
                forward = self.store.read_idempotency_record(
                    scope=REBUILD_SCOPE, route_path=COPY_ROUTE_EFFECT, idempotency_key=self.key
                )
                if forward is None:
                    raise ValueError("The inverse requires its original forward route receipt.")
                original = self.store.read_original_mutation_intent(forward)
                self.before = TestingLaneRoute.model_validate(original.get("before"))
                if TestingLaneRoute.model_validate(original.get("after")) != self.after:
                    raise ValueError("The inverse crossed its original deployment route.")
            execution = self.transport.execution_intent(
                self.after,
                self.after.new_backend,
                operation_id=self.key,
                route_preimage=self.before,
                inverse=inverse,
            )
        controller = TestingRouteFenceController(
            store=self.store,
            transport=self.transport,
            execution=execution,
            guard=self.guard,
            checkpoint=self.checkpoint,
            lease_owner=lambda: self.lease_owner,
        )

        def publish(
            attempt: TestingRouteWriterAttempt, dispatch: Callable[[Callable[[], None]], None]
        ) -> object:
            return self.transport.publish(
                self.after,
                self.after.new_backend,
                expected_backend=self.before.new_backend,
                dispatch_guard=dispatch,
                desired_runtime_identity=self.identity,
                execution=execution,
                execution_key=attempt.key,
                writer_attempt=attempt,
                route_preimage=self.before,
                inverse=inverse,
            )

        effects = TestingRouteEffects(
            controller=controller,
            phase="testing-route-maintenance",
            present=lambda: self.present(inverse=inverse),
            publish=publish,
            recovery_guard=self.guard,
            inverse=inverse,
        )
        parent = self

        class Route:
            def target_key(self) -> str:
                return "testing-route:" + parent.after.route_id

            def reconciliation_key(self) -> str:
                return execution.document_sha256

            def observe(
                self,
                provider_operation_key: str,
                provider_effect_phase: str,
                reconciliation_key: str,
            ) -> ProviderObservation:
                if reconciliation_key != execution.document_sha256 or prior is None:
                    return ProviderObservation("unknown")
                result = effects.observe(provider_effect_phase, prior.attempt)
                if result.outcome == "present":
                    return ProviderObservation(
                        "present",
                        200,
                        {
                            "route": (parent.before if inverse else parent.after).model_dump(
                                mode="json"
                            ),
                            "observed_at": datetime.now(UTC).isoformat(),
                        },
                    )
                return result

            def apply(
                self, provider_operation_key: str, lease: ProviderOperationLease
            ) -> ProviderMutationOutcome:
                result = effects.apply(lease)
                if not parent.present(inverse=inverse):
                    raise NativeSourceUnavailable(
                        "The original deployment route writer requires reconciliation."
                    )
                return ProviderMutationOutcome(
                    200,
                    {
                        "route": (parent.before if inverse else parent.after).model_dump(
                            mode="json"
                        ),
                        "observed_at": datetime.now(UTC).isoformat(),
                    },
                    provider_effect_performed=result.provider_effect_performed,
                )

        result = run_durable_provider_operation(
            store=self.store,
            scope=REBUILD_SCOPE,
            route_path=route_path,
            idempotency_key=self.key,
            request_fingerprint=canonical_json_sha256(execution.model_dump(mode="json")),
            lease_owner=self.lease_owner,
            response_trace_id=self.key,
            adapter=Route(),
            original_intent={
                "before": self.before.model_dump(mode="json"),
                "after": self.after.model_dump(mode="json"),
                "execution": execution.model_dump(mode="json"),
            },
        )
        controller.completed(result)
        if not self.present(inverse=inverse):
            raise NativeSourceUnavailable("The original deployment route is not applied.")
        return self.key
