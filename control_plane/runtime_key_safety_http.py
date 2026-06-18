from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import runtime_key_safety_service as control_plane_runtime_key_safety_service
from control_plane.contracts.runtime_key_safety_policy import RuntimeSecretSafetyRule
from control_plane.runtime_key_safety_service import (
    RecordSlugProvider,
    RuntimeKeySafetyPolicyStore,
    TimestampProvider,
)


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
