from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import runtime_key_safety_service as control_plane_runtime_key_safety_service
from control_plane.contracts.runtime_key_safety_policy import RuntimeSecretSafetyRule
from control_plane.runtime_key_safety_service import RuntimeKeySafetyPolicyStore
from control_plane.service_auth import LaunchplaneAuthzPolicy, LaunchplaneIdentity


JsonResponse = Callable[..., list[bytes]]
RecordSlugProvider = Callable[[str], str]
StartResponse = Callable[[str, list[tuple[str, str]]], None]
TimestampProvider = Callable[[], str]

LAUNCHPLANE_SERVICE_CONTEXT = "launchplane"


class RuntimeKeySafetyPolicyApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    source_label: str = "service:runtime-key-safety-policy"
    rules: tuple[RuntimeSecretSafetyRule, ...]

    @model_validator(mode="after")
    def _validate_alignment(self) -> "RuntimeKeySafetyPolicyApplyEnvelope":
        if self.product.strip() != "launchplane":
            raise ValueError("Runtime key-safety policy writes require product 'launchplane'.")
        self.product = "launchplane"
        self.source_label = self.source_label.strip() or "service:runtime-key-safety-policy"
        if not self.rules:
            raise ValueError("Runtime key-safety policy writes require at least one rule.")
        return self


@dataclass(frozen=True)
class RuntimeKeySafetyPolicyRouteResult:
    result: dict[str, object]
    driver_result: dict[str, object]


def validate_runtime_key_safety_policy_request(
    *,
    authz_policy: LaunchplaneAuthzPolicy,
    identity: LaunchplaneIdentity,
    payload: dict[str, object],
    trace_id: str,
    json_response: JsonResponse,
    start_response: StartResponse,
) -> tuple[RuntimeKeySafetyPolicyApplyEnvelope | None, list[bytes] | None]:
    request = RuntimeKeySafetyPolicyApplyEnvelope.model_validate(payload)
    if _runtime_key_safety_policy_authorized(
        authz_policy=authz_policy,
        identity=identity,
        product=request.product,
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
                "message": "Workflow cannot write Launchplane runtime key-safety policy records.",
            },
        },
    )


def apply_runtime_key_safety_policy_route(
    *,
    record_store: RuntimeKeySafetyPolicyStore,
    request: RuntimeKeySafetyPolicyApplyEnvelope,
    now_timestamp: TimestampProvider,
    record_slug: RecordSlugProvider,
) -> RuntimeKeySafetyPolicyRouteResult:
    runtime_policy_record, changed = (
        control_plane_runtime_key_safety_service.write_runtime_key_safety_policy(
            record_store=record_store,
            rules=request.rules,
            source_label=request.source_label,
            now_timestamp=now_timestamp,
            record_slug=record_slug,
        )
    )
    return RuntimeKeySafetyPolicyRouteResult(
        result={
            "runtime_key_safety_policy_record_id": runtime_policy_record.record_id,
            "runtime_key_safety_policy_changed": str(changed).lower(),
        },
        driver_result={
            "runtime_key_safety_policy": control_plane_runtime_key_safety_service.summarize_runtime_key_safety_policy_record(
                runtime_policy_record
            ),
            "changed": changed,
        },
    )


def runtime_key_safety_database_required_response(
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
                "message": "Runtime key-safety policy writes require Launchplane database storage.",
            },
        },
    )


def _runtime_key_safety_policy_authorized(
    *,
    authz_policy: LaunchplaneAuthzPolicy,
    identity: LaunchplaneIdentity,
    product: str,
) -> bool:
    return authz_policy.allows(
        identity=identity,
        action="runtime_key_safety.write",
        product=product,
        context=LAUNCHPLANE_SERVICE_CONTEXT,
    )
