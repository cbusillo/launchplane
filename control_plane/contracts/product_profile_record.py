from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.product_health_monitoring_migration import (
    canonical_health_check_record_token,
)
from control_plane.contracts.product_health_monitoring_migration import health_check_record_token


PRODUCT_PREVIEW_DEFAULT_ENABLE_LABEL = "launchplane-preview"
OdooDataAuthority = Literal["unknown", "resettable", "restorable", "authoritative"]
OdooRebuildSourceMode = Literal["empty", "upstream_restore"]


class ProductImageProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = ""

    @model_validator(mode="after")
    def _validate_image(self) -> "ProductImageProfile":
        self.repository = self.repository.strip()
        return self


class ProductOdooStableBootstrapPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    approval_issue_url: str = ""
    data_source_mode: Literal["empty"] = "empty"
    confirmation: str = ""
    expected_target_name: str = ""
    expected_domains: tuple[str, ...] = ()
    require_health_verification: bool = True
    require_canonical_verification: bool = True
    require_logo_verification: bool = True

    @model_validator(mode="after")
    def _validate_policy(self) -> "ProductOdooStableBootstrapPolicy":
        self.approval_issue_url = self.approval_issue_url.strip()
        self.confirmation = self.confirmation.strip().lower()
        self.expected_target_name = self.expected_target_name.strip()
        normalized_domains: list[str] = []
        for raw_domain in self.expected_domains:
            domain = (
                raw_domain.strip()
                .lower()
                .removeprefix("https://")
                .removeprefix("http://")
                .rstrip("/")
            )
            if not domain:
                raise ValueError(
                    "Odoo stable bootstrap policy expected_domains values must be non-empty"
                )
            if domain not in normalized_domains:
                normalized_domains.append(domain)
        self.expected_domains = tuple(normalized_domains)
        if self.enabled:
            if not self.approval_issue_url:
                raise ValueError("enabled Odoo stable bootstrap policy requires approval_issue_url")
            if not self.confirmation:
                raise ValueError("enabled Odoo stable bootstrap policy requires confirmation")
            if not self.expected_target_name:
                raise ValueError(
                    "enabled Odoo stable bootstrap policy requires expected_target_name"
                )
            if not self.expected_domains:
                raise ValueError("enabled Odoo stable bootstrap policy requires expected_domains")
        return self


class ProductOdooPrelaunchRebuildPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    approval_issue_url: str = ""
    data_source_mode: Literal["empty", "upstream_restore"] = "empty"
    confirmation: str = ""
    expected_target_name: str = ""
    expected_domains: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_policy(self) -> "ProductOdooPrelaunchRebuildPolicy":
        self.approval_issue_url = self.approval_issue_url.strip()
        self.confirmation = self.confirmation.strip().lower()
        self.expected_target_name = self.expected_target_name.strip()
        normalized_domains: list[str] = []
        for raw_domain in self.expected_domains:
            domain = (
                raw_domain.strip()
                .lower()
                .removeprefix("https://")
                .removeprefix("http://")
                .rstrip("/")
            )
            if not domain:
                raise ValueError(
                    "Odoo prelaunch rebuild policy expected_domains values must be non-empty"
                )
            if domain not in normalized_domains:
                normalized_domains.append(domain)
        self.expected_domains = tuple(normalized_domains)
        if self.enabled:
            if not self.approval_issue_url:
                raise ValueError(
                    "enabled Odoo prelaunch rebuild policy requires approval_issue_url"
                )
            if not self.confirmation:
                raise ValueError("enabled Odoo prelaunch rebuild policy requires confirmation")
            if not self.expected_target_name:
                raise ValueError(
                    "enabled Odoo prelaunch rebuild policy requires expected_target_name"
                )
            if not self.expected_domains:
                raise ValueError("enabled Odoo prelaunch rebuild policy requires expected_domains")
        return self


class ProductOdooLaneDataPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_authority: OdooDataAuthority = "unknown"
    allowed_rebuild_sources: tuple[OdooRebuildSourceMode, ...] = ()
    upstream_source: str = ""
    requires_backup_before_destroy: bool = True
    requires_restore_proof: bool = True
    requires_runtime_identity: bool = True

    @model_validator(mode="after")
    def _validate_policy(self) -> "ProductOdooLaneDataPolicy":
        normalized_sources: list[OdooRebuildSourceMode] = []
        for source in self.allowed_rebuild_sources:
            if source not in normalized_sources:
                normalized_sources.append(source)
        self.allowed_rebuild_sources = tuple(normalized_sources)
        self.upstream_source = self.upstream_source.strip()
        if self.data_authority == "unknown" and self.allowed_rebuild_sources:
            raise ValueError("unknown Odoo data authority cannot allow rebuild sources")
        if "upstream_restore" in self.allowed_rebuild_sources and not self.upstream_source:
            raise ValueError("Odoo data policy allowing upstream_restore requires upstream_source")
        if self.data_authority == "authoritative" and not self.requires_backup_before_destroy:
            raise ValueError("authoritative Odoo data policy requires backup before destroy")
        if self.data_authority == "authoritative" and not self.requires_restore_proof:
            raise ValueError("authoritative Odoo data policy requires restore proof")
        return self

    def allows_rebuild_source(self, source: str) -> bool:
        return source in self.allowed_rebuild_sources


ProductLaneHealthCheckKind = Literal["public_http", "private_http", "provider"]


class ProductLaneHealthCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: ProductLaneHealthCheckKind = "public_http"
    enabled: bool = True
    url: str = ""
    private_endpoint_key: str = ""
    require_runtime_identity: bool = False
    alert_issue_url: str = ""
    provider: str = ""
    provider_check: str = ""

    @model_validator(mode="after")
    def _validate_check(self) -> "ProductLaneHealthCheck":
        self.name = self.name.strip()
        self.url = self.url.strip()
        self.alert_issue_url = self.alert_issue_url.strip()
        self.provider = self.provider.strip()
        self.provider_check = self.provider_check.strip()
        if not self.name:
            raise ValueError("product lane health check requires name")
        self.private_endpoint_key = self.private_endpoint_key.strip()
        if self.kind == "private_http" and self.url:
            raise ValueError("private HTTP health checks must use private_endpoint_key")
        if not health_check_record_token(self.name):
            raise ValueError(
                "product lane health check name must contain at least one alphanumeric character"
            )
        if self.kind != "public_http" and canonical_health_check_record_token(self.name) == "":
            raise ValueError("non-public health checks cannot use the reserved public-ingress name")
        if self.kind in {"public_http", "private_http"}:
            if self.provider or self.provider_check:
                raise ValueError("HTTP health checks cannot set provider fields")
            if self.kind != "private_http" and self.private_endpoint_key:
                raise ValueError("only private HTTP health checks can set private_endpoint_key")
        elif self.kind == "provider":
            if self.url:
                raise ValueError("provider health checks cannot set url")
            if self.private_endpoint_key:
                raise ValueError("provider health checks cannot set private_endpoint_key")
            if not self.provider:
                raise ValueError("provider health check requires provider")
            if not self.provider_check:
                raise ValueError("provider health check requires provider_check")
        return self


class ProductLaneHealthMonitoringPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checks: tuple[ProductLaneHealthCheck, ...] = ()

    @model_validator(mode="after")
    def _validate_policy(self) -> "ProductLaneHealthMonitoringPolicy":
        tokens: list[str] = []
        for check in self.checks:
            token = canonical_health_check_record_token(check.name)
            if token in tokens:
                raise ValueError("product lane health check names must be unique")
            tokens.append(token)
        return self


class ProductLaneProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance: str
    context: str
    base_url: str = ""
    health_url: str = ""
    odoo_stable_bootstrap: ProductOdooStableBootstrapPolicy = Field(
        default_factory=ProductOdooStableBootstrapPolicy
    )
    odoo_prelaunch_rebuild: ProductOdooPrelaunchRebuildPolicy = Field(
        default_factory=ProductOdooPrelaunchRebuildPolicy
    )
    odoo_data_policy: ProductOdooLaneDataPolicy = Field(default_factory=ProductOdooLaneDataPolicy)
    health_monitoring: ProductLaneHealthMonitoringPolicy = Field(
        default_factory=ProductLaneHealthMonitoringPolicy
    )

    @model_validator(mode="after")
    def _validate_lane(self) -> "ProductLaneProfile":
        if not self.instance.strip():
            raise ValueError("product lane profile requires instance")
        if not self.context.strip():
            raise ValueError("product lane profile requires context")
        return self


class ProductPreviewProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    context: str = ""
    enable_label: str = PRODUCT_PREVIEW_DEFAULT_ENABLE_LABEL
    slug_template: str = "pr-{number}"
    app_name_prefix: str = ""
    template_instance: str = "testing"
    required_template_env_keys: tuple[str, ...] = ()
    copied_env_keys: tuple[str, ...] = ()
    omitted_env_keys: tuple[str, ...] = ()
    override_env: dict[str, str] = Field(default_factory=dict)
    preview_url_env_keys: tuple[str, ...] = ()
    preview_domain_env_keys: tuple[str, ...] = ()
    required_provider_fields: tuple[str, ...] = ()
    data_transport_mode: Literal["none", "clone", "bootstrap", "migrate_seed", "driver"] = "none"
    migration_command: str = ""
    seed_command: str = ""

    @model_validator(mode="after")
    def _validate_preview(self) -> "ProductPreviewProfile":
        if self.enabled and not self.context.strip():
            raise ValueError("enabled product preview profile requires context")
        enable_label = self.enable_label.strip() or PRODUCT_PREVIEW_DEFAULT_ENABLE_LABEL
        if self.enabled and not enable_label:
            raise ValueError("enabled product preview profile requires enable_label")
        self.enable_label = enable_label
        if self.enabled and "{number}" not in self.slug_template:
            raise ValueError("enabled product preview profile slug_template requires {number}")
        if self.enabled and not self.template_instance.strip():
            raise ValueError("enabled product preview profile requires template_instance")
        key_fields = {
            "required_template_env_keys": self.required_template_env_keys,
            "copied_env_keys": self.copied_env_keys,
            "omitted_env_keys": self.omitted_env_keys,
            "preview_url_env_keys": self.preview_url_env_keys,
            "preview_domain_env_keys": self.preview_domain_env_keys,
            "required_provider_fields": self.required_provider_fields,
        }
        normalized: dict[str, tuple[str, ...]] = {}
        for field_name, raw_keys in key_fields.items():
            keys: list[str] = []
            for raw_key in raw_keys:
                key = raw_key.strip()
                if not key:
                    raise ValueError(
                        f"product preview profile {field_name} values must be non-empty"
                    )
                if key in keys:
                    raise ValueError(f"product preview profile {field_name} values must be unique")
                keys.append(key)
            normalized[field_name] = tuple(keys)
        copied = set(normalized["copied_env_keys"])
        omitted = set(normalized["omitted_env_keys"])
        overlap = sorted(copied & omitted)
        if overlap:
            raise ValueError(
                "product preview profile cannot both copy and omit env keys: " + ", ".join(overlap)
            )
        for raw_key, raw_value in self.override_env.items():
            key = raw_key.strip()
            if not key:
                raise ValueError("product preview profile override_env keys must be non-empty")
            if raw_value is None:
                raise ValueError("product preview profile override_env values must not be null")
        if self.data_transport_mode == "none" and (self.migration_command or self.seed_command):
            raise ValueError(
                "product preview profile migration_command/seed_command require a data transport mode"
            )
        return self


class ProductPromotionWorkflowProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str = "promote-prod.yml"
    ref: str = "main"
    dry_run_input: str = "dry_run"
    bump_input: str = "bump"
    default_bump: str = "patch"

    @model_validator(mode="after")
    def _validate_workflow(self) -> "ProductPromotionWorkflowProfile":
        if not self.workflow_id.strip():
            raise ValueError("product promotion workflow requires workflow_id")
        if not self.ref.strip():
            raise ValueError("product promotion workflow requires ref")
        if not self.dry_run_input.strip():
            raise ValueError("product promotion workflow requires dry_run_input")
        if not self.bump_input.strip():
            raise ValueError("product promotion workflow requires bump_input")
        if self.default_bump.strip() not in {"patch", "minor", "major"}:
            raise ValueError(
                "product promotion workflow default_bump must be patch, minor, or major"
            )
        return self


class ProductRuntimeConfigRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    context: str = ""
    instance: str = ""

    @model_validator(mode="after")
    def _validate_requirement(self) -> "ProductRuntimeConfigRequirement":
        if not self.key.strip():
            raise ValueError("product runtime config requirement requires key")
        if self.instance.strip() and not self.context.strip():
            raise ValueError("instance runtime config requirement requires context")
        self.key = self.key.strip()
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        return self


class ProductSecretConfigRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_key: str
    integration: str = "runtime_environment"
    context: str = ""
    instance: str = ""

    @model_validator(mode="after")
    def _validate_requirement(self) -> "ProductSecretConfigRequirement":
        if not self.binding_key.strip():
            raise ValueError("product secret config requirement requires binding_key")
        if not self.integration.strip():
            raise ValueError("product secret config requirement requires integration")
        if self.instance.strip() and not self.context.strip():
            raise ValueError("instance secret config requirement requires context")
        self.binding_key = self.binding_key.strip()
        self.integration = self.integration.strip()
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        return self


class ProductExpectedConfigProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime_environment_keys: tuple[ProductRuntimeConfigRequirement, ...] = ()
    managed_secret_bindings: tuple[ProductSecretConfigRequirement, ...] = ()

    @model_validator(mode="after")
    def _validate_expected_config(self) -> "ProductExpectedConfigProfile":
        runtime_keys = [
            (requirement.context, requirement.instance, requirement.key)
            for requirement in self.runtime_environment_keys
        ]
        if len(runtime_keys) != len(set(runtime_keys)):
            raise ValueError("product expected runtime config keys must be unique")
        secret_keys = [
            (
                requirement.integration,
                requirement.context,
                requirement.instance,
                requirement.binding_key,
            )
            for requirement in self.managed_secret_bindings
        ]
        if len(secret_keys) != len(set(secret_keys)):
            raise ValueError("product expected secret config bindings must be unique")
        return self


class LaunchplaneProductProfileRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    display_name: str
    repository: str
    driver_id: str
    image: ProductImageProfile
    runtime_port: int = Field(default=0, ge=0, le=65535)
    health_path: str = ""
    lanes: tuple[ProductLaneProfile, ...] = ()
    historical_contexts: tuple[str, ...] = ()
    preview: ProductPreviewProfile = Field(default_factory=ProductPreviewProfile)
    promotion_workflow: ProductPromotionWorkflowProfile = Field(
        default_factory=ProductPromotionWorkflowProfile
    )
    expected_config: ProductExpectedConfigProfile = Field(
        default_factory=ProductExpectedConfigProfile
    )
    updated_at: str
    source: str

    @model_validator(mode="after")
    def _validate_record(self) -> "LaunchplaneProductProfileRecord":
        if not self.product.strip():
            raise ValueError("product profile requires product")
        if not self.display_name.strip():
            raise ValueError("product profile requires display_name")
        if not self.repository.strip():
            raise ValueError("product profile requires repository")
        if not self.driver_id.strip():
            raise ValueError("product profile requires driver_id")
        self.health_path = self.health_path.strip()
        if self.health_path and not self.health_path.startswith("/"):
            raise ValueError("product profile health_path must start with /")
        if self.runtime_port == 0 and self.health_path:
            raise ValueError("product profile with runtime_port=0 cannot set health_path")
        if self.runtime_port > 0 and not self.health_path:
            raise ValueError("product profile with runtime_port requires health_path")
        if self.preview.enabled:
            if not self.image.repository.strip():
                raise ValueError("enabled product preview requires image repository")
            if self.runtime_port <= 0:
                raise ValueError("enabled product preview requires runtime_port")
            if not self.health_path:
                raise ValueError("enabled product preview requires health_path")
        if not self.updated_at.strip():
            raise ValueError("product profile requires updated_at")
        if not self.source.strip():
            raise ValueError("product profile requires source")
        normalized_historical_contexts: list[str] = []
        for raw_context in self.historical_contexts:
            context = raw_context.strip()
            if not context:
                raise ValueError("product profile historical_contexts values must be non-empty")
            if context not in normalized_historical_contexts:
                normalized_historical_contexts.append(context)
        self.historical_contexts = tuple(normalized_historical_contexts)
        return self

    def validate_write_contract(self) -> "LaunchplaneProductProfileRecord":
        for lane in self.lanes:
            for check in lane.health_monitoring.checks:
                if not check.enabled:
                    continue
                if check.kind == "provider":
                    continue
                if check.kind == "private_http":
                    if check.private_endpoint_key:
                        continue
                    raise ValueError("private HTTP health check requires private_endpoint_key")
                if check.url:
                    continue
                if lane.health_url.strip():
                    continue
                if not lane.base_url.strip():
                    raise ValueError(
                        "public HTTP health check requires base_url or explicit health_url"
                    )
                if not self.health_path:
                    raise ValueError("public HTTP health check with base_url requires health_path")
        return self
