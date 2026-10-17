"""Initial testing custody, separate from stranded replacement operation history."""

from enum import StrEnum
from pathlib import Path

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.disposable_lane import DisposableTestingResourcePreparation
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.disposable_lanes import (
    disposable_lane_binding_sha256,
    disposable_lane_configuration,
)
from control_plane.lane_usage import evaluate_lane_usage
from control_plane.odoo_testing_usage import read_odoo_testing_usage
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_replacement_resources import (
    TestingReplacementResourcesRequest,
    resource_request_payload,
)


class InitialTestingPrerequisiteCode(StrEnum):
    PREPARATION_UNAVAILABLE = "testing_initial_preparation_unavailable"
    COPY_PREREQUISITE_UNVERIFIED = "testing_initial_copy_prerequisite_unverified"
    USAGE_UNVERIFIED = "testing_initial_usage_unverified"


def initial_preparation_id(product: str, context: str, scope: str, key: str) -> str:
    if not key.strip():
        raise ValueError(
            "Initial preparation requires the original stable key, including planning."
        )
    return "initial-testing-" + canonical_json_sha256(
        {"product": product, "context": context, "scope": scope, "key": key.strip()}
    )


def initial_resource_context(
    store: PostgresRecordStore,
    product: str,
    request: TestingReplacementResourcesRequest,
    preparation: DisposableTestingResourcePreparation,
    allow_bootstrap_resume: bool,
) -> None:
    if preparation.initial_released_at:
        raise ValueError(
            "This initial intent was released; retain its history and review a new key."
        )
    if not preparation.initial_request_sha256 or (
        preparation.product,
        preparation.context,
        preparation.operation_id,
    ) != (product, request.context, request.operation_id):
        raise ValueError("Initial testing preparation belongs to another intent.")
    if not allow_bootstrap_resume and preparation.initial_request_sha256 != canonical_json_sha256(
        resource_request_payload(request)
    ):
        raise ValueError("Use the original initial resource intent.")
    if preparation.initial_source_mode != request.source_mode:
        raise ValueError("The initial bootstrap source mode changed.")
    store.require_initial_testing_lane_available(preparation, allow_bootstrap_resume)
    if not preparation.replacement_target_id:
        profile = store.read_product_profile_record(product)
        target = store.read_dokploy_target_record(
            context_name=request.context, instance_name="testing"
        )
        target_id = store.read_dokploy_target_id_record(
            context_name=request.context, instance_name="testing"
        )
        binding = disposable_lane_binding_sha256(
            profile,
            target,
            target_id,
            configuration=disposable_lane_configuration(store, request.context, "testing"),
        )
        if binding != preparation.initial_binding_sha256:
            raise ValueError("The original testing authority changed; review a new plan.")


def require_initial_bootstrap_prerequisites(
    store: PostgresRecordStore,
    product: str,
    request: TestingReplacementResourcesRequest,
) -> None:
    from control_plane.testing_replacement_bootstrap import bootstrap_copy_prerequisites

    profile = store.read_product_profile_record(product)
    target = store.read_dokploy_target_record(context_name=request.context, instance_name="testing")
    # Adoption installs an empty target; source wiring that exists only on the
    # retained target cannot qualify the future independent copy.
    bootstrap_copy_prerequisites(
        store,
        profile,
        request.context,
        target.model_copy(update={"env": {}}),
        None,
        request.source_mode,
    )


def require_initial_usage(
    root: Path,
    store: PostgresRecordStore,
    product: str,
    request: TestingReplacementResourcesRequest,
) -> None:
    allocation = store.read_disposable_resource_preparation(request.operation_id)
    if allocation is None or allocation.retained_target_record is None:
        raise ValueError("Original testing custody is unavailable.")
    target = allocation.retained_target_record
    target_id = DokployTargetIdRecord(
        context=request.context,
        instance="testing",
        target_id=allocation.retained_target_id,
        updated_at=target.updated_at,
    )
    require_testing_generation_usage(root, store, product, request.context, target, target_id)


class TestingGenerationUsageUnverified(ValueError):
    """The original use cannot be established from current read-only evidence."""


def require_testing_generation_usage(
    root: Path,
    store: PostgresRecordStore,
    product: str,
    context: str,
    target: DokployTargetRecord,
    target_id: DokployTargetIdRecord,
) -> None:
    current = store.read_dokploy_target_record(context_name=context, instance_name="testing")
    if (
        target.policies.staff_testing_hold is not None
        or current.policies.staff_testing_hold is not None
    ):
        raise ValueError("A hold protects testing.")
    usage = read_odoo_testing_usage(
        target=target,
        target_id=target_id,
        control_plane_root=root,
        database_url=store.database_url,
    )
    evidence = store.read_disposable_lane_safety(product, context, "testing")
    known_use = evidence is not None and evidence.usage == "in_use"
    decision = evaluate_lane_usage(
        store,
        profile=store.read_product_profile_record(product),
        target=target,
        target_id=target_id,
        instance="testing",
        usage="in_use" if known_use else usage.usage,
        runtime_stopped=usage.reason_code == "serving_container_stopped",
    )
    if decision.protected:
        if not known_use and usage.usage == "unknown":
            raise TestingGenerationUsageUnverified("Original testing use is unverified.")
        raise ValueError("Original testing use is protected.")
    # Data custody is preserved by construction: only the separately named empty
    # target is ever written. No positive fact is asserted about the old copy.
