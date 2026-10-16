"""Qualify the original Director agent's disposable staff-hold continuation."""

from control_plane.contracts.dokploy_target_record import DokployTargetRecord, TestingHoldRehearsal
from control_plane.disposable_lanes import DisposableLaneSafetyUnknown, require_disposable_lane
from control_plane.generic_web_testing_exclusion import _target_reservations
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_lane_hold import (
    TestingHoldApplyRequest,
    TestingHoldPlan,
    TestingHoldStale,
    build_testing_hold_plan,
    TESTING_HOLD_SOURCE_LABEL,
)
from control_plane.integration_allowances import target_record_sha256, utc_now_timestamp
from control_plane.workflows.generic_web_deploy_provider import generic_web_provider_target_key


def qualify_testing_hold_rehearsal(
    store: PostgresRecordStore, request: TestingHoldApplyRequest, actor: str
) -> TestingHoldRehearsal | None:
    if (
        not request.rehearsal
        or request.purpose != "staff_testing"
        or request.activation_plan_sha256
    ):
        raise DisposableLaneSafetyUnknown("A staff-hold rehearsal cannot use a staging interlock.")
    target = store.read_dokploy_target_record(
        context_name=request.context, instance_name=request.instance
    )
    existing = target.policies.staff_testing_hold
    if existing is None and not request.hold:
        raise DisposableLaneSafetyUnknown("Only the original rehearsal hold can be lifted.")
    binding = require_disposable_lane(
        store,
        product=request.product,
        context=request.context,
        instance=request.instance,
        testing_hold_actor=actor if existing else "",
    )
    target_id = store.read_dokploy_target_id_record(
        context_name=request.context, instance_name=request.instance
    )
    try:
        provider = store.read_provider_target_record(
            context_name=request.context, instance_name=request.instance
        )
    except FileNotFoundError:
        provider = None
    if provider is not None and (
        provider.provider_id != "dokploy"
        or provider.target_id != target_id.target_id
        or provider.provider_target_type != target.target_type
    ):
        raise DisposableLaneSafetyUnknown("The current provider recipient changed or is ambiguous.")
    # Include other provider-record aliases, not only legacy target-id records.
    if any(
        item.target_id == target_id.target_id
        and (item.context, item.instance) != (request.context, request.instance)
        for item in store.list_provider_target_records()
    ):
        raise DisposableLaneSafetyUnknown("The testing recipient aliases another lane.")
    key = generic_web_provider_target_key(
        provider_id="dokploy",
        context=request.context,
        instance=request.instance,
        target_type=target.target_type,
        target_id=target_id.target_id,
    )
    if _target_reservations(
        store,
        product=request.product,
        context=request.context,
        target_key=key,
        target_id=target_id.target_id,
    ) or store.list_odoo_stable_target_replacement_operation_records(
        context_name=request.context,
        instance_name=request.instance,
        statuses=("pending", "running", "reconciliation_required"),
        limit=1,
    ):
        raise DisposableLaneSafetyUnknown("Testing execution is active or its outcome is unknown.")
    if existing is not None:
        return existing.rehearsal
    if not request.hold:
        return None
    return TestingHoldRehearsal(
        binding_sha256=binding,
        target_updated_at=target.updated_at,
        target_source_label=target.source_label,
    )


def prepare_testing_hold_rehearsal_apply(
    store: PostgresRecordStore,
    request: TestingHoldApplyRequest,
    actor: str,
    rehearsal: TestingHoldRehearsal,
) -> tuple[TestingHoldPlan, DokployTargetRecord, DokployTargetRecord]:
    plan, replacement = build_testing_hold_plan(
        record_store=store,
        request=request,
        actor=actor,
        rehearsal=rehearsal,
    )
    if request.reviewed_plan_sha256 != plan.plan_sha256:
        raise TestingHoldStale("Reviewed testing hold plan no longer matches the lane's record.")
    expected = store.read_dokploy_target_record(
        context_name=request.context, instance_name=request.instance
    )
    if target_record_sha256(expected) != plan.record_sha256_before:
        raise TestingHoldStale("The testing hold changed before apply.")
    if plan.changed:
        replacement = replacement.model_copy(
            update={
                "updated_at": utc_now_timestamp(),
                "source_label": TESTING_HOLD_SOURCE_LABEL,
            }
        )
    return (
        plan.model_copy(
            update={
                "applied": plan.changed,
                "read_back": replacement.policies.staff_testing_hold,
                "read_back_matches": True,
                "reconcile_requested": not request.hold,
                "record_sha256_after": target_record_sha256(replacement),
            }
        ),
        expected,
        replacement,
    )
