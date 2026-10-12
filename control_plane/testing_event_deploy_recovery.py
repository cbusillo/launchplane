"""Retire a service testing attempt only after original inputs and live effects are absent."""

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
import logging

import click
from sqlalchemy.exc import SQLAlchemyError

from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    parse_launchplane_mutation_timestamp,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.generic_web_deploy_http import GENERIC_WEB_DEPLOY_ROUTE
from control_plane.provider_operations import build_provider_operation_key, provider_operation_title
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebDeployNoLiveEffectProvider,
    GenericWebDeployProvider,
    TestingEventDeployRecoveryRefused,
    decode_generic_web_provider_reconciliation_target,
)
from control_plane.workflows.generic_web_application_snapshot import (
    has_recorded_application_lifecycle,
    generic_web_provider_deployment_record_id,
)
from control_plane.workflows.odoo_generic_web_post_deploy import (
    generic_web_post_deploy_executor_for_driver_id,
)


def testing_event_execution_lock_name(target_key: str) -> str:
    return f"event-deploy:{target_key}"


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
    record_refusal: Callable[[str], None] = lambda _: None,
) -> dict[str, object] | None:
    def decline(code: str) -> dict[str, object] | None:
        logging.getLogger(__name__).info(
            "Testing event recovery refused for %s: %s", reservation.record_id, code
        )
        record_refusal(code)
        return None

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
        or any(
            lane.context == context
            and lane.instance == "testing"
            and lane.generic_web_extensions.configured
            for lane in profile.lanes
        )
    ):
        return decline("recovery_scope_or_effect_unsupported")
    try:
        snapshot = decode_generic_web_provider_reconciliation_target(reservation.reconciliation_key)
        if (snapshot.product, snapshot.context, snapshot.instance) != (
            profile.product,
            context,
            "testing",
        ):
            return decline("recovery_lane_changed")
        lookup = store.lookup_existing_mutation_reservation(
            route_path=reservation.route_path,
            idempotency_key=reservation.idempotency_key,
            request_fingerprint=reservation.request_fingerprint,
        )
        if lookup.status != "found" or lookup.record != reservation:
            return decline("recovery_reservation_changed")
        if not reservation.lease_expires_at or parse_launchplane_mutation_timestamp(
            reservation.lease_expires_at, field_name="lease_expires_at"
        ) >= parse_launchplane_mutation_timestamp(lookup.observed_at, field_name="observed_at"):
            return decline("recovery_lease_active")
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
            return decline("recovery_original_available")
        bindings = (
            profile,
            store.read_dokploy_target_record(context_name=context, instance_name="testing"),
            store.read_dokploy_target_id_record(context_name=context, instance_name="testing"),
            store.read_provider_target_record(context_name=context, instance_name="testing"),
        )
        if (
            bindings[2].target_id != snapshot.target_id
            or bindings[3].target_id != snapshot.target_id
            or bindings[3].provider_id != snapshot.provider_id
        ):
            return decline("recovery_target_changed")
        check_execution_lock()
        inspected_at = datetime.now(timezone.utc).isoformat()
        operation_key = build_provider_operation_key(
            scope=reservation.scope,
            route_path=reservation.route_path,
            idempotency_key=reservation.idempotency_key,
            request_fingerprint=reservation.request_fingerprint,
            reconciliation_key=reservation.reconciliation_key,
        )
        if has_recorded_application_lifecycle(
            store,
            context=context,
            instance="testing",
            deployment_record_id=generic_web_provider_deployment_record_id(
                operation_key, context=context, instance="testing"
            ),
        ):
            return decline("recovery_application_lifecycle_required")
        evidence = provider.observe_testing_no_live_effect(
            control_plane_root=root,
            target=snapshot,
            operation_title=provider_operation_title(operation_key),
            provider_effect_phase=reservation.provider_effect_phase,
        )
        if evidence is None:
            return decline("recovery_provider_unproven")
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
        return (
            audit
            if result.status == "adopted"
            else decline("recovery_reservation_or_binding_changed")
        )
    except TestingEventDeployRecoveryRefused as error:
        return decline(error.code)
    except (RuntimeError, SQLAlchemyError):
        return decline("recovery_execution_lock_or_storage_unavailable")
    except (FileNotFoundError, ValueError, click.ClickException):
        return decline("recovery_evidence_unavailable_or_changed")
