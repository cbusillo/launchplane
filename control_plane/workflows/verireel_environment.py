from __future__ import annotations

from pathlib import Path
from typing import Literal

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane import runtime_environments as control_plane_runtime_environments


class VeriReelStableEnvironmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: Literal["verireel"] = "verireel"
    instance: Literal["testing", "prod"]

    @model_validator(mode="after")
    def _validate_request(self) -> "VeriReelStableEnvironmentRequest":
        if self.context != "verireel":
            raise ValueError("VeriReel stable environment requires context 'verireel'.")
        return self


class VeriReelStableEnvironmentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    instance: str
    target_name: str
    target_type: str
    target_id: str
    base_urls: tuple[str, ...]
    primary_base_url: str
    healthcheck_path: str
    health_urls: tuple[str, ...]


def resolve_verireel_stable_environment(
    *,
    control_plane_root: Path,
    request: VeriReelStableEnvironmentRequest,
) -> VeriReelStableEnvironmentResult:
    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    target_definition = control_plane_dokploy.find_dokploy_target_definition(
        source_of_truth,
        context_name=request.context,
        instance_name=request.instance,
    )
    if target_definition is None:
        raise click.ClickException(
            f"No Dokploy target definition found for {request.context}/{request.instance}."
        )
    environment_values = control_plane_runtime_environments.resolve_runtime_environment_values(
        control_plane_root=control_plane_root,
        context_name=request.context,
        instance_name=request.instance,
    )
    base_urls = control_plane_dokploy.resolve_healthcheck_base_urls(
        target_definition=target_definition,
        environment_values=environment_values,
    )
    if not base_urls:
        raise click.ClickException(
            f"No base URL configured for {request.context}/{request.instance}."
        )
    healthcheck_path = control_plane_dokploy.normalize_healthcheck_path(
        target_definition.healthcheck_path
    )
    return VeriReelStableEnvironmentResult(
        context=request.context,
        instance=request.instance,
        target_name=target_definition.target_name.strip(),
        target_type=target_definition.target_type,
        target_id=target_definition.target_id.strip(),
        base_urls=base_urls,
        primary_base_url=base_urls[0],
        healthcheck_path=healthcheck_path,
        health_urls=tuple(f"{base_url}{healthcheck_path}" for base_url in base_urls),
    )
