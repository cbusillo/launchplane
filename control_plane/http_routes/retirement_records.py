"""Reads of product and detached-application retirement records.

A retirement's plan and apply records were readable only by replaying the plan
or apply with its grant. These reads show a record's structured outcome to a
caller with ``operations.read``: ids, mode, outcome, times, provider-effect
flags and the error code. The reason, the error message, provider names and
observations, and authority snapshots stay out.
"""

from typing import Annotated, Literal, cast

from fastapi import Depends, Path
from pydantic import BaseModel, ConfigDict

from control_plane.contracts.detached_application_retirement import (
    DetachedApplicationRetirementRecord,
)
from control_plane.contracts.product_retirement import ProductRetirementRecord
from control_plane.http_routes.support import (
    LAUNCHPLANE_SERVICE_CONTEXT,
    ApiRouteRegistrar,
    ReadRouteDependencies,
)
from control_plane.operation_status_read import (
    OPERATION_STATUS_READ_ACTION,
    OPERATION_STATUS_READ_PRODUCT,
    safe_operation_error_code,
)
from control_plane.service_auth import AuthorizationTarget, LaunchplaneIdentity

PRODUCT_RETIREMENT_RECORD_ROUTE = "/v1/product-retirements/{record_id}"
DETACHED_APPLICATION_RETIREMENT_RECORD_ROUTE = "/v1/detached-application-retirements/{record_id}"


class RetirementOutcomeView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    plan_record_id: str
    mode: str
    outcome: str
    requested_at: str
    recorded_at: str
    completed_at: str
    provider_effect_attempted: bool
    provider_effect_performed: bool
    provider_absence_verified: bool
    error_code: str
    free_text_omitted: Literal[True] = True


class ProductRetirementOutcomeView(RetirementOutcomeView):
    product: str
    context: str
    instance: str
    lifecycle_before: str
    lifecycle_after: str


class DetachedApplicationRetirementOutcomeView(RetirementOutcomeView):
    protected_targets_unchanged: bool


class ProductRetirementRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: ProductRetirementOutcomeView


class DetachedApplicationRetirementRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: DetachedApplicationRetirementOutcomeView


def _common_view_fields(
    record: ProductRetirementRecord | DetachedApplicationRetirementRecord,
) -> dict[str, object]:
    evidence = record.mutation_evidence
    return {
        "record_id": record.record_id,
        "plan_record_id": record.plan_record_id,
        "mode": record.mode,
        "outcome": record.outcome,
        "requested_at": record.requested_at,
        "recorded_at": record.recorded_at,
        "completed_at": record.completed_at,
        "provider_effect_attempted": evidence.provider_effect_attempted,
        "provider_effect_performed": evidence.provider_effect_performed,
        "provider_absence_verified": evidence.provider_absence_verified,
        "error_code": safe_operation_error_code(evidence.error_code),
    }


def product_retirement_outcome_view(
    record: ProductRetirementRecord,
) -> ProductRetirementOutcomeView:
    return ProductRetirementOutcomeView.model_validate(
        {
            **_common_view_fields(record),
            "product": record.product,
            "context": record.context,
            "instance": record.instance,
            "lifecycle_before": record.mutation_evidence.lifecycle_before,
            "lifecycle_after": record.mutation_evidence.lifecycle_after,
        }
    )


def detached_application_retirement_outcome_view(
    record: DetachedApplicationRetirementRecord,
) -> DetachedApplicationRetirementOutcomeView:
    return DetachedApplicationRetirementOutcomeView.model_validate(
        {
            **_common_view_fields(record),
            "protected_targets_unchanged": record.mutation_evidence.protected_targets_unchanged,
        }
    )


def register_retirement_record_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
) -> None:
    def read_record(
        record_store: object, method_name: str, record_id: str, trace_id: str
    ) -> object:
        read = getattr(record_store, method_name, None)
        if not callable(read):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Retirement records require Launchplane database storage.",
            )
        try:
            return read(record_id)
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Retirement record was not found.",
            ) from error

    def require_read(
        identity: LaunchplaneIdentity, context: str, target: AuthorizationTarget, trace_id: str
    ) -> None:
        if not dependencies.authorization_allows(
            identity=identity,
            action=OPERATION_STATUS_READ_ACTION,
            product=OPERATION_STATUS_READ_PRODUCT,
            context=context,
            target=target,
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="The caller cannot read retirement records for the requested context.",
            )

    def read_product_retirement_record(
        record_id: Annotated[str, Path(min_length=1, max_length=256, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> ProductRetirementRecordResponse:
        trace_id = dependencies.next_trace_id()
        record = cast(
            ProductRetirementRecord,
            read_record(record_store, "read_product_retirement_record", record_id, trace_id),
        )
        require_read(
            identity,
            record.context,
            AuthorizationTarget(scope="instance", instances=(record.instance,)),
            trace_id,
        )
        return ProductRetirementRecordResponse(
            trace_id=trace_id, record=product_retirement_outcome_view(record)
        )

    def read_detached_application_retirement_record(
        record_id: Annotated[str, Path(min_length=1, max_length=256, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> DetachedApplicationRetirementRecordResponse:
        trace_id = dependencies.next_trace_id()
        # A detached application belongs to no product lane, so the read uses
        # Launchplane's own service context.
        require_read(
            identity, LAUNCHPLANE_SERVICE_CONTEXT, AuthorizationTarget(scope="context"), trace_id
        )
        record = cast(
            DetachedApplicationRetirementRecord,
            read_record(
                record_store, "read_detached_application_retirement_record", record_id, trace_id
            ),
        )
        return DetachedApplicationRetirementRecordResponse(
            trace_id=trace_id, record=detached_application_retirement_outcome_view(record)
        )

    responses = {
        code: {"model": dependencies.error_response_model} for code in (401, 403, 404, 503)
    }
    app.add_api_route(
        PRODUCT_RETIREMENT_RECORD_ROUTE,
        read_product_retirement_record,
        methods=["GET"],
        response_model=ProductRetirementRecordResponse,
        operation_id="read_product_retirement_record",
        summary="Read one product retirement record's structured outcome",
        responses=responses,
    )
    app.add_api_route(
        DETACHED_APPLICATION_RETIREMENT_RECORD_ROUTE,
        read_detached_application_retirement_record,
        methods=["GET"],
        response_model=DetachedApplicationRetirementRecordResponse,
        operation_id="read_detached_application_retirement_record",
        summary="Read one detached application retirement record's structured outcome",
        responses=responses,
    )


__all__ = [
    "DETACHED_APPLICATION_RETIREMENT_RECORD_ROUTE",
    "PRODUCT_RETIREMENT_RECORD_ROUTE",
    "register_retirement_record_read_routes",
]
