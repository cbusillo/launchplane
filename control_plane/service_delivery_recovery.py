"""Read-only recovery of the original service delivery Apply."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.delivery_apply_receipt import read_delivery_apply_receipt
from control_plane.github_delivery_configuration import (
    DeliveryGitHubAppConfigurationRequest,
    GITHUB_DELIVERY_CONFIGURATION_ROUTE,
)
from control_plane.service_github_delivery_controls import (
    ServiceTokenRetirementRequest,
    SERVICE_TOKEN_RETIREMENT_ROUTE,
)
from control_plane.storage.postgres import PostgresRecordStore

SERVICE_DELIVERY_APPLY_RECEIPT_ROUTE = "/v1/service/delivery/apply-receipt"


class ServiceDeliveryApplyReceiptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    original_request: DeliveryGitHubAppConfigurationRequest | ServiceTokenRetirementRequest
    idempotency_key: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def require_original_apply(self) -> "ServiceDeliveryApplyReceiptRequest":
        if self.original_request.mode != "apply" or not self.idempotency_key.strip():
            raise ValueError("Receipt recovery requires the original Apply request and key.")
        return self


class ServiceDeliveryApplyReceiptResponse(BaseModel):
    state: Literal["completed", "unknown"]
    operation: Literal["delivery-selection", "service-token-retirement"]
    idempotency_key: str
    trace_id: str
    original_trace_id: str | None = None


def read_service_delivery_apply_receipt(
    *,
    store: PostgresRecordStore,
    request: ServiceDeliveryApplyReceiptRequest,
    actor: str,
    trace_id: str,
) -> ServiceDeliveryApplyReceiptResponse:
    selection = isinstance(request.original_request, DeliveryGitHubAppConfigurationRequest)
    receipt = read_delivery_apply_receipt(
        store=store,
        request=request.original_request,
        actor=actor,
        scope=f"{'github-delivery-configuration' if selection else 'service-token-retirement'}:{actor}",
        route_path=GITHUB_DELIVERY_CONFIGURATION_ROUTE
        if selection
        else SERVICE_TOKEN_RETIREMENT_ROUTE,
        idempotency_key=request.idempotency_key,
    )
    return ServiceDeliveryApplyReceiptResponse(
        state="completed" if receipt else "unknown",
        operation="delivery-selection" if selection else "service-token-retirement",
        idempotency_key=request.idempotency_key,
        trace_id=trace_id,
        original_trace_id=receipt.response_trace_id if receipt else None,
    )
