"""Continue only the original caller's fresh, undeployed testing application."""

from pathlib import Path
from collections.abc import Callable

from control_plane.application_state_volume import (
    ApplicationStateVolumeAdapter,
    state_volume_target_key,
)
from control_plane.disposable_lanes import DisposableLaneSafetyUnknown
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.generic_web_testing_exclusion import _target_reservations
from control_plane.lane_usage import lane_usage_protected
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_lane_hold import TestingHoldApplyRequest
from control_plane.workflows.generic_web_deploy_provider import (
    dokploy_generic_web_provider_target_key,
)


def is_application_staging_lift(store: object, request: TestingHoldApplyRequest) -> bool:
    """Select the bounded continuation, without treating its shape as qualification."""
    if (
        not isinstance(store, PostgresRecordStore)
        or request.instance != "testing"
        or request.hold
        or request.purpose != "isolation_staging"
        or request.rehearsal
        or request.activation_plan_sha256
    ):
        return False
    try:
        target = store.read_dokploy_target_record(
            context_name=request.context, instance_name=request.instance
        )
    except FileNotFoundError:
        return False
    return target.target_type == "application"


def qualify_application_staging_lift(
    store: PostgresRecordStore,
    request: TestingHoldApplyRequest,
    *,
    root: Path,
    actor: str,
    scope: str,
    trace: str,
    inspect_provider: bool,
    inspect_storage: bool = False,
    before_inspection: Callable[[str], None] | None = None,
) -> str:
    """Return a current DB binding after all unchanged allocation checks pass."""
    if not is_application_staging_lift(store, request):
        raise ValueError("Only an original testing application's staging interlock can continue.")
    profile = store.read_product_profile_record(request.product)
    if (
        profile.driver_id != "generic-web"
        or not profile.is_active
        or sum(
            (lane.context, lane.instance) == (request.context, request.instance)
            for lane in profile.lanes
        )
        != 1
    ):
        raise ValueError("Continuation requires its active, exact generic-web testing lane.")
    target = store.read_dokploy_target_record(
        context_name=request.context, instance_name=request.instance
    )
    hold = target.policies.staff_testing_hold
    if hold is None or hold.purpose != "isolation_staging" or hold.recorded_by != actor:
        raise ValueError("Only this caller's unchanged staging interlock can continue.")
    target_id = store.read_dokploy_target_id_record(
        context_name=request.context, instance_name=request.instance
    )
    provider = store.read_provider_target_record(
        context_name=request.context, instance_name=request.instance
    )
    physical_targets: tuple[ProviderTargetRecord | DokployTargetIdRecord, ...] = (
        *store.list_physical_provider_target_records(),
        *store.list_dokploy_target_id_records(),
    )
    if any(
        item.target_id == target_id.target_id
        and (item.context, item.instance) != (request.context, request.instance)
        for item in physical_targets
    ):
        raise ValueError("The application aliases another lane's recipient.")
    evidence = store.read_disposable_lane_safety(request.product, request.context, request.instance)
    if evidence is not None and (
        lane_usage_protected(
            store,
            product=request.product,
            context=request.context,
            instance=request.instance,
            usage=evidence.usage,
            require_unused=False,
        )
        or evidence.data == "only_copy"
        or evidence.production_resources in {"shared", "separation_ready"}
    ):
        raise DisposableLaneSafetyUnknown(
            "Recorded person use or protected resources must be resolved."
        )
    for deployment in store.list_deployment_records(
        context_name=request.context, instance_name=request.instance
    ):
        if deployment.deploy.status == "pass" or (
            deployment.deployed_target is not None
            and deployment.deployed_target.target_id == target_id.target_id
        ):
            raise ValueError("The application already has deployment history.")
    try:
        store.read_environment_inventory(
            context_name=request.context, instance_name=request.instance
        )
    except FileNotFoundError:
        pass
    else:
        raise ValueError("The testing lane already has a current artifact.")
    key = dokploy_generic_web_provider_target_key(provider)
    if (
        _target_reservations(
            store,
            product=request.product,
            context=request.context,
            target_key=key,
            target_id=target_id.target_id,
        )
        or any(
            row.provider_target_key
            in {
                state_volume_target_key(request.context),
                f"testing-private-ingress:{request.product}:{request.context}",
            }
            for row in store.list_held_provider_target_reservations()
        )
        or store.list_odoo_stable_target_replacement_operation_records(
            context_name=request.context,
            instance_name=request.instance,
            statuses=("pending", "running", "reconciliation_required"),
            limit=1,
        )
    ):
        raise ValueError("Testing effects are active or their outcome is unknown.")
    adapter = ApplicationStateVolumeAdapter.completed_for_testing_lane(
        root, store, product=request.product, context=request.context, scope=scope, trace=trace
    )
    binding = adapter.qualify_testing_continuation(inspect_provider=inspect_provider)
    if inspect_storage:
        from control_plane.application_testing_storage import inspect_application_testing_storage

        if before_inspection is None:
            raise ValueError(
                "Host inspection requires its live execution and authority checkpoint."
            )
        inspect_application_testing_storage(adapter, checkpoint=before_inspection)
    return binding
