"""Resolve one held event testing deploy without exposing its key or request."""

from collections.abc import Callable
from dataclasses import dataclass

from control_plane.contracts.generic_web_deploy_recovery import (
    build_generic_web_deploy_recovery_digest,
)
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.drivers.generic_web_dispatch import GenericWebDeployEnvelope
from control_plane.event_testing_deploy import event_testing_deploy_request
from control_plane.generic_web_deploy_http import GENERIC_WEB_DEPLOY_ROUTE
from control_plane.product_reconcile import RECONCILE_SOURCE, reconcile_reservation_scope
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_deploy_provider import (
    decode_generic_web_provider_reconciliation_target,
)


@dataclass(frozen=True)
class EventDeployRecoveryCoordinates:
    reservation: LaunchplaneIdempotencyRecord
    original_deploy: GenericWebDeployEnvelope
    context: str

    @property
    def reference(self) -> str:
        return "event-deploy-" + build_generic_web_deploy_recovery_digest(
            {
                "reservation": self.reservation.model_dump(mode="json"),
                "original_deploy": self.original_deploy.model_dump(mode="json"),
            }
        )


def resolve_event_deploy_recovery_coordinates(
    *,
    store: PostgresRecordStore,
    product: str,
    context: str,
    fingerprint: Callable[..., str],
) -> EventDeployRecoveryCoordinates:
    candidates = []
    for record in store.list_held_provider_target_reservations():
        if (
            record.route_path != GENERIC_WEB_DEPLOY_ROUTE
            or record.scope != reconcile_reservation_scope(product)
        ):
            continue
        snapshot = decode_generic_web_provider_reconciliation_target(record.reconciliation_key)
        if (
            snapshot.product != product
            or snapshot.instance != "testing"
            or snapshot.context != context
        ):
            raise ValueError("Event deploy lane evidence changed.")
        if not record.idempotency_key.startswith(
            f"{RECONCILE_SOURCE}:{product}:{context}:testing:"
        ):
            raise ValueError("Event deploy key does not match its lane.")
        candidates.append(record)
    if not candidates:
        raise FileNotFoundError("No held event deploy.")
    if len(candidates) != 1:
        raise ValueError("Held event deploy is ambiguous.")
    reservation = candidates[0]
    lookup = store.lookup_existing_mutation_reservation(
        route_path=reservation.route_path,
        idempotency_key=reservation.idempotency_key,
        request_fingerprint=reservation.request_fingerprint,
    )
    if lookup.status != "found" or lookup.record != reservation:
        raise ValueError("Held event deploy reservation is not exact.")
    matches: dict[str, GenericWebDeployEnvelope] = {}
    snapshot = decode_generic_web_provider_reconciliation_target(reservation.reconciliation_key)
    if snapshot.original_event_deploy is not None:
        envelope = GenericWebDeployEnvelope.model_validate(snapshot.original_event_deploy)
        if (
            envelope.product != product
            or envelope.deploy.instance != "testing"
            or fingerprint(
                route_path=GENERIC_WEB_DEPLOY_ROUTE, payload=envelope.model_dump(mode="json")
            )
            != reservation.request_fingerprint
            or fingerprint(
                route_path=GENERIC_WEB_DEPLOY_ROUTE, payload=snapshot.original_event_deploy
            )
            != reservation.request_fingerprint
        ):
            raise ValueError("Stored event deploy request changed.")
        matches[envelope.model_dump_json()] = envelope
    try:
        plan = store.read_product_reconcile_request(f"{product}:testing").last_plan
    except FileNotFoundError:
        plan = {}
    image_reference, source_commit = plan.get("desired_artifact_id"), plan.get("desired_commit")
    if (
        isinstance(image_reference, str)
        and image_reference
        and isinstance(source_commit, str)
        and source_commit
    ):
        envelope = event_testing_deploy_request(
            product=product, image_reference=image_reference, source_commit=source_commit
        )
        if (
            fingerprint(
                route_path=GENERIC_WEB_DEPLOY_ROUTE, payload=envelope.model_dump(mode="json")
            )
            == reservation.request_fingerprint
        ):
            matches[envelope.model_dump_json()] = envelope
    if len(matches) != 1:
        raise ValueError("Original event deploy evidence is absent or ambiguous.")
    return EventDeployRecoveryCoordinates(reservation, next(iter(matches.values())), context)
