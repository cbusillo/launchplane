from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field

from control_plane.contracts.testing_activation import (
    TestingActivationRequest,
    TestingActivationRefusal,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_activation import ROUTE, activation_record, preparation_resources
from control_plane.testing_separation_inspection import inspect_preparation_programs
from control_plane.testing_separation_resources import (
    SeparationResourcesStatus,
    SeparationProgramInspection,
)


class TestingActivationInspection(SeparationResourcesStatus):
    activation_phase: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    predecessor_recorded: bool
    external_capability_count: int = Field(ge=0)
    programs: tuple[SeparationProgramInspection, ...]
    inspected_at: AwareDatetime
    qualification: Literal["not_evaluated"] = "not_evaluated"


def inspect_application_activation(
    *,
    root: Path,
    store: PostgresRecordStore,
    product: str,
    request: TestingActivationRequest,
    actor: str,
    scope: str,
    idempotency_key: str,
) -> TestingActivationInspection:
    if not idempotency_key or request.application is None:
        raise TestingActivationRefusal(
            "Inspection requires the original application key and request."
        )
    row = store.read_idempotency_record(
        scope=scope, route_path=ROUTE.format(product=product), idempotency_key=idempotency_key
    )
    original = store.read_testing_application_activation_operation(
        product, request.context, scope, request.preparation_key
    )
    record = activation_record(
        store, product, request.context, scope=scope, preparation_key=request.preparation_key
    )
    if (
        row is None
        or original is None
        or original.record_id != row.record_id
        or record is None
        or record.actor != actor
        or record.plan_sha256 != row.reconciliation_key
        or record.request.model_dump(exclude={"mode", "reviewed_plan_sha256"})
        != request.model_dump(exclude={"mode", "reviewed_plan_sha256"})
    ):
        raise TestingActivationRefusal("Inspection belongs to another original operation.")
    resources, preparation_id = preparation_resources(store, product, request, scope)
    if (
        preparation_id != record.preparation_record_id
        or resources.target_id != record.retained_target_id.target_id
        or record.namespace != resources.namespace + "-recipient"
    ):
        raise TestingActivationRefusal("Inspection preparation custody changed.")
    programs = inspect_preparation_programs(
        root=root,
        resources=resources,
        store=store,
        application=True,
        namespace=record.namespace,
        include_storage=False,
    )
    return TestingActivationInspection(
        product=product,
        context=request.context,
        state=row.state,
        plan_sha256=record.plan_sha256,
        provider_effect_phase=row.provider_effect_phase,
        created_at=row.created_at,
        updated_at=row.updated_at,
        lease_expires_at=row.lease_expires_at,
        provider_effect_started_at=row.provider_effect_started_at,
        activation_phase=record.phase,
        predecessor_recorded=record.predecessor is not None,
        external_capability_count=len(record.external_capabilities),
        programs=tuple(programs),
        inspected_at=datetime.now(timezone.utc),
    )
