from __future__ import annotations

from pathlib import Path

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.workflows.preview_desired_state import discover_github_preview_desired_state


class GenericWebPreviewDesiredStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    source: str = "generic-web-preview"
    label: str = "preview"
    max_pages: int = Field(default=10, ge=1, le=20)

    @model_validator(mode="after")
    def _validate_request(self) -> "GenericWebPreviewDesiredStateRequest":
        if not self.product.strip():
            raise ValueError("Generic web preview desired state requires product.")
        if not self.source.strip():
            raise ValueError("Generic web preview desired state requires source.")
        if not self.label.strip():
            raise ValueError("Generic web preview desired state requires label.")
        return self


def _anchor_repo(repository: str) -> str:
    owner, separator, repo = repository.strip().partition("/")
    if not separator or not owner.strip() or not repo.strip() or "/" in repo.strip():
        raise click.ClickException("GitHub repository must use owner/repo format.")
    return repo.strip()


def _preview_slug_prefix(slug_template: str) -> str:
    prefix, _, _ = slug_template.partition("{number}")
    return prefix or "pr-"


def resolve_generic_web_preview_profile(
    *, record_store: object, product: str
) -> LaunchplaneProductProfileRecord:
    profile = record_store.read_product_profile_record(product)
    if profile.driver_id != "generic-web":
        raise click.ClickException(
            f"Product {profile.product!r} is configured for driver {profile.driver_id!r}, not generic-web."
        )
    if not profile.preview.enabled:
        raise click.ClickException(f"Product {profile.product!r} does not have generic-web previews enabled.")
    if not profile.preview.context.strip():
        raise click.ClickException(f"Product {profile.product!r} does not define a preview context.")
    return profile


def discover_generic_web_preview_desired_state(
    *,
    control_plane_root: Path,
    record_store: object,
    request: GenericWebPreviewDesiredStateRequest,
    discovered_at: str,
    profile: LaunchplaneProductProfileRecord | None = None,
) -> PreviewDesiredStateRecord:
    resolved_profile = profile
    if resolved_profile is None:
        resolved_profile = resolve_generic_web_preview_profile(
            record_store=record_store,
            product=request.product,
        )
    return discover_github_preview_desired_state(
        control_plane_root=control_plane_root,
        product=resolved_profile.product,
        context=resolved_profile.preview.context,
        source=request.source,
        discovered_at=discovered_at,
        repository=resolved_profile.repository,
        label=request.label,
        anchor_repo=_anchor_repo(resolved_profile.repository),
        preview_slug_prefix=_preview_slug_prefix(resolved_profile.preview.slug_template),
        preview_slug_template=resolved_profile.preview.slug_template,
        max_pages=request.max_pages,
    )
