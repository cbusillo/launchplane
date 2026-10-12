"""Retire a service testing attempt only after original inputs and live effects are absent."""

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import click

from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    parse_launchplane_mutation_timestamp,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.generic_web_deploy_http import GENERIC_WEB_DEPLOY_ROUTE
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebDeployNoLiveEffectProvider,
    GenericWebDeployProvider,
    decode_generic_web_provider_reconciliation_target,
)
from control_plane.workflows.odoo_generic_web_post_deploy import (
    generic_web_post_deploy_executor_for_driver_id,
)


def retire_unavailable_testing_event_deploy(
    *,
    store: object,
    reservation: LaunchplaneIdempotencyRecord,
    profile: LaunchplaneProductProfileRecord,
    context: str,
    current_target_key: str,
    provider: GenericWebDeployProvider,
    root: Path,
    trace_id: str,
    check_execution_lock: Callable[[], None],
) -> dict[str, object] | None:
    # Imported here because coordinate resolution also serves the HTTP path and
    # reads the reconciler's original request builder.
    from control_plane.event_deploy_recovery_reference import (
        OriginalEventDeployEvidenceUnavailable,
        resolve_event_deploy_recovery_coordinates,
    )
    from control_plane.http_app import idempotency_request_fingerprint
    from control_plane.product_reconcile import RECONCILE_SOURCE, reconcile_reservation_scope

    if (
        not isinstance(store, PostgresRecordStore)
        or not isinstance(provider, GenericWebDeployNoLiveEffectProvider)
        or reservation.scope != reconcile_reservation_scope(profile.product)
        or reservation.route_path != GENERIC_WEB_DEPLOY_ROUTE
        or not reservation.idempotency_key.startswith(
            f"{RECONCILE_SOURCE}:{profile.product}:{context}:testing:"
        )
        or reservation.provider_target_key != current_target_key
        or reservation.provider_effect_phase not in {"", "target_update", "deploy_trigger"}
        or generic_web_post_deploy_executor_for_driver_id(profile.driver_id) is not None
    ):
        return None
    try:
        snapshot = decode_generic_web_provider_reconciliation_target(reservation.reconciliation_key)
        if (snapshot.product, snapshot.context, snapshot.instance) != (
            profile.product,
            context,
            "testing",
        ):
            return None
        lookup = store.lookup_existing_mutation_reservation(
            route_path=reservation.route_path,
            idempotency_key=reservation.idempotency_key,
            request_fingerprint=reservation.request_fingerprint,
        )
        if lookup.status != "found" or lookup.record != reservation:
            return None
        if not reservation.lease_expires_at or parse_launchplane_mutation_timestamp(
            reservation.lease_expires_at, field_name="lease_expires_at"
        ) >= parse_launchplane_mutation_timestamp(lookup.observed_at, field_name="observed_at"):
            return None
        try:
            resolve_event_deploy_recovery_coordinates(
                store=store,
                product=profile.product,
                context=context,
                fingerprint=idempotency_request_fingerprint,
            )
        except OriginalEventDeployEvidenceUnavailable:
            pass
        else:
            # Existing exact recovery owns recoverable attempts.
            return None
        bindings = (
            profile,
            store.read_dokploy_target_record(context_name=context, instance_name="testing"),
            store.read_dokploy_target_id_record(context_name=context, instance_name="testing"),
            store.read_provider_target_record(context_name=context, instance_name="testing"),
        )
        if bindings[2].target_id != snapshot.target_id:
            return None
        check_execution_lock()
        inspected_at = datetime.now(timezone.utc).isoformat()
        evidence = provider.observe_testing_no_live_effect(control_plane_root=root, target=snapshot)
        if evidence is None:
            return None
        check_execution_lock()
        audit: dict[str, object] = {
            "action": "retire_unavailable_testing_event_deploy",
            "reservation_id": reservation.record_id,
            "attempt": reservation.attempt,
            "original_evidence": "unavailable",
            "provider_inspected_at": inspected_at,
            "provider_evidence": evidence,
        }
        result = store.adopt_reconciled_mutation(
            reservation=reservation,
            response_status_code=409,
            response_trace_id=trace_id,
            response_payload={
                "status": "failed",
                "trace_id": trace_id,
                "error": {"code": "testing_deploy_retired_without_live_effect"},
                "recovery": audit,
            },
            expected_binding_evidence=bindings,
        )
        return audit if result.status == "adopted" else None
    except (FileNotFoundError, ValueError, click.ClickException):
        return None
