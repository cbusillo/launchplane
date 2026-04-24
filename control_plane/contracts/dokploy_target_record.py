from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DokployTargetType = Literal["compose", "application"]
DEFAULT_DOKPLOY_HEALTHCHECK_PATH = "/web/health"


class DokployTargetShopifyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protected_store_keys: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _normalize_store_keys(self) -> "DokployTargetShopifyPolicy":
        normalized_keys: list[str] = []
        for raw_key in self.protected_store_keys:
            normalized_key = raw_key.strip()
            if not normalized_key:
                raise ValueError("Dokploy Shopify protected store keys must be non-empty")
            if normalized_key not in normalized_keys:
                normalized_keys.append(normalized_key)
        self.protected_store_keys = tuple(normalized_keys)
        return self


class DokployTargetPolicies(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shopify: DokployTargetShopifyPolicy = Field(default_factory=DokployTargetShopifyPolicy)


class DokployTargetRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str
    project_name: str = ""
    target_type: DokployTargetType = "compose"
    target_name: str = ""
    git_branch: str = ""
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
    healthcheck_path: str = DEFAULT_DOKPLOY_HEALTHCHECK_PATH
    healthcheck_timeout_seconds: int | None = Field(default=None, ge=1)
    env: dict[str, str] = Field(default_factory=dict)
    domains: tuple[str, ...] = ()
    policies: DokployTargetPolicies = Field(default_factory=DokployTargetPolicies)
    updated_at: str
    source_label: str = ""

    @field_validator("context", "instance", "updated_at", mode="after")
    @classmethod
    def _validate_required_string(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Dokploy target record requires non-empty string fields")
        return value.strip()
