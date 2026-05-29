from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane import product_config as control_plane_product_config
from control_plane import product_config_service as control_plane_product_config_service
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.product_config import ProductConfigStore
from control_plane.service_auth import (
    LaunchplaneAuthzPolicy,
    LaunchplaneIdentity,
    LocalAdminIdentity,
    LocalOperatorIdentity,
)


JsonResponse = Callable[..., list[bytes]]
StartResponse = Callable[[str, list[tuple[str, str]]], None]


class ProductConfigRouteStore(ProductConfigStore, Protocol):
    def list_dokploy_target_records(self) -> tuple[DokployTargetRecord, ...]: ...


class ProductConfigApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: str
    product: str
    context: str = ""
    instance: str = ""
    source_label: str = "product-config-api"
    reason: str = ""
    runtime_env: dict[str, object] | None = None
    runtime_environment: dict[str, object] | None = None
    secrets: list[dict[str, object]] = Field(default_factory=list)

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, value: str) -> str:
        normalized_value = value.strip().lower()
        if normalized_value not in {"dry-run", "apply"}:
            raise ValueError("Product config mode must be 'dry-run' or 'apply'.")
        return normalized_value

    @model_validator(mode="after")
    def _validate_product(self) -> "ProductConfigApplyEnvelope":
        self.product = self.product.strip()
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        self.source_label = self.source_label.strip() or "product-config-api"
        self.reason = self.reason.strip()
        if not self.product:
            raise ValueError("Product config apply requires product.")
        return self

    def product_config_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "product": self.product,
            "context": self.context,
            "instance": self.instance,
            "secrets": self.secrets,
        }
        if self.runtime_env is not None:
            payload["runtime_env"] = self.runtime_env
        if self.runtime_environment is not None:
            payload["runtime_environment"] = self.runtime_environment
        return payload


@dataclass(frozen=True)
class ProductConfigRouteResult:
    driver_result: dict[str, object] | None


def product_config_live_target_next_actions(
    *,
    request: ProductConfigApplyEnvelope,
    driver_result: dict[str, object] | None,
    tracked_targets: tuple[DokployTargetRecord, ...],
) -> list[dict[str, object]]:
    if request.mode != "apply" or not driver_result:
        return []
    runtime_environment = driver_result.get("runtime_environment")
    if not isinstance(runtime_environment, dict):
        return []
    changed_keys = runtime_environment.get("changed_keys")
    if not isinstance(changed_keys, list) or not changed_keys:
        return []
    context_name = str(runtime_environment.get("context") or "")
    instance_name = str(runtime_environment.get("instance") or "")
    target = next(
        (
            record
            for record in tracked_targets
            if record.context == context_name and record.instance == instance_name
        ),
        None,
    )
    if target is None:
        return []
    return [
        {
            "kind": "live_target_runtime_apply",
            "required": True,
            "status": "live_sync_required",
            "target": {
                "context": context_name,
                "instance": instance_name,
                "target_type": target.target_type,
                "target_name": target.target_name,
            },
            "changed_keys": sorted(str(key) for key in changed_keys),
            "dry_run": {
                "method": "POST",
                "endpoint": "/v1/live-target-runtime/apply",
                "mode": "dry-run",
            },
            "apply": {
                "method": "POST",
                "endpoint": "/v1/live-target-runtime/apply",
                "mode": "apply",
            },
            "instruction": (
                "Run live-target-runtime dry-run, then apply with a concrete reason. "
                "Redeploying the same app image does not sync the live Dokploy target environment."
            ),
        }
    ]


def validate_product_config_apply_request(
    *,
    authz_policy: LaunchplaneAuthzPolicy,
    identity: LaunchplaneIdentity,
    payload: dict[str, object],
    trace_id: str,
    json_response: JsonResponse,
    start_response: StartResponse,
) -> tuple[ProductConfigApplyEnvelope | None, list[bytes] | None]:
    request = ProductConfigApplyEnvelope.model_validate(payload)
    if isinstance(identity, (LocalOperatorIdentity, LocalAdminIdentity)) and not request.reason:
        return None, json_response(
            start_response=start_response,
            status_code=400,
            payload={
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": "reason_required",
                    "message": "Local operator product-config requests require a reason.",
                },
            },
        )
    action = "product_config.apply" if request.mode == "apply" else "product_config.plan"
    if authz_policy.allows(
        identity=identity,
        action=action,
        product=request.product,
        context=request.context,
    ):
        return request, None
    return None, json_response(
        start_response=start_response,
        status_code=403,
        payload={
            "status": "rejected",
            "trace_id": trace_id,
            "error": {
                "code": "authorization_denied",
                "message": (
                    "Workflow cannot plan or apply product config for the requested"
                    " product/context."
                ),
            },
        },
    )


def apply_product_config_route(
    *,
    record_store: ProductConfigRouteStore,
    request: ProductConfigApplyEnvelope,
    actor: str,
    trace_id: str,
    json_response: JsonResponse,
    start_response: StartResponse,
) -> ProductConfigRouteResult | list[bytes]:
    driver_result, product_config_error = (
        control_plane_product_config_service.apply_product_config_service_request(
            record_store=record_store,
            payload=request.product_config_payload(),
            mode=cast(control_plane_product_config.ProductConfigMode, request.mode),
            actor=actor,
            source_label=request.source_label,
        )
    )
    if product_config_error is not None:
        return json_response(
            start_response=start_response,
            status_code=product_config_error.status_code,
            payload={
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": product_config_error.code,
                    "message": product_config_error.message,
                },
            },
        )
    next_actions = product_config_live_target_next_actions(
        request=request,
        driver_result=driver_result,
        tracked_targets=record_store.list_dokploy_target_records(),
    )
    if next_actions and driver_result is not None:
        driver_result = {
            **driver_result,
            "status": "records_applied_live_sync_required",
            "next_actions": next_actions,
        }
    return ProductConfigRouteResult(driver_result=driver_result)


def product_config_database_required_response(
    *,
    trace_id: str,
    json_response: JsonResponse,
    start_response: StartResponse,
) -> list[bytes]:
    return json_response(
        start_response=start_response,
        status_code=503,
        payload={
            "status": "rejected",
            "trace_id": trace_id,
            "error": {
                "code": "database_required",
                "message": "Product config apply requires DB-backed Launchplane storage.",
            },
        },
    )
