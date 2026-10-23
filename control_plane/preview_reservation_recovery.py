"""Original preview intent and recovery, independent of mutable desired state."""

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from typing import Callable

from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    parse_launchplane_mutation_timestamp,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.odoo_preview_apply_http import (
    ODOO_PREVIEW_APPLY_ROUTE,
    OdooPreviewApplyEnvelope,
    validate_odoo_preview_issued_plan,
    validate_odoo_preview_profile_authority,
    odoo_preview_supersession_is_quiescent,
)
from control_plane.odoo_preview_apply_execution import (
    ObserveOdooPreviewApply,
    preview_execution_lock_name,
    prepare_odoo_preview_apply_operation,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_preview import (
    effective_preview_app_name_prefix,
    preview_application_name,
    preview_pr_number_from_slug,
    preview_slug_from_application_name,
)
from control_plane.workflows.odoo_preview_runtime import (
    ODOO_PREVIEW_SUPERSESSION_GRACE_SECONDS,
    OdooPreviewApplyInputsResult,
)


@dataclass(frozen=True)
class OriginalPreviewIntent:
    issued_plan: OdooPreviewApplyInputsResult
    apply_request: OdooPreviewApplyEnvelope


def original_preview_intent(
    reservation: LaunchplaneIdempotencyRecord,
    *,
    profile: LaunchplaneProductProfileRecord,
    store: object,
) -> OriginalPreviewIntent:
    """Accept only reservation-owned issued intent and its exact target binding."""
    if not isinstance(store, PostgresRecordStore):
        raise ValueError("Preview original intent requires database storage.")
    intent = store.read_original_mutation_intent(reservation)
    return validate_original_preview_intent(reservation, profile=profile, intent=intent)


def validate_original_preview_intent(
    reservation: LaunchplaneIdempotencyRecord,
    *,
    profile: LaunchplaneProductProfileRecord,
    intent: dict[str, object],
) -> OriginalPreviewIntent:
    """Validate already-read evidence without masking storage failures at the caller."""
    if reservation.route_path != ODOO_PREVIEW_APPLY_ROUTE or not intent:
        raise ValueError("preview_original_intent_unavailable")
    issued = OdooPreviewApplyInputsResult.model_validate(intent.get("issued_plan"))
    request = validate_odoo_preview_issued_plan(
        plan_id=reservation.idempotency_key,
        issued_plan=issued,
        request=OdooPreviewApplyEnvelope.model_validate(intent.get("apply_request")),
    )
    validate_odoo_preview_profile_authority(
        profile=profile, issued_plan=issued, observation_only=True
    )
    if intent.get("product") != profile.product:
        raise ValueError("preview_original_binding_changed")
    key = f"dokploy:compose:{issued.context.strip()}:{request.apply.dry_run_plan.compose_name.strip()}"
    target = "dokploy-provider-target:" + hashlib.sha256(key.encode()).hexdigest()
    if (key, target) != (reservation.reconciliation_key, reservation.provider_target_key):
        raise ValueError("preview_original_binding_changed")
    return OriginalPreviewIntent(issued, request)


def preview_reservation_reference(
    reservation: LaunchplaneIdempotencyRecord, retained_intent: dict[str, object]
) -> str:
    snapshot = json.dumps(
        {"reservation": reservation.model_dump(mode="json"), "original_intent": retained_intent},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "preview-reservation-" + hashlib.sha256(snapshot.encode()).hexdigest()


def classify_preview_reservation(
    reservation: LaunchplaneIdempotencyRecord,
    *,
    profile: LaunchplaneProductProfileRecord,
    store: object,
) -> dict[str, object]:
    """Database-only projection: never return coordinates, keys, plans or request bodies."""
    if not isinstance(store, PostgresRecordStore):
        raise ValueError("Preview classification requires database storage.")
    retained_intent = store.read_original_mutation_intent(reservation)
    available = False
    action = "unavailable"
    number = 0
    try:
        intent = original_preview_intent(reservation, profile=profile, store=store)
        available = True
        action = intent.issued_plan.operation
        number = intent.issued_plan.plan_request.pr_number
    except ValueError:
        pass
    # Current target attribution can stop new planning; it cannot restore the
    # missing original action, plan, attempt or schedule binding for settlement.
    target_number = 0
    prefix = f"dokploy:compose:{profile.preview.context.strip()}:"
    key = reservation.reconciliation_key
    if key.startswith(prefix) and profile.preview.context.strip():
        name = key.removeprefix(prefix)
        app_prefix = effective_preview_app_name_prefix(profile=profile)
        slug = preview_slug_from_application_name(app_name_prefix=app_prefix, application_name=name)
        candidate = preview_pr_number_from_slug(
            preview_slug=slug, slug_template=profile.preview.slug_template
        )
        if (
            candidate is not None
            and candidate > 0
            and profile.preview.slug_template.replace("{number}", str(candidate)) == slug
            and preview_application_name(app_name_prefix=app_prefix, preview_slug=slug) == name
            and reservation.provider_target_key
            == "dokploy-provider-target:" + hashlib.sha256(key.encode()).hexdigest()
        ):
            target_number = candidate
    return {
        "reservation_id": reservation.record_id,
        "recovery_reference": preview_reservation_reference(reservation, retained_intent),
        "state": reservation.state,
        "attempt": reservation.attempt,
        "original_plan_available": bool(retained_intent.get("issued_plan")),
        "original_action_available": available,
        "original_attempt_available": available,
        "original_binding_available": available,
        "original_schedule_binding_available": bool(retained_intent.get("schedule_binding")),
        "action": action,
        "pull_request_number": number,
        "target_pull_request_number": target_number,
        "classification": "original_available" if available else "original_evidence_unavailable",
        "next_action": "observe_original" if available else "retain_fence_missing_original_proof",
    }


def held_preview_reservations(
    store: object, *, product: str, limit: int = 101
) -> tuple[LaunchplaneIdempotencyRecord, ...]:
    if not isinstance(store, PostgresRecordStore):
        return ()
    return store.list_held_odoo_preview_reservations(product=product, limit=limit)


def recover_held_preview(
    *,
    store: PostgresRecordStore,
    reservation: LaunchplaneIdempotencyRecord,
    profile: LaunchplaneProductProfileRecord,
    root: Path,
    observe: ObserveOdooPreviewApply,
    quiescence: Callable[..., bool] = odoo_preview_supersession_is_quiescent,
) -> dict[str, object]:
    retained_intent = store.read_original_mutation_intent(reservation)
    result = classify_preview_reservation(reservation, profile=profile, store=store)

    def refuse(code: str) -> dict[str, object]:
        return {**result, "settlement": "refused", "reason": code}

    try:
        intent = original_preview_intent(reservation, profile=profile, store=store)
        with store.testing_replacement_execution_lock(
            preview_execution_lock_name(reservation.provider_target_key)
        ) as check:
            if check is None:
                return refuse("preview_executor_active")
            check()
            lookup = store.lookup_existing_mutation_reservation(
                route_path=reservation.route_path,
                idempotency_key=reservation.idempotency_key,
                request_fingerprint=reservation.request_fingerprint,
            )
            if lookup.status != "found" or lookup.record != reservation:
                return refuse("preview_reservation_changed")
            if not reservation.lease_expires_at or (
                parse_launchplane_mutation_timestamp(
                    reservation.lease_expires_at, field_name="lease_expires_at"
                )
                + timedelta(seconds=ODOO_PREVIEW_SUPERSESSION_GRACE_SECONDS)
                > parse_launchplane_mutation_timestamp(lookup.observed_at, field_name="observed_at")
            ):
                return refuse("preview_grace_active")
            if not quiescence(
                control_plane_root_path=root,
                request=intent.apply_request,
                database_url=store.database_url,
                schedule_binding=retained_intent.get("schedule_binding"),
            ):
                return refuse("preview_quiescence_unproven")
            check()
            # Do not let a provider read adopt a snapshot that changed during the probe.
            current = store.read_idempotency_record(
                scope=reservation.scope,
                route_path=reservation.route_path,
                idempotency_key=reservation.idempotency_key,
            )
            if (
                current != reservation
                or store.read_product_profile_record(profile.product) != profile
            ):
                return refuse("preview_reservation_changed")

            def observe_exact(**kwargs: object) -> tuple[str, dict[str, object] | None, bool]:
                before = store.read_idempotency_record(
                    scope=reservation.scope,
                    route_path=reservation.route_path,
                    idempotency_key=reservation.idempotency_key,
                )
                if before is None or before.model_dump(
                    exclude={"state", "updated_at"}
                ) != reservation.model_dump(exclude={"state", "updated_at"}):
                    raise ValueError("preview_reservation_changed")
                observed = observe(**kwargs)
                check()
                after = store.read_idempotency_record(
                    scope=reservation.scope,
                    route_path=reservation.route_path,
                    idempotency_key=reservation.idempotency_key,
                )
                if (
                    after != before
                    or store.read_original_mutation_intent(reservation) != retained_intent
                    or store.read_product_profile_record(profile.product) != profile
                ):
                    raise ValueError("preview_reservation_changed")
                return observed

            operation = prepare_odoo_preview_apply_operation(
                control_plane_root=root,
                record_store=store,
                profile=profile,
                apply_request=intent.apply_request,
                issued_plan=intent.issued_plan,
                reservation_scope=reservation.scope,
                idempotency_key=reservation.idempotency_key,
                request_fingerprint=reservation.request_fingerprint,
                trace_id="preview-recovery-" + reservation.record_id,
                execute_apply=lambda **_: (_ for _ in ()).throw(
                    AssertionError("Observation cannot dispatch preview effects.")
                ),
                observe_apply=observe_exact,
            )
            binding = retained_intent.get("schedule_binding")
            if isinstance(binding, dict):
                operation.adapter.schedule_binding = binding
            outcome = operation.run_excluded(store, allow_mutation=False, check=check)
            driver_result = outcome.response_payload.get("result")
            return {
                **result,
                "settlement": outcome.status,
                "result_status": driver_result.get("status", "")
                if isinstance(driver_result, dict)
                else "",
            }
    except (ValueError, OSError, RuntimeError):
        return refuse("preview_original_or_provider_proof_unavailable")
