from collections.abc import Callable
import json
from pathlib import Path
from typing import Any

from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.testing_rebuild import REBUILD_SCOPE
from control_plane.contracts.testing_publication import TestingPublicationRecord
from control_plane.dokploy import api, source
from control_plane.dokploy.native_copy_stream import (
    NativeSourceUnavailable,
    require_registered_remote_server,
)
from control_plane.dokploy.testing_route import (
    TestingLaneRoute,
    observe_testing_route,
    route_document,
)
from control_plane.dokploy.testing_route_execution import (
    TestingRouteExecutionIntent,
    TestingRouteWriterAttempt,
)
from control_plane.dokploy.testing_route_transport import DokployTestingRouteTransport
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_address_storage import effective_testing_route
from control_plane.testing_route_effects import TestingRouteEffects, TestingRouteFenceController
from control_plane.testing_serving_copy import published_testing_copy

COPY_ROUTE_EFFECT = "worker:odoo-stable-deploy:testing-route"


def _read_copy_route(
    *,
    root: Path,
    store: PostgresRecordStore,
    identity: RuntimeIdentity,
    publication: TestingPublicationRecord,
) -> tuple[TestingLaneRoute, DokployTestingRouteTransport, dict[str, Any], int]:
    plan = effective_testing_route(store, publication)
    candidate = publication.plan.candidate
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
    transport = DokployTestingRouteTransport(
        host=host,
        token=token,
        server_id=require_registered_remote_server(candidate.server_id),
        old_project=str(actual["appName"]),
        product=identity.product,
        context=identity.context,
        public_host=plan.host,
        public_aliases=plan.aliases,
        entrypoints=plan.public_entrypoints,
        old_runtime_identity=identity,
    )
    frame = transport.read()
    workers = frame["backend_workers"].get(plan.new_backend)
    if type(workers) is not int:
        raise ValueError("The current testing Odoo worker mode is unavailable.")
    return plan, transport, frame, workers


def _route_current(plan: TestingLaneRoute, frame: dict[str, Any], workers: int) -> bool:
    if frame["backend_workers"].get(plan.new_backend) != workers:
        raise ValueError("The current testing Odoo worker mode changed.")
    owned = json.loads(frame["owned_file"]) if frame["owned_file"] is not None else None
    if observe_testing_route(plan, frame["raw"], owned) != plan.new_backend:
        raise ValueError("The current testing route selected another copy.")
    return owned == route_document(plan, plan.new_backend, evented=bool(workers))


def testing_copy_route_current(
    *, root: Path | None, store: PostgresRecordStore, product: str, context: str
) -> bool | None:
    """Read native route drift; None leaves legacy testing delivery unchanged."""
    publication = published_testing_copy(store, product=product, context=context)
    if publication is None:
        return None
    inventory = store.read_environment_inventory(context_name=context, instance_name="testing")
    identity = inventory.runtime_identity
    if (
        root is None
        or identity is None
        or (
            identity.product,
            identity.context,
            identity.instance,
        )
        != (product, context, "testing")
    ):
        raise ValueError("The current native testing runtime identity is unavailable.")
    plan, _transport, frame, workers = _read_copy_route(
        root=root, store=store, identity=identity, publication=publication
    )
    if published_testing_copy(store, product=product, context=context) != publication:
        raise ValueError("The maintained testing publication changed.")
    return _route_current(plan, frame, workers)


def publish_testing_copy_route(
    *,
    root: Path,
    store: PostgresRecordStore,
    identity: RuntimeIdentity,
    checkpoint: Callable[[str], None],
    operation_id: str,
    lease_owner: str,
) -> None:
    if not operation_id or not lease_owner:
        raise ValueError("Testing route maintenance requires its durable service operation lease.")
    publication = published_testing_copy(store, product=identity.product, context=identity.context)
    if publication is None or identity.instance != "testing":
        raise ValueError("Route maintenance requires its applied native testing publication.")
    plan, transport, _frame, workers = _read_copy_route(
        root=root, store=store, identity=identity, publication=publication
    )

    def guard() -> None:
        if (
            published_testing_copy(store, product=identity.product, context=identity.context)
            != publication
        ):
            raise ValueError("The maintained testing publication changed.")
        checkpoint("testing_route_maintenance")

    def present() -> bool:
        return _route_current(plan, transport.read(), workers)

    prior = store.read_idempotency_record(
        scope=REBUILD_SCOPE, route_path=COPY_ROUTE_EFFECT, idempotency_key=operation_id
    )
    execution = (
        TestingRouteExecutionIntent.model_validate(
            store.read_original_mutation_intent(prior).get("route_execution")
        )
        if prior is not None
        else transport.execution_intent(plan, plan.new_backend, operation_id=operation_id)
    )
    controller = TestingRouteFenceController(
        store=store,
        transport=transport,
        execution=execution,
        guard=guard,
        checkpoint=checkpoint,
        lease_owner=lambda: lease_owner,
    )

    def publish(
        attempt: TestingRouteWriterAttempt, dispatch: Callable[[Callable[[], None]], None]
    ) -> object:
        return transport.publish(
            plan,
            plan.new_backend,
            expected_backend=plan.new_backend,
            dispatch_guard=dispatch,
            desired_runtime_identity=identity,
            execution=execution,
            execution_key=attempt.key,
            writer_attempt=attempt,
        )

    effects = TestingRouteEffects(
        controller=controller,
        phase="testing-route-maintenance",
        present=present,
        publish=publish,
        recovery_guard=guard,
        inverse=False,
    )
    effects.run(scope=REBUILD_SCOPE, route_path=COPY_ROUTE_EFFECT, key=operation_id)
    if not present():
        raise NativeSourceUnavailable("The original testing route writer requires reconciliation.")
