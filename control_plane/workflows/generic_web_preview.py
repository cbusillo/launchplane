from __future__ import annotations

from pathlib import Path
from typing import Literal

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
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


class GenericWebPreviewReadinessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    source: str = "generic-web-preview-readiness"

    @model_validator(mode="after")
    def _validate_request(self) -> "GenericWebPreviewReadinessRequest":
        if not self.product.strip():
            raise ValueError("Generic web preview readiness requires product.")
        if not self.source.strip():
            raise ValueError("Generic web preview readiness requires source.")
        return self


class GenericWebPreviewTransportSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_transport_mode: Literal["none", "clone", "bootstrap", "migrate_seed", "driver"]
    copied_env_keys: tuple[str, ...]
    omitted_env_keys: tuple[str, ...]
    override_env_keys: tuple[str, ...]
    preview_url_env_keys: tuple[str, ...]
    preview_domain_env_keys: tuple[str, ...]
    migration_command_configured: bool
    seed_command_configured: bool


class GenericWebPreviewReadinessCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: str
    status: Literal["pass", "blocked"]
    message: str


class GenericWebPreviewReadinessResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    readiness_status: Literal["pass", "blocked"]
    checked_at: str
    product: str
    context: str
    template_context: str
    template_instance: str
    template_target_type: str = ""
    template_target_id: str = ""
    template_target_name: str = ""
    source: str
    missing_template_env_keys: tuple[str, ...]
    missing_provider_fields: tuple[str, ...]
    transport: GenericWebPreviewTransportSummary
    checks: tuple[GenericWebPreviewReadinessCheck, ...]


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


def _template_lane(*, profile: LaunchplaneProductProfileRecord) -> ProductLaneProfile | None:
    template_instance = profile.preview.template_instance.strip()
    for lane in profile.lanes:
        if lane.instance == template_instance:
            return lane
    return None


def _field_value(payload: dict[str, object], field_path: str) -> object:
    current: object = payload
    for part in field_path.split("."):
        if not part:
            return None
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _read_template_payload(
    *, control_plane_root: Path, template_lane: ProductLaneProfile
) -> tuple[control_plane_dokploy.DokployTargetDefinition | None, dict[str, object] | None, str]:
    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    target_definition = control_plane_dokploy.find_dokploy_target_definition(
        source_of_truth,
        context_name=template_lane.context,
        instance_name=template_lane.instance,
    )
    if target_definition is None:
        return None, None, f"No Dokploy target definition found for {template_lane.context}/{template_lane.instance}."
    if target_definition.target_type != "application":
        return (
            target_definition,
            None,
            "Generic web preview readiness requires the template lane to be a Dokploy application.",
        )
    if not target_definition.target_id.strip():
        return (
            target_definition,
            None,
            "Generic web preview readiness requires the template lane to have a Dokploy target_id.",
        )
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    payload = control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=target_definition.target_type,
        target_id=target_definition.target_id,
    )
    return target_definition, payload, ""


def _transport_summary(*, profile: LaunchplaneProductProfileRecord) -> GenericWebPreviewTransportSummary:
    return GenericWebPreviewTransportSummary(
        data_transport_mode=profile.preview.data_transport_mode,
        copied_env_keys=profile.preview.copied_env_keys,
        omitted_env_keys=profile.preview.omitted_env_keys,
        override_env_keys=tuple(sorted(profile.preview.override_env)),
        preview_url_env_keys=profile.preview.preview_url_env_keys,
        preview_domain_env_keys=profile.preview.preview_domain_env_keys,
        migration_command_configured=bool(profile.preview.migration_command.strip()),
        seed_command_configured=bool(profile.preview.seed_command.strip()),
    )


def evaluate_generic_web_preview_readiness(
    *,
    control_plane_root: Path,
    record_store: object,
    request: GenericWebPreviewReadinessRequest,
    checked_at: str,
    profile: LaunchplaneProductProfileRecord | None = None,
) -> GenericWebPreviewReadinessResult:
    resolved_profile = profile
    if resolved_profile is None:
        resolved_profile = resolve_generic_web_preview_profile(
            record_store=record_store,
            product=request.product,
        )

    checks: list[GenericWebPreviewReadinessCheck] = []
    template_lane = _template_lane(profile=resolved_profile)
    if template_lane is None:
        checks.append(
            GenericWebPreviewReadinessCheck(
                check_id="template_lane",
                status="blocked",
                message=(
                    "Product profile has no lane matching preview.template_instance "
                    f"{resolved_profile.preview.template_instance!r}."
                ),
            )
        )
        return GenericWebPreviewReadinessResult(
            readiness_status="blocked",
            checked_at=checked_at,
            product=resolved_profile.product,
            context=resolved_profile.preview.context,
            template_context="",
            template_instance=resolved_profile.preview.template_instance,
            source=request.source,
            missing_template_env_keys=(),
            missing_provider_fields=(),
            transport=_transport_summary(profile=resolved_profile),
            checks=tuple(checks),
        )

    target_definition: control_plane_dokploy.DokployTargetDefinition | None = None
    template_payload: dict[str, object] | None = None
    target_error = ""
    try:
        target_definition, template_payload, target_error = _read_template_payload(
            control_plane_root=control_plane_root,
            template_lane=template_lane,
        )
    except click.ClickException as exc:
        target_error = str(exc)
    if target_error:
        checks.append(
            GenericWebPreviewReadinessCheck(
                check_id="template_target",
                status="blocked",
                message=target_error,
            )
        )
        return GenericWebPreviewReadinessResult(
            readiness_status="blocked",
            checked_at=checked_at,
            product=resolved_profile.product,
            context=resolved_profile.preview.context,
            template_context=template_lane.context,
            template_instance=template_lane.instance,
            template_target_type=(target_definition.target_type if target_definition is not None else ""),
            template_target_id=(target_definition.target_id if target_definition is not None else ""),
            template_target_name=(target_definition.target_name if target_definition is not None else ""),
            source=request.source,
            missing_template_env_keys=(),
            missing_provider_fields=(),
            transport=_transport_summary(profile=resolved_profile),
            checks=tuple(checks),
        )

    assert target_definition is not None
    assert template_payload is not None
    env_map = control_plane_dokploy.parse_dokploy_env_text(str(template_payload.get("env") or ""))
    required_env_keys = tuple(
        dict.fromkeys(
            (
                *resolved_profile.preview.required_template_env_keys,
                *resolved_profile.preview.copied_env_keys,
            )
        )
    )
    missing_env_keys = tuple(key for key in required_env_keys if not env_map.get(key, "").strip())
    missing_provider_fields = tuple(
        field
        for field in resolved_profile.preview.required_provider_fields
        if _is_blank(_field_value(template_payload, field))
    )

    if missing_env_keys:
        checks.append(
            GenericWebPreviewReadinessCheck(
                check_id="template_env",
                status="blocked",
                message="Template lane is missing required env keys: " + ", ".join(missing_env_keys),
            )
        )
    else:
        checks.append(
            GenericWebPreviewReadinessCheck(
                check_id="template_env",
                status="pass",
                message="Template lane includes required env keys.",
            )
        )
    if missing_provider_fields:
        checks.append(
            GenericWebPreviewReadinessCheck(
                check_id="template_provider_fields",
                status="blocked",
                message="Template lane is missing required provider fields: "
                + ", ".join(missing_provider_fields),
            )
        )
    else:
        checks.append(
            GenericWebPreviewReadinessCheck(
                check_id="template_provider_fields",
                status="pass",
                message="Template lane includes required provider fields.",
            )
        )
    checks.append(
        GenericWebPreviewReadinessCheck(
            check_id="transport_policy",
            status="pass",
            message=(
                "Preview transport policy is configured for "
                f"{resolved_profile.preview.data_transport_mode!r} mode."
            ),
        )
    )
    status: Literal["pass", "blocked"] = "blocked" if missing_env_keys or missing_provider_fields else "pass"
    return GenericWebPreviewReadinessResult(
        readiness_status=status,
        checked_at=checked_at,
        product=resolved_profile.product,
        context=resolved_profile.preview.context,
        template_context=template_lane.context,
        template_instance=template_lane.instance,
        template_target_type=target_definition.target_type,
        template_target_id=target_definition.target_id,
        template_target_name=target_definition.target_name,
        source=request.source,
        missing_template_env_keys=missing_env_keys,
        missing_provider_fields=missing_provider_fields,
        transport=_transport_summary(profile=resolved_profile),
        checks=tuple(checks),
    )


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
