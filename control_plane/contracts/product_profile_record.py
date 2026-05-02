from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProductImageProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str

    @model_validator(mode="after")
    def _validate_image(self) -> "ProductImageProfile":
        if not self.repository.strip():
            raise ValueError("product image profile requires repository")
        return self


class ProductLaneProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance: str
    context: str
    base_url: str = ""
    health_url: str = ""

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
                    raise ValueError(f"product preview profile {field_name} values must be non-empty")
                if key in keys:
                    raise ValueError(f"product preview profile {field_name} values must be unique")
                keys.append(key)
            normalized[field_name] = tuple(keys)
        copied = set(normalized["copied_env_keys"])
        omitted = set(normalized["omitted_env_keys"])
        overlap = sorted(copied & omitted)
        if overlap:
            raise ValueError("product preview profile cannot both copy and omit env keys: " + ", ".join(overlap))
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
            raise ValueError("product promotion workflow default_bump must be patch, minor, or major")
        return self


class LaunchplaneProductProfileRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    display_name: str
    repository: str
    driver_id: str
    image: ProductImageProfile
    runtime_port: int = Field(ge=1, le=65535)
    health_path: str
    lanes: tuple[ProductLaneProfile, ...] = ()
    preview: ProductPreviewProfile = Field(default_factory=ProductPreviewProfile)
    promotion_workflow: ProductPromotionWorkflowProfile = Field(
        default_factory=ProductPromotionWorkflowProfile
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
        if not self.health_path.startswith("/"):
            raise ValueError("product profile health_path must start with /")
        if not self.updated_at.strip():
            raise ValueError("product profile requires updated_at")
        if not self.source.strip():
            raise ValueError("product profile requires source")
        return self
