"""Original preview intent and recovery, independent of mutable desired state."""

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import click
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
from control_plane.workflows.launchplane import find_preview_record


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


def interrupted_preview_restore(
    *,
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    reservation: LaunchplaneIdempotencyRecord,
    root: Path,
) -> bool:
    from control_plane.dokploy import api, source
    from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied
    from control_plane.odoo_preview_resources import preview_resource_environment
    from control_plane.preview_filestore import (
        PreviewRestoreDisposalRequired,
        prepare_preview_filestore,
    )
    from control_plane.workflows.odoo_preview_runtime import resolve_original_preview_compose_id

    if not profile.preview.clean_testing_snapshot:
        return False
    try:
        original = original_preview_intent(reservation, profile=profile, store=store)
    except ValueError:
        return False
    plan = original.issued_plan.dry_run_plan
    if original.issued_plan.operation != "refresh" or plan.clean_snapshot is None:
        return False
    try:
        compose_id = resolve_original_preview_compose_id(
            control_plane_root=root,
            request=original.apply_request.apply,
            database_url=store.database_url,
        )
        if not compose_id:
            return False
        host, token = source.read_dokploy_config(
            control_plane_root=root, database_url=store.database_url
        )
        target = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=compose_id
        )
        environment = preview_resource_environment(plan.compose_name)
        prepare_preview_filestore(
            host=host,
            token=token,
            server_id=str(target.get("serverId") or ""),
            project=str(target.get("appName") or ""),
            data_volume=environment["ODOO_DATA_VOLUME"],
            database_volume=environment["ODOO_DB_VOLUME"],
            logs_volume=environment["ODOO_LOG_VOLUME"],
            database_name=environment["ODOO_DB_NAME"],
            snapshot=plan.clean_snapshot,
            apply=False,
            dispatch_guard=lambda dispatch: dispatch(),
        )
    except PreviewRestoreDisposalRequired:
        return True
    except NativeSourceAuthorizationDenied:
        raise
    except (OSError, ValueError, click.ClickException):
        return False
    return False


def require_interrupted_preview_disposal(
    store: PostgresRecordStore,
    *,
    profile: LaunchplaneProductProfileRecord,
    number: int,
    original_reservation_id: str,
) -> None:
    from control_plane.disposable_lanes import require_disposable_lane

    if store.read_product_profile_record(profile.product) != profile:
        raise ValueError("The preview profile changed before disposal.")
    original = store.read_idempotency_record_by_id(original_reservation_id)
    if original is None:
        raise ValueError("Interrupted preview original proof is unavailable.")
    retained = original_preview_intent(original, profile=profile, store=store).issued_plan
    if (
        not profile.preview.clean_testing_snapshot
        or retained.operation != "refresh"
        or retained.plan_request.pr_number != number
        or retained.dry_run_plan.clean_snapshot is None
    ):
        raise ValueError("Interrupted preview original proof differs.")
    instance = profile.preview.slug_template.format(number=number)
    preview = find_preview_record(
        record_store=store,
        context_name=profile.preview.context,
        anchor_repo=profile.repository.partition("/")[2],
        anchor_pr_number=number,
    )
    if preview is None:
        require_unrecorded_preview_original(
            store, profile=profile, number=number, reservation_id=original_reservation_id
        )
    else:
        require_disposable_lane(
            store,
            product=profile.product,
            context=profile.preview.context,
            instance=instance,
            preview=preview,
            preview_operation="destroy",
        )
    report = store.read_disposable_lane_safety(profile.product, profile.preview.context, instance)
    try:
        target = store.read_dokploy_target_record(
            context_name=profile.preview.context, instance_name=instance
        )
    except FileNotFoundError:
        target = None
    if (target is not None and target.policies.staff_testing_hold is not None) or (
        report is not None
        and (
            report.usage == "in_use"
            or report.data == "only_copy"
            or report.production_resources in {"shared", "separation_ready"}
        )
    ):
        raise ValueError("Interrupted preview disposal is held by use, data or shared resources.")


def require_unrecorded_preview_original(
    store: PostgresRecordStore,
    *,
    profile: LaunchplaneProductProfileRecord,
    number: int,
    reservation_id: str,
) -> LaunchplaneIdempotencyRecord:
    from control_plane.disposable_lanes import require_lane_target_ownership

    reservation = store.read_idempotency_record_by_id(reservation_id)
    if reservation is None:
        raise ValueError("Original preview reservation is unavailable.")
    original = original_preview_intent(reservation, profile=profile, store=store)
    issued = original.issued_plan
    instance = profile.preview.slug_template.format(number=number)
    if (
        issued.operation != "refresh"
        or issued.plan_request.pr_number != number
        or issued.context != profile.preview.context
        or issued.preview_slug != instance
        or issued.dry_run_plan.compose_name
        != preview_application_name(
            app_name_prefix=effective_preview_app_name_prefix(profile=profile),
            preview_slug=instance,
        )
        or any(
            profile.preview.slug_template.format(number=item.anchor_pr_number) == instance
            and (
                item.state != "destroyed"
                or parse_launchplane_mutation_timestamp(item.updated_at, field_name="updated_at")
                >= parse_launchplane_mutation_timestamp(
                    reservation.created_at, field_name="created_at"
                )
            )
            for item in store.list_preview_records(context_name=profile.preview.context)
        )
    ):
        raise ValueError("Original preview does not identify an unrecorded preview.")
    for other in store.list_product_profile_records():
        if any(
            (lane.context, lane.instance) == (issued.context, instance) for lane in other.lanes
        ) or (
            other.product != profile.product
            and issued.context
            in {
                other.preview.context,
                *other.historical_contexts,
                *(lane.context for lane in other.lanes),
            }
        ):
            raise ValueError("Original preview has shared lane or context ownership.")
    target_id = issued.dry_run_plan.compose_ref
    require_lane_target_ownership(
        (*store.list_dokploy_target_id_records(), *store.list_physical_provider_target_records()),
        context=issued.context,
        instance=instance,
        target_id="" if target_id.startswith("${created.") else target_id,
    )
    return reservation


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
