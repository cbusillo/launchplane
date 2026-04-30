from __future__ import annotations

from pathlib import Path
from typing import Literal

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.workflows.preview_desired_state import discover_github_preview_desired_state
from control_plane.workflows.ship import utc_now_timestamp


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


class GenericWebPreviewInventoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    source: str = "generic-web-preview-inventory"

    @model_validator(mode="after")
    def _validate_request(self) -> "GenericWebPreviewInventoryRequest":
        if not self.product.strip():
            raise ValueError("Generic web preview inventory requires product.")
        if not self.source.strip():
            raise ValueError("Generic web preview inventory requires source.")
        return self


class GenericWebPreviewInventoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applicationId: str
    applicationName: str
    previewSlug: str


class GenericWebPreviewInventoryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    source: str
    app_name_prefix: str
    previews: tuple[GenericWebPreviewInventoryItem, ...]


class GenericWebPreviewDestroyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    preview_slug: str
    destroy_reason: str
    timeout_seconds: int = Field(default=300, ge=1)

    @model_validator(mode="after")
    def _validate_request(self) -> "GenericWebPreviewDestroyRequest":
        if not self.product.strip():
            raise ValueError("Generic web preview destroy requires product.")
        if not self.preview_slug.strip():
            raise ValueError("Generic web preview destroy requires preview_slug.")
        if not self.destroy_reason.strip():
            raise ValueError("Generic web preview destroy requires destroy_reason.")
        return self


class GenericWebPreviewDestroyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destroy_status: Literal["pass", "fail"]
    destroy_started_at: str
    destroy_finished_at: str
    product: str
    context: str
    preview_slug: str
    application_name: str
    application_id: str
    error_message: str = ""


def _anchor_repo(repository: str) -> str:
    owner, separator, repo = repository.strip().partition("/")
    if not separator or not owner.strip() or not repo.strip() or "/" in repo.strip():
        raise click.ClickException("GitHub repository must use owner/repo format.")
    return repo.strip()


def _preview_slug_prefix(slug_template: str) -> str:
    prefix, _, _ = slug_template.partition("{number}")
    return prefix or "pr-"


def effective_preview_app_name_prefix(*, profile: LaunchplaneProductProfileRecord) -> str:
    return profile.preview.app_name_prefix.strip() or f"{profile.product}-preview"


def preview_application_name(*, app_name_prefix: str, preview_slug: str) -> str:
    return f"{app_name_prefix.strip()}-{preview_slug.strip()}"


def preview_slug_from_application_name(*, app_name_prefix: str, application_name: str) -> str:
    prefix = f"{app_name_prefix.strip()}-"
    if not application_name.startswith(prefix):
        return ""
    return application_name[len(prefix) :]


def preview_pr_number_from_slug(*, preview_slug: str, slug_template: str) -> int | None:
    prefix, separator, suffix = slug_template.partition("{number}")
    if not separator:
        return None
    slug = preview_slug.strip()
    if not slug.startswith(prefix):
        return None
    tail = slug[len(prefix) :]
    if suffix:
        if not tail.endswith(suffix):
            return None
        tail = tail[: -len(suffix)]
    if not tail.isdigit():
        return None
    number = int(tail)
    return number if number > 0 else None


def _iter_dokploy_applications(raw_projects: object):
    if not isinstance(raw_projects, list):
        raise click.ClickException("Dokploy project inventory returned an invalid response payload.")
    for raw_project in raw_projects:
        project = control_plane_dokploy.as_json_object(raw_project)
        if project is None:
            continue
        raw_environments = project.get("environments")
        if not isinstance(raw_environments, list):
            continue
        for raw_environment in raw_environments:
            environment = control_plane_dokploy.as_json_object(raw_environment)
            if environment is None:
                continue
            raw_applications = environment.get("applications")
            if not isinstance(raw_applications, list):
                continue
            for raw_application in raw_applications:
                application = control_plane_dokploy.as_json_object(raw_application)
                if application is not None:
                    yield application


def _find_application_by_name(*, host: str, token: str, application_name: str):
    raw_projects = control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/project.all",
    )
    for application in _iter_dokploy_applications(raw_projects):
        if str(application.get("name") or "").strip() == application_name:
            return application
    return None


def _delete_domain(*, host: str, token: str, domain_id: str) -> None:
    control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/domain.delete",
        method="POST",
        payload={"domainId": domain_id},
    )


def _delete_application(*, host: str, token: str, application_id: str) -> None:
    control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/application.delete",
        method="POST",
        payload={"applicationId": application_id},
    )


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


def execute_generic_web_preview_inventory(
    *,
    control_plane_root: Path,
    record_store: object,
    request: GenericWebPreviewInventoryRequest,
    profile: LaunchplaneProductProfileRecord | None = None,
) -> GenericWebPreviewInventoryResult:
    resolved_profile = profile
    if resolved_profile is None:
        resolved_profile = resolve_generic_web_preview_profile(
            record_store=record_store,
            product=request.product,
        )
    app_name_prefix = effective_preview_app_name_prefix(profile=resolved_profile)
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    raw_projects = control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/project.all",
    )
    preview_items: list[GenericWebPreviewInventoryItem] = []
    for application in _iter_dokploy_applications(raw_projects):
        application_name = str(application.get("name") or "").strip()
        preview_slug = preview_slug_from_application_name(
            app_name_prefix=app_name_prefix,
            application_name=application_name,
        )
        if not preview_slug:
            continue
        application_id = str(application.get("applicationId") or application.get("id") or "").strip()
        if not application_id:
            continue
        preview_items.append(
            GenericWebPreviewInventoryItem(
                applicationId=application_id,
                applicationName=application_name,
                previewSlug=preview_slug,
            )
        )
    return GenericWebPreviewInventoryResult(
        product=resolved_profile.product,
        context=resolved_profile.preview.context,
        source=request.source,
        app_name_prefix=app_name_prefix,
        previews=tuple(sorted(preview_items, key=lambda item: item.previewSlug)),
    )


def execute_generic_web_preview_destroy(
    *,
    control_plane_root: Path,
    record_store: object,
    request: GenericWebPreviewDestroyRequest,
    profile: LaunchplaneProductProfileRecord | None = None,
) -> GenericWebPreviewDestroyResult:
    resolved_profile = profile
    if resolved_profile is None:
        resolved_profile = resolve_generic_web_preview_profile(
            record_store=record_store,
            product=request.product,
        )
    started_at = utc_now_timestamp()
    app_name_prefix = effective_preview_app_name_prefix(profile=resolved_profile)
    application_name = preview_application_name(
        app_name_prefix=app_name_prefix,
        preview_slug=request.preview_slug,
    )
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    application = _find_application_by_name(
        host=host,
        token=token,
        application_name=application_name,
    )
    application_id = str((application or {}).get("applicationId") or "").strip()
    cleanup_errors: list[str] = []
    if application_id:
        try:
            raw_domains = control_plane_dokploy.dokploy_request(
                host=host,
                token=token,
                path="/api/domain.byApplicationId",
                query={"applicationId": application_id},
            )
            if isinstance(raw_domains, list):
                for raw_domain in raw_domains:
                    domain = control_plane_dokploy.as_json_object(raw_domain)
                    if domain is None:
                        continue
                    domain_id = str(domain.get("domainId") or "").strip()
                    if domain_id:
                        _delete_domain(host=host, token=token, domain_id=domain_id)
        except click.ClickException as exc:
            cleanup_errors.append(f"domain cleanup failed: {exc}")
        try:
            _delete_application(host=host, token=token, application_id=application_id)
        except click.ClickException as exc:
            cleanup_errors.append(f"application cleanup failed: {exc}")
    finished_at = utc_now_timestamp()
    if cleanup_errors:
        return GenericWebPreviewDestroyResult(
            destroy_status="fail",
            destroy_started_at=started_at,
            destroy_finished_at=finished_at,
            product=resolved_profile.product,
            context=resolved_profile.preview.context,
            preview_slug=request.preview_slug,
            application_name=application_name,
            application_id=application_id,
            error_message="; ".join(cleanup_errors),
        )
    return GenericWebPreviewDestroyResult(
        destroy_status="pass",
        destroy_started_at=started_at,
        destroy_finished_at=finished_at,
        product=resolved_profile.product,
        context=resolved_profile.preview.context,
        preview_slug=request.preview_slug,
        application_name=application_name,
        application_id=application_id,
    )
