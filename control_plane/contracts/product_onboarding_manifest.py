from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.dokploy_target_record import DokployTargetType
from control_plane.contracts.product_profile_record import (
    ProductPreviewProfile,
    ProductPromotionWorkflowProfile,
)
from control_plane.contracts.runtime_environment_record import ScalarValue
from control_plane.contracts.secret_record import SecretStatus


class ProductOnboardingLaneManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance: str
    context: str
    base_url: str = ""
    health_url: str = ""

    @model_validator(mode="after")
    def _validate_lane(self) -> "ProductOnboardingLaneManifest":
        if not self.instance.strip():
            raise ValueError("product onboarding lane requires instance")
        if not self.context.strip():
            raise ValueError("product onboarding lane requires context")
        return self


class ProductOnboardingPreviewManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    context: str = ""
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


class ProductOnboardingTargetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    instance: str
    target_id: str = ""
    project_name: str = ""
    target_type: DokployTargetType = "application"
    target_name: str = ""
    source_git_ref: str = "origin/main"
    source_type: str = ""
    custom_git_url: str = ""
    custom_git_branch: str = ""
    compose_path: str = ""
    watch_paths: tuple[str, ...] = ()
    enable_submodules: bool | None = None
    require_test_gate: bool = False
    require_prod_gate: bool = False
    deploy_timeout_seconds: int | None = Field(default=None, ge=1)
    healthcheck_enabled: bool = True
    healthcheck_path: str = ""
    healthcheck_timeout_seconds: int | None = Field(default=None, ge=1)
    env: dict[str, str] = Field(default_factory=dict)
    domains: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_target(self) -> "ProductOnboardingTargetManifest":
        if not self.context.strip():
            raise ValueError("product onboarding target requires context")
        if not self.instance.strip():
            raise ValueError("product onboarding target requires instance")
        if self.healthcheck_path and not self.healthcheck_path.startswith("/"):
            raise ValueError("product onboarding target healthcheck_path must start with /")
        return self


class ProductOnboardingRuntimeEnvironmentManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["global", "context", "instance"]
    context: str = ""
    instance: str = ""
    env: dict[str, ScalarValue]


class ProductOnboardingSecretBindingManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_key: str
    context: str
    instance: str = ""
    integration: str = "runtime_environment"
    secret_id: str = ""
    binding_id: str = ""
    status: SecretStatus = "disabled"

    @model_validator(mode="after")
    def _validate_binding(self) -> "ProductOnboardingSecretBindingManifest":
        if not self.binding_key.strip():
            raise ValueError("product onboarding secret binding requires binding_key")
        if not self.context.strip():
            raise ValueError("product onboarding secret binding requires context")
        if not self.integration.strip():
            raise ValueError("product onboarding secret binding requires integration")
        return self


class ProductOnboardingManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    display_name: str
    repository: str
    driver_id: str = "generic-web"
    image_repository: str
    runtime_port: int = Field(ge=1, le=65535)
    health_path: str
    lanes: tuple[ProductOnboardingLaneManifest, ...]
    preview: ProductOnboardingPreviewManifest = Field(
        default_factory=ProductOnboardingPreviewManifest
    )
    promotion_workflow: ProductPromotionWorkflowProfile = Field(
        default_factory=ProductPromotionWorkflowProfile
    )
    dokploy_targets: tuple[ProductOnboardingTargetManifest, ...] = ()
    runtime_environments: tuple[ProductOnboardingRuntimeEnvironmentManifest, ...] = ()
    secret_bindings: tuple[ProductOnboardingSecretBindingManifest, ...] = ()
    updated_at: str = ""
    source_label: str = "product-onboarding"

    @model_validator(mode="after")
    def _validate_manifest(self) -> "ProductOnboardingManifest":
        if not self.product.strip():
            raise ValueError("product onboarding manifest requires product")
        if not self.display_name.strip():
            raise ValueError("product onboarding manifest requires display_name")
        if not self.repository.strip():
            raise ValueError("product onboarding manifest requires repository")
        if not self.driver_id.strip():
            raise ValueError("product onboarding manifest requires driver_id")
        if not self.image_repository.strip():
            raise ValueError("product onboarding manifest requires image_repository")
        if not self.health_path.startswith("/"):
            raise ValueError("product onboarding manifest health_path must start with /")
        if not self.lanes:
            raise ValueError("product onboarding manifest requires at least one stable lane")

        lane_routes = {(lane.context.strip(), lane.instance.strip()) for lane in self.lanes}
        if len(lane_routes) != len(self.lanes):
            raise ValueError("product onboarding lanes must be unique by context and instance")
        lane_instances = [lane.instance.strip() for lane in self.lanes]
        if len(set(lane_instances)) != len(lane_instances):
            raise ValueError("product onboarding lanes must be unique by instance")
        ProductPreviewProfile.model_validate(self.preview.model_dump(mode="json"))

        for target in self.dokploy_targets:
            route = (target.context.strip(), target.instance.strip())
            if route not in lane_routes:
                raise ValueError(
                    "product onboarding target must match a stable lane: "
                    f"{target.context}/{target.instance}"
                )

        allowed_contexts = {lane.context.strip() for lane in self.lanes}
        if self.preview.context.strip():
            allowed_contexts.add(self.preview.context.strip())
        for record in self.runtime_environments:
            if record.scope == "global":
                if record.context.strip() or record.instance.strip():
                    raise ValueError(
                        "global runtime environment records cannot set context/instance"
                    )
                continue
            if record.context.strip() not in allowed_contexts:
                raise ValueError(
                    "runtime environment context is not owned by the product profile: "
                    f"{record.context}"
                )
            if record.scope == "context" and record.instance.strip():
                raise ValueError("context runtime environment records cannot set instance")
            if (
                record.scope == "instance"
                and (record.context.strip(), record.instance.strip()) not in lane_routes
            ):
                raise ValueError(
                    "instance runtime environment record must match a stable lane: "
                    f"{record.context}/{record.instance}"
                )

        for binding in self.secret_bindings:
            if binding.context.strip() not in allowed_contexts:
                raise ValueError(
                    f"secret binding context is not owned by the product profile: {binding.context}"
                )
            if (
                binding.instance.strip()
                and (binding.context.strip(), binding.instance.strip()) not in lane_routes
            ):
                raise ValueError(
                    "instance secret binding must match a stable lane: "
                    f"{binding.context}/{binding.instance}"
                )
        return self
