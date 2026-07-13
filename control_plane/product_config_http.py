from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.runtime_environment_record import (
    RuntimeEnvironmentScope,
    ScalarValue,
)
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyFinding,
    RuntimeKeySafetyTarget,
)
from control_plane.contracts.secret_record import SecretScope


ProductConfigMode = Literal["dry-run", "apply"]


class ProductConfigRuntimeInput(BaseModel):
    model_config = ConfigDict(extra="allow")

    scope: RuntimeEnvironmentScope | None = None
    context: str | None = None
    instance: str | None = None
    env: dict[str, ScalarValue] = Field(default_factory=dict)


class ProductConfigSecretInput(BaseModel):
    model_config = ConfigDict(extra="allow")

    scope: SecretScope | None = None
    context: str | None = None
    instance: str | None = None
    integration: str | None = None
    name: str
    binding_key: str
    value: str
    description: str = ""


class ProductConfigRuntimeEnvironmentRecordSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: RuntimeEnvironmentScope
    context: str
    instance: str
    updated_at: str
    source_label: str
    env_keys: list[str]
    env_value_count: int = Field(ge=0)


class ProductConfigRuntimeEnvironmentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["skipped", "created", "updated", "unchanged"]
    scope: RuntimeEnvironmentScope
    context: str
    instance: str
    keys: list[str]
    changed_keys: list[str]
    unchanged_keys: list[str]
    env_value_count_after: int = Field(ge=0)
    record: ProductConfigRuntimeEnvironmentRecordSummary | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


class ProductConfigRuntimeKeySafetyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: bool
    status: Literal["skipped", "pass"]
    policy_record_id: str = ""
    policy_sha256: str = ""
    target: RuntimeKeySafetyTarget | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    checked_binding_keys: list[str] = Field(default_factory=list)
    findings: list[RuntimeKeySafetyFinding] = Field(default_factory=list)


class ProductConfigSecretResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["created", "rotated", "unchanged"]
    scope: SecretScope
    integration: str
    name: str
    binding_key: str
    context: str
    instance: str
    secret_id: str = ""


class ProductConfigApplySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime_changed_key_count: int = Field(ge=0)
    secret_change_count: int = Field(ge=0)


class ProductConfigLiveTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    instance: str
    target_type: str
    target_name: str


class ProductConfigLiveTargetRuntimeOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["POST"]
    endpoint: str
    mode: ProductConfigMode


class ProductConfigLiveTargetRuntimeNextAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["live_target_runtime_apply"]
    required: bool
    status: Literal["live_sync_required"]
    target: ProductConfigLiveTarget
    changed_keys: list[str]
    dry_run: ProductConfigLiveTargetRuntimeOperation
    apply: ProductConfigLiveTargetRuntimeOperation
    instruction: str


class ProductConfigApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "records_applied_live_sync_required"]
    mode: ProductConfigMode
    product: str
    context: str
    instance: str
    actor: str
    source_label: str
    runtime_environment: ProductConfigRuntimeEnvironmentResult
    runtime_key_safety: ProductConfigRuntimeKeySafetyResult
    secrets: list[ProductConfigSecretResult]
    summary: ProductConfigApplySummary
    next_actions: list[ProductConfigLiveTargetRuntimeNextAction] = Field(default_factory=list)


class ProductConfigApplyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    trace_id: str
    records: dict[str, str] = Field(default_factory=dict)
    result: ProductConfigApplyResult
    replayed: bool | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    original_trace_id: str | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


class ProductConfigApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: ProductConfigMode
    product: str
    context: str = ""
    instance: str = ""
    source_label: str = "product-config-api"
    reason: str = ""
    runtime_env: dict[str, ScalarValue] | ProductConfigRuntimeInput | None = Field(
        default=None,
        union_mode="left_to_right",
    )
    runtime_environment: dict[str, ScalarValue] | ProductConfigRuntimeInput | None = Field(
        default=None,
        union_mode="left_to_right",
    )
    secrets: list[ProductConfigSecretInput] = Field(default_factory=list)

    @field_validator("mode", mode="before")
    @classmethod
    def _validate_mode(cls, value: object) -> ProductConfigMode:
        normalized_value = str(value).strip().lower()
        if normalized_value not in {"dry-run", "apply"}:
            raise ValueError("Product config mode must be 'dry-run' or 'apply'.")
        return cast(ProductConfigMode, normalized_value)

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
            "secrets": [secret.model_dump(exclude_none=True) for secret in self.secrets],
        }
        if self.runtime_env is not None:
            payload["runtime_env"] = _runtime_input_payload(self.runtime_env)
        if self.runtime_environment is not None:
            payload["runtime_environment"] = _runtime_input_payload(self.runtime_environment)
        return payload


def _runtime_input_payload(
    value: dict[str, ScalarValue] | ProductConfigRuntimeInput,
) -> dict[str, object]:
    if isinstance(value, ProductConfigRuntimeInput):
        return value.model_dump(exclude_none=True)
    return dict(value)


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
