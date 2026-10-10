from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane import provider_key_adoption
from control_plane.product_secret_copy import ProductSecretCopyFrom
from control_plane.contracts.runtime_environment_record import (
    RuntimeEnvironmentScope,
    ScalarValue,
    normalize_retired_provider_keys,
)
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyFinding,
    RuntimeKeySafetyTarget,
    RuntimeSecretClass,
)
from control_plane.contracts.secret_record import SecretScope
from control_plane.contracts.secret_record import SecretSharingKind
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
    ProductSecretConfigRequirement,
    product_config_requirement_applies_to_lane,
)


from control_plane.contracts.public_hosts import normalize_public_hosts

ProductConfigMode = Literal["dry-run", "apply"]

_ENV_KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


class ProductConfigRuntimeInput(BaseModel):
    model_config = ConfigDict(extra="allow")

    scope: RuntimeEnvironmentScope | None = None
    context: str | None = None
    instance: str | None = None
    env: dict[str, ScalarValue] = Field(default_factory=dict)
    retired_provider_keys: tuple[str, ...] | None = None
    adopt_provider_keys: tuple[str, ...] | None = None


class ProductConfigSharingReasonInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: SecretSharingKind
    reason: str
    evidence: str


class ProductConfigSecretInput(BaseModel):
    model_config = ConfigDict(extra="allow")

    scope: SecretScope | None = None
    context: str | None = None
    instance: str | None = None
    integration: str | None = None
    name: str | None = None
    binding_key: str | None = None
    value: str | None = Field(default=None, repr=False)
    copy_from: ProductSecretCopyFrom | None = None
    adopt_from_provider: Literal[True] | None = Field(
        default=None,
        description=(
            "Store the lane's current provider env value for binding_key; the service "
            "reads it, and no value is sent or returned."
        ),
    )
    description: str = ""
    secret_class: RuntimeSecretClass | None = None
    sharing_reason: ProductConfigSharingReasonInput | None = None

    @model_validator(mode="after")
    def _require_secret_identity(self) -> "ProductConfigSecretInput":
        if not (self.name or "").strip() and not (self.binding_key or "").strip():
            raise ValueError("Product config secrets require name or binding_key.")
        sources = (self.value, self.copy_from, self.adopt_from_provider)
        if sum(source is not None for source in sources) != 1:
            raise ValueError(
                "Product config secrets require exactly one value, copy_from or "
                "adopt_from_provider."
            )
        return self


class ProductEnvironmentManagedSecretInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_key: str
    integration: str
    value: str = Field(default="", repr=False)
    owner_submission_version_id: str = ""

    @model_validator(mode="after")
    def _validate_secret(self) -> "ProductEnvironmentManagedSecretInput":
        self.binding_key = self.binding_key.strip()
        self.integration = self.integration.strip()
        if not self.binding_key:
            raise ValueError("Managed secret input requires binding_key.")
        if not self.integration:
            raise ValueError("Managed secret input requires integration.")
        if bool(self.value.strip()) == bool(self.owner_submission_version_id.strip()):
            raise ValueError("Managed secret input requires a value or one Owner submission.")
        return self


class ProductEnvironmentConfigApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: ProductConfigMode
    reason: str = ""
    confirmation: str = ""
    runtime_settings: dict[str, ScalarValue] = Field(default_factory=dict)
    retired_provider_keys: list[str] = Field(
        default_factory=list,
        description="Provider keys to retire on this lane, added to those it already retires.",
    )
    managed_secrets: list[ProductEnvironmentManagedSecretInput] = Field(default_factory=list)

    @field_validator("mode", mode="before")
    @classmethod
    def _validate_mode(cls, value: object) -> ProductConfigMode:
        normalized_value = str(value).strip().lower()
        if normalized_value not in {"dry-run", "apply"}:
            raise ValueError("Product config mode must be 'dry-run' or 'apply'.")
        return cast(ProductConfigMode, normalized_value)

    @field_validator("retired_provider_keys", mode="before")
    @classmethod
    def _validate_retired_provider_keys(cls, value: object) -> list[str]:
        if isinstance(value, (list, tuple)):
            value = [key.strip() if isinstance(key, str) else key for key in value]
        return list(normalize_retired_provider_keys(value))

    @model_validator(mode="after")
    def _validate_input_boundary(self) -> "ProductEnvironmentConfigApplyEnvelope":
        self.reason = self.reason.strip()
        self.confirmation = self.confirmation.strip()
        normalized_runtime_settings: dict[str, ScalarValue] = {}
        for raw_key, value in self.runtime_settings.items():
            key = raw_key.strip()
            if not key:
                raise ValueError("Runtime setting keys must be non-empty.")
            if key in normalized_runtime_settings:
                raise ValueError("Runtime setting keys must be unique after normalization.")
            normalized_runtime_settings[key] = value
        self.runtime_settings = normalized_runtime_settings
        secret_keys = [(secret.integration, secret.binding_key) for secret in self.managed_secrets]
        if len(secret_keys) != len(set(secret_keys)):
            raise ValueError("Managed secret integration and binding keys must be unique.")
        runtime_change = bool(self.runtime_settings or self.retired_provider_keys)
        if runtime_change == bool(self.managed_secrets):
            raise ValueError(
                "Product environment config requests must contain runtime settings or managed "
                "secrets, but not both."
            )
        return self


class ProductConfigRuntimeEnvironmentRecordSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: RuntimeEnvironmentScope
    context: str
    instance: str
    updated_at: str
    source_label: str
    env_keys: list[str]
    env_value_count: int = Field(ge=0)
    retired_provider_keys: list[str] = Field(default_factory=list)


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
    retired_provider_keys_before: list[str] = Field(default_factory=list)
    retired_provider_keys_after: list[str] = Field(default_factory=list)
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
    copy_from: ProductSecretCopyFrom | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    sharing_reason: dict[str, str] | None = Field(
        default=None, json_schema_extra={"x-launchplane-optional-response": True}
    )
    secret_class: RuntimeSecretClass | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


class ProductConfigProviderKeyAdoptionResult(BaseModel):
    """One key named for adoption and what the service decided; never its value."""

    model_config = ConfigDict(extra="forbid")

    key: str
    disposition: Literal[
        "adopted",
        "template_default",
        "already_recorded",
        "refused_credential",
        "missing",
    ]


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


class ProductConfigPublicHostsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    before: list[str]
    plan_digest: str
    after: list[str]
    added: list[str]
    updated: list[str]
    removed: list[str]
    unchanged: list[str]
    runtime_port: int = Field(ge=1, le=65535)
    https: Literal[True] = True
    service_name: Literal["web"] = "web"
    certificate_type: Literal["none"] = "none"
    verified: bool
    read_back_hosts: list[str]


class ProductConfigApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "records_applied_live_sync_required"]
    mode: ProductConfigMode
    product: str
    context: str
    instance: str
    actor: str
    source_label: str
    reason: str = ""
    runtime_environment: ProductConfigRuntimeEnvironmentResult
    runtime_key_safety: ProductConfigRuntimeKeySafetyResult
    secrets: list[ProductConfigSecretResult]
    provider_key_adoption: list[ProductConfigProviderKeyAdoptionResult] = Field(
        default_factory=list
    )
    summary: ProductConfigApplySummary
    next_actions: list[ProductConfigLiveTargetRuntimeNextAction] = Field(default_factory=list)
    public_hosts: ProductConfigPublicHostsResult | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


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
    confirmation: str = ""
    runtime_env: dict[str, ScalarValue] | ProductConfigRuntimeInput | None = Field(
        default=None,
        union_mode="left_to_right",
    )
    runtime_environment: dict[str, ScalarValue] | ProductConfigRuntimeInput | None = Field(
        default=None,
        union_mode="left_to_right",
    )
    secrets: list[ProductConfigSecretInput] = Field(default_factory=list)
    public_hosts: tuple[str, ...] | None = None

    @field_validator("public_hosts", mode="before")
    @classmethod
    def _validate_public_hosts(cls, value: object) -> tuple[str, ...]:
        return normalize_public_hosts(value)

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
        self.confirmation = self.confirmation.strip()
        if not self.product:
            raise ValueError("Product config apply requires product.")
        if self.public_hosts is not None and (self.instance != "prod" or not self.context):
            raise ValueError("Public hosts require an exact prod lane.")
        return self

    def adopts_provider_keys(self) -> bool:
        return any(
            isinstance(runtime_input, ProductConfigRuntimeInput)
            and runtime_input.adopt_provider_keys is not None
            for runtime_input in (self.runtime_env, self.runtime_environment)
        )

    def adopts_provider_secrets(self) -> bool:
        return any(secret.adopt_from_provider for secret in self.secrets)

    def reads_lane_provider_env(self) -> bool:
        return self.adopts_provider_keys() or self.adopts_provider_secrets()

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
        if self.public_hosts is not None:
            payload["public_hosts"] = list(self.public_hosts)
        return payload


def _runtime_input_payload(
    value: dict[str, ScalarValue] | ProductConfigRuntimeInput,
) -> dict[str, object]:
    if isinstance(value, ProductConfigRuntimeInput):
        return value.model_dump(exclude_none=True, exclude_unset=True)
    return dict(value)


def product_environment_config_confirmation(*, product: str, environment: str) -> str:
    return f"APPLY {product.strip()}/{environment.strip()}"


class ProductEnvironmentConfigRefused(ValueError):
    """The form request names a site setting the service will not record; names only."""

    def __init__(self, message: str, *, code: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def product_config_has_undeclared_runtime_settings(
    *, profile: LaunchplaneProductProfileRecord, payload: dict[str, object]
) -> bool:
    lane = next(
        (
            lane
            for lane in profile.lanes
            if lane.context.strip() == payload["context"]
            and lane.instance.strip() == payload["instance"]
        ),
        None,
    )
    declared = {
        requirement.key
        for requirement in profile.expected_config.runtime_environment_keys
        if lane is not None
        and product_config_requirement_applies_to_lane(
            requirement_context=requirement.context,
            requirement_instance=requirement.instance,
            lane=lane,
        )
    }
    # The shared normalizer owns flat input, aliases, extra fields and target keys.
    runtime_input = cast(dict[str, object], payload["runtime_env"])
    values = cast(dict[str, ScalarValue], runtime_input["env"])
    adopted_keys = cast(tuple[str, ...], runtime_input.get("adopt_provider_keys") or ())
    return bool((set(values) | set(adopted_keys)) - declared)


def product_environment_config_apply_request(
    *,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    request: ProductEnvironmentConfigApplyEnvelope,
    owner_submission_resolver: Callable[[ProductSecretConfigRequirement, str], str] | None = None,
    current_retired_provider_keys: tuple[str, ...] = (),
) -> ProductConfigApplyEnvelope:
    runtime_requirements = {
        requirement.key
        for requirement in profile.expected_config.runtime_environment_keys
        if product_config_requirement_applies_to_lane(
            requirement_context=requirement.context,
            requirement_instance=requirement.instance,
            lane=lane,
        )
    }

    secret_requirements = {
        (requirement.integration, requirement.binding_key): requirement
        for requirement in profile.expected_config.managed_secret_bindings
        if product_config_requirement_applies_to_lane(
            requirement_context=requirement.context,
            requirement_instance=requirement.instance,
            lane=lane,
        )
    }
    unknown_secret_keys = sorted(
        {(secret.integration, secret.binding_key) for secret in request.managed_secrets}
        - set(secret_requirements)
    )
    if unknown_secret_keys:
        raise ValueError("Managed secrets contain bindings not declared for this environment.")

    # A site's own settings need no declaration (#2467); no declaration vouches for them,
    # so each must be a plain setting rather than a credential.
    site_setting_keys = sorted(set(request.runtime_settings) - runtime_requirements)
    secret_binding_keys = {binding_key for _, binding_key in secret_requirements}
    for key in site_setting_keys:
        if (
            not _ENV_KEY_PATTERN.fullmatch(key)
            or key in secret_binding_keys
            or provider_key_adoption.looks_like_credential(key, "")
        ):
            raise ProductEnvironmentConfigRefused(
                f"Site setting {key!r} must be an env key name holding a plain setting; "
                "write credentials as managed secrets.",
                code="runtime_setting_refused",
            )

    secrets = []
    for secret in request.managed_secrets:
        requirement = secret_requirements[(secret.integration, secret.binding_key)]
        value = secret.value
        if secret.owner_submission_version_id:
            if owner_submission_resolver is None:
                raise ValueError("Owner submissions require the authorized service resolver.")
            value = owner_submission_resolver(requirement, secret.owner_submission_version_id)
        secrets.append(
            _product_config_secret_input(
                requirement=secret_requirements[(secret.integration, secret.binding_key)],
                lane=lane,
                value=value,
            )
        )
    runtime_env = None
    retired_provider_keys = None
    if request.retired_provider_keys:
        retired_provider_keys = tuple(
            sorted(set(current_retired_provider_keys) | set(request.retired_provider_keys))
        )
    if request.runtime_settings or retired_provider_keys is not None:
        runtime_env = ProductConfigRuntimeInput(
            scope="instance",
            context=lane.context,
            instance=lane.instance,
            env=request.runtime_settings,
            **(
                {"retired_provider_keys": retired_provider_keys}
                if retired_provider_keys is not None
                else {}
            ),
        )
    return ProductConfigApplyEnvelope(
        # Retirement is a product-config schema v2 field.
        schema_version=2 if retired_provider_keys is not None else request.schema_version,
        mode=request.mode,
        product=profile.product,
        context=lane.context,
        instance=lane.instance,
        source_label="product-environment-api",
        reason=request.reason,
        confirmation=request.confirmation,
        runtime_env=runtime_env,
        secrets=secrets,
    )


def _product_config_secret_input(
    *,
    requirement: ProductSecretConfigRequirement,
    lane: ProductLaneProfile,
    value: str,
) -> ProductConfigSecretInput:
    return ProductConfigSecretInput(
        scope="context_instance",
        context=lane.context,
        instance=lane.instance,
        integration=requirement.integration,
        name=requirement.binding_key,
        binding_key=requirement.binding_key,
        value=value,
    )


def product_config_live_target_next_actions(
    *,
    request: ProductConfigApplyEnvelope,
    driver_result: dict[str, object] | None,
    tracked_targets: tuple[DokployTargetRecord, ...],
) -> list[dict[str, object]]:
    if not driver_result:
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
