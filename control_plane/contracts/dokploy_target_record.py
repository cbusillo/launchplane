from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DokployTargetType = Literal["compose", "application"]
DEFAULT_DOKPLOY_HEALTHCHECK_PATH = "/web/health"


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
    updated_at: str
    source_label: str = ""

    @field_validator("context", "instance", "updated_at", mode="after")
    @classmethod
    def _validate_required_string(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Dokploy target record requires non-empty string fields")
        return value.strip()

