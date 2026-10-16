"""Retire incomplete testing generation custody without releasing its operation."""

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.odoo_testing_source import NativeTestingCopyCapture
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.product_profile_record import (
    is_exclusive_product_context,
    product_context_owner_map,
)
from control_plane.disposable_lanes import disposable_lane_configuration
from control_plane.launchplane_reconcile_authorization import (
    launchplane_reconcile_authorization_allows,
)
from control_plane.odoo_testing_restore import FreshOdooTestingGeneration, generation_binding
from control_plane.testing_replacement_recovery import (
    FRESH_TESTING_INTENT_BINDING_PREFIX,
    service_testing_operation,
)

ROUTE = "/v1/products/{product}/testing-generation-retirement"

if TYPE_CHECKING:
    from control_plane.storage.postgres import PostgresRecordStore


class TestingGenerationRetirementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    context: str = Field(min_length=1)
    instance: Literal["testing"] = "testing"
    operation_id: str = Field(min_length=1)
    mode: Literal["dry-run", "apply", "readback"] = "dry-run"
    reason: str = Field(min_length=1)
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(|[a-f0-9]{64})$")


class TestingGenerationRetirementPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    instance: Literal["testing"] = "testing"
    operation_id: str
    plan_sha256: str
    generation_binding_sha256: str
    original_authority: Literal["unverified"] = "unverified"
    historical_provider_effects: Literal["unknown"] = "unknown"
    operation_fence_retained: Literal[True] = True
    resources_preserved: Literal[True] = True
    applied: bool = False


class RetainedTestingGeneration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generation: FreshOdooTestingGeneration
    operation: OdooStableTargetReplacementOperationRecord
    target: DokployTargetRecord
    capture: NativeTestingCopyCapture | None = None
    plan: TestingGenerationRetirementPlan
    retired_at: str
    retired_by: str
    reason: str


def retirement_snapshot(
    store: "PostgresRecordStore", product: str, request: TestingGenerationRetirementRequest
) -> RetainedTestingGeneration:
    """Read only current authority; never pretend it is the missing old preimage."""
    operation = getattr(store, "read_odoo_stable_target_replacement_operation_record")(
        request.operation_id
    )
    generation = getattr(store, "read_fresh_odoo_testing_generation")(product, request.context)
    profile = getattr(store, "read_product_profile_record")(product)
    target = getattr(store, "read_dokploy_target_record")(
        context_name=request.context, instance_name="testing"
    )
    target_id = getattr(store, "read_dokploy_target_id_record")(
        context_name=request.context, instance_name="testing"
    )
    journal = operation.recovery_journal
    if (
        not service_testing_operation(operation)
        or operation.status != "reconciliation_required"
        or (operation.product, operation.context, operation.instance)
        != (product, request.context, "testing")
        or journal is None
        or journal.attempt != operation.attempt
        or journal.target_fingerprint.startswith(FRESH_TESTING_INTENT_BINDING_PREFIX)
        or generation is None
        or generation.completed_at
        or not generation.reconcile_requested_at
        or generation.operation_id == operation.operation_id
        or generation.target_id != journal.target_id
        or generation.target_id != target_id.target_id
        or profile.driver_id != "odoo"
        or not profile.is_active
        or target.target_type != "compose"
        or target.policies.staff_testing_hold is not None
        or target.pending_public_host_effects
        or generation.domain_host not in target.domains
        or not is_exclusive_product_context(
            context=request.context,
            product=product,
            owners=product_context_owner_map(getattr(store, "list_product_profile_records")()),
        )
    ):
        raise ValueError("Retirement requires the exact incomplete legacy testing generation.")
    assert operation.authorization is not None
    if not launchplane_reconcile_authorization_allows(
        authorization=operation.authorization,
        product=product,
        context=request.context,
        instances=("testing",),
        record_store=store,
    ):
        raise ValueError("The current service testing authority is unavailable.")
    allocation = getattr(store, "read_disposable_resource_preparation")(generation.operation_id)
    if allocation is None or (
        allocation.replacement_target_id,
        allocation.resource_namespace,
        allocation.resources_binding_sha256,
    ) != (generation.target_id, generation.namespace, generation.resources_binding_sha256):
        raise ValueError("Original allocation custody is unavailable.")
    identities = getattr(store, "list_dokploy_target_id_records")()
    providers = getattr(store, "list_physical_provider_target_records")()
    if any(
        item.target_id == generation.target_id
        and (item.context, item.instance) != (request.context, "testing")
        for item in identities
    ) or any(
        item.provider_id == "dokploy"
        and item.target_id == generation.target_id
        and (item.context, item.instance) != (request.context, "testing")
        for item in providers
    ):
        raise ValueError("The retained target aliases another lane.")
    capture = getattr(store, "read_native_testing_copy_capture")(generation_binding(generation))
    try:
        overrides = store.read_odoo_instance_override_record(
            context_name=request.context, instance_name="testing"
        )
    except FileNotFoundError:
        overrides = None
    evidence = {
        "operation": operation.model_dump(mode="json"),
        "generation": generation.model_dump(mode="json"),
        "profile": profile.model_dump(mode="json"),
        "target": target.model_dump(mode="json"),
        "target_id": target_id.model_dump(mode="json"),
        "allocation": allocation.model_dump(mode="json"),
        "capture": capture.model_dump(mode="json") if capture else None,
        "overrides": overrides.model_dump(mode="json") if overrides else None,
        "configuration": [
            item.model_dump(mode="json")
            for item in disposable_lane_configuration(store, request.context, "testing")
        ],
        "reason": request.reason,
    }
    return RetainedTestingGeneration(
        generation=generation,
        operation=operation,
        target=target,
        capture=capture,
        plan=TestingGenerationRetirementPlan(
            product=product,
            context=request.context,
            operation_id=operation.operation_id,
            plan_sha256=canonical_json_sha256(evidence),
            generation_binding_sha256=generation_binding(generation),
        ),
        retired_at="",
        retired_by="",
        reason=request.reason,
    )
