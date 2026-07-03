from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterator, Literal, Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane import secrets as control_plane_secrets
from control_plane.dokploy import JsonObject, JsonValue
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.runtime_identity import (
    RuntimeIdentity,
    health_payload_runtime_identity_status,
    runtime_identity_env,
)
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeKeySafetyTarget,
)
from control_plane.contracts.secret_record import SecretBinding
from control_plane.runtime_key_safety import (
    evaluate_runtime_key_safety,
    latest_active_runtime_key_safety_policy,
)
from control_plane.workflows.generic_web_deploy import product_profile_uses_generic_web_base
from control_plane.workflows.preview_desired_state import (
    discover_github_preview_desired_state,
    render_preview_slug,
)
from control_plane.workflows.preview_resource_destroy import (
    destroy_dokploy_preview_resource,
)
from control_plane.workflows.ship import generate_deployment_record_id, utc_now_timestamp


_SECRET_SHAPED_RUNTIME_ENV_KEY_PARTS = {"PASSWORD", "TOKEN", "SECRET", "KEY"}
_PREVIEW_BASE_URL_ENV_KEY = "LAUNCHPLANE_PREVIEW_BASE_URL"


class GenericWebPreviewProfileStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...


@runtime_checkable
class GenericWebPreviewRuntimeKeySafetyStore(Protocol):
    def list_runtime_key_safety_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RuntimeKeySafetyPolicyRecord, ...]: ...

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]: ...


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
    providerType: Literal["application", "compose-domain"] = "application"
    domainId: str = ""
    domainHost: str = ""


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
    preview_slug: str = ""
    anchor_pr_number: int | None = Field(default=None, ge=1)
    destroy_reason: str
    timeout_seconds: int = Field(default=300, ge=1)

    @model_validator(mode="after")
    def _validate_request(self) -> "GenericWebPreviewDestroyRequest":
        if not self.product.strip():
            raise ValueError("Generic web preview destroy requires product.")
        if not self.preview_slug.strip() and self.anchor_pr_number is None:
            raise ValueError(
                "Generic web preview destroy requires preview_slug or anchor_pr_number."
            )
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
    provider_type: Literal["application", "compose-domain"] = "application"
    domain_ids: tuple[str, ...] = ()
    error_message: str = ""


class GenericWebPreviewRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    preview_slug: str = ""
    preview_url: str = ""
    image_reference: str
    anchor_pr_number: int | None = Field(default=None, ge=1)
    anchor_pr_url: str = ""
    anchor_head_sha: str = ""
    source: str = "generic-web-preview-refresh"
    timeout_seconds: int = Field(default=300, ge=1)
    no_cache: bool = False

    @model_validator(mode="after")
    def _validate_request(self) -> "GenericWebPreviewRefreshRequest":
        if not self.product.strip():
            raise ValueError("Generic web preview refresh requires product.")
        if not self.preview_slug.strip() and self.anchor_pr_number is None:
            raise ValueError(
                "Generic web preview refresh requires preview_slug or anchor_pr_number."
            )
        if self.preview_url.strip():
            parsed = urlparse(self.preview_url.strip())
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(
                    "Generic web preview refresh preview_url must be an absolute http(s) URL."
                )
        if not self.image_reference.strip():
            raise ValueError("Generic web preview refresh requires image_reference.")
        if not self.source.strip():
            raise ValueError("Generic web preview refresh requires source.")
        if self.anchor_pr_url.strip():
            parsed_pr_url = urlparse(self.anchor_pr_url.strip())
            if parsed_pr_url.scheme not in {"http", "https"} or not parsed_pr_url.netloc:
                raise ValueError(
                    "Generic web preview refresh anchor_pr_url must be an absolute http(s) URL."
                )
        return self


class GenericWebPreviewRefreshResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_status: Literal["pass", "blocked", "fail"]
    refresh_started_at: str
    refresh_finished_at: str
    product: str
    context: str
    preview_slug: str
    application_name: str
    application_id: str = ""
    preview_url: str
    readiness: GenericWebPreviewReadinessResult | None = None
    smoke: GenericWebPreviewSmokeResult | None = None
    error_message: str = ""


class MissingPreviewBaseUrlError(click.ClickException):
    pass


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


class GenericWebPreviewSmokeCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: str
    status: Literal["pass", "fail"]
    message: str


class GenericWebPreviewSmokeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    smoke_status: Literal["pass", "fail"]
    checked_at: str
    checks: tuple[GenericWebPreviewSmokeCheck, ...]
    failure_summary: str = ""


def _anchor_repo(repository: str) -> str:
    owner, separator, repo = repository.strip().partition("/")
    if not separator or not owner.strip() or not repo.strip() or "/" in repo.strip():
        raise click.ClickException("GitHub repository must use owner/repo format.")
    return repo.strip()


def _build_preview_runtime_identity(
    *,
    profile: "LaunchplaneProductProfileRecord",
    preview_slug: str,
    image_reference: str,
    anchor_head_sha: str,
    deployed_at: str = "",
) -> RuntimeIdentity | None:
    if not anchor_head_sha.strip():
        return None
    return RuntimeIdentity(
        product=profile.product,
        context=profile.preview.context,
        instance=preview_slug,
        environment_kind="preview",
        deployment_record_id=generate_deployment_record_id(
            context_name=profile.preview.context,
            instance_name=preview_slug,
        ),
        artifact_id=image_reference,
        source_git_ref=anchor_head_sha,
        image_reference=image_reference,
        preview_id=preview_slug,
        deployed_at=deployed_at,
    )


def _preview_slug_prefix(slug_template: str) -> str:
    prefix, _, _ = slug_template.partition("{number}")
    return prefix or "pr-"


def resolve_generic_web_preview_slug(
    *,
    profile: LaunchplaneProductProfileRecord,
    preview_slug: str,
    anchor_pr_number: int | None,
    label: str,
) -> str:
    supplied_slug = preview_slug.strip()
    if anchor_pr_number is None:
        if supplied_slug:
            return supplied_slug
        raise click.ClickException(f"{label} requires preview_slug or anchor_pr_number.")
    derived_slug = render_preview_slug(
        anchor_pr_number=anchor_pr_number,
        preview_slug_prefix=_preview_slug_prefix(profile.preview.slug_template),
        preview_slug_template=profile.preview.slug_template,
    )
    if supplied_slug and supplied_slug != derived_slug:
        raise click.ClickException(
            f"{label} preview_slug does not match the product profile slug policy."
        )
    return derived_slug


def _refresh_request_with_resolved_preview_slug(
    *,
    profile: LaunchplaneProductProfileRecord,
    request: GenericWebPreviewRefreshRequest,
) -> GenericWebPreviewRefreshRequest:
    return request.model_copy(
        update={
            "preview_slug": resolve_generic_web_preview_slug(
                profile=profile,
                preview_slug=request.preview_slug,
                anchor_pr_number=request.anchor_pr_number,
                label="Generic web preview refresh",
            )
        }
    )


def _destroy_request_with_resolved_preview_slug(
    *,
    profile: LaunchplaneProductProfileRecord,
    request: GenericWebPreviewDestroyRequest,
) -> GenericWebPreviewDestroyRequest:
    return request.model_copy(
        update={
            "preview_slug": resolve_generic_web_preview_slug(
                profile=profile,
                preview_slug=request.preview_slug,
                anchor_pr_number=request.anchor_pr_number,
                label="Generic web preview destroy",
            )
        }
    )


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


def _iter_dokploy_applications(raw_projects: object) -> Iterator[JsonObject]:
    if not isinstance(raw_projects, list):
        raise click.ClickException(
            "Dokploy project inventory returned an invalid response payload."
        )
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


def _find_application_by_name(*, host: str, token: str, application_name: str) -> JsonObject | None:
    raw_projects = control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/project.all",
    )
    for application in _iter_dokploy_applications(raw_projects):
        if str(application.get("name") or "").strip() == application_name:
            return application
    return None


def _fetch_application(*, host: str, token: str, application_id: str) -> JsonObject:
    return control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type="application",
        target_id=application_id,
    )


def _ensure_application(
    *,
    host: str,
    token: str,
    application_name: str,
    app_name: str,
    description: str,
    template_application: JsonObject,
) -> tuple[JsonObject, bool]:
    existing = _find_application_by_name(host=host, token=token, application_name=application_name)
    if existing is not None:
        application_id = str(existing.get("applicationId") or "").strip()
        if not application_id:
            raise click.ClickException(
                f"Dokploy application {application_name!r} exists but does not expose an applicationId."
            )
        return _fetch_application(host=host, token=token, application_id=application_id), False

    environment_id = str(template_application.get("environmentId") or "").strip()
    server_id = str(template_application.get("serverId") or "").strip()
    if not environment_id:
        raise click.ClickException(
            "Generic web preview template application is missing environmentId."
        )
    if not server_id:
        raise click.ClickException("Generic web preview template application is missing serverId.")
    created = control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/application.create",
        method="POST",
        payload={
            "name": application_name,
            "appName": app_name,
            "description": description,
            "environmentId": environment_id,
            "serverId": server_id,
        },
    )
    created_application = control_plane_dokploy.as_json_object(created)
    created_application_id = str((created_application or {}).get("applicationId") or "").strip()
    if not created_application_id:
        raise click.ClickException(
            f"Dokploy did not return an applicationId for preview app {application_name!r}."
        )
    return _fetch_application(host=host, token=token, application_id=created_application_id), True


def _preview_endpoint_spec_swarm(template_application: JsonObject) -> JsonValue:
    return template_application.get("endpointSpecSwarm") or {"Mode": "dnsrr"}


def _configure_application(
    *,
    host: str,
    token: str,
    application: JsonObject,
    template_application: JsonObject,
    image_reference: str,
    env_text: str,
) -> None:
    application_id = str(application.get("applicationId") or "").strip()
    if not application_id:
        raise click.ClickException("Preview application payload is missing applicationId.")
    control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/application.update",
        method="POST",
        payload={
            "applicationId": application_id,
            "description": str(application.get("description") or "").strip(),
            "sourceType": "docker",
            "autoDeploy": True,
            "replicas": template_application.get("replicas"),
            "endpointSpecSwarm": _preview_endpoint_spec_swarm(template_application),
            "createEnvFile": template_application.get("createEnvFile"),
            "triggerType": template_application.get("triggerType"),
            "enabled": template_application.get("enabled"),
        },
    )
    control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/application.saveBuildType",
        method="POST",
        payload={
            "applicationId": application_id,
            "buildType": template_application.get("buildType"),
            "dockerfile": template_application.get("dockerfile"),
            "dockerContextPath": template_application.get("dockerContextPath"),
            "dockerBuildStage": template_application.get("dockerBuildStage"),
            "herokuVersion": template_application.get("herokuVersion"),
            "railpackVersion": template_application.get("railpackVersion"),
            "publishDirectory": template_application.get("publishDirectory"),
            "isStaticSpa": template_application.get("isStaticSpa"),
        },
    )
    control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/application.saveDockerProvider",
        method="POST",
        payload={
            "applicationId": application_id,
            "dockerImage": image_reference,
            "username": template_application.get("username"),
            "password": template_application.get("password"),
            "registryUrl": template_application.get("registryUrl"),
        },
    )
    control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/application.saveEnvironment",
        method="POST",
        payload={
            "applicationId": application_id,
            "env": env_text,
            "buildArgs": template_application.get("buildArgs"),
            "buildSecrets": template_application.get("buildSecrets"),
            "createEnvFile": template_application.get("createEnvFile"),
        },
    )


def _ensure_domain(
    *, host: str, token: str, application_id: str, preview_host: str, runtime_port: int
) -> tuple[str, tuple[str, ...]]:
    raw_domains = control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/domain.byApplicationId",
        query={"applicationId": application_id},
    )
    domains = raw_domains if isinstance(raw_domains, list) else []
    existing: JsonObject | None = None
    stale_domain_ids: list[str] = []
    for raw_domain in domains:
        domain = control_plane_dokploy.as_json_object(raw_domain)
        if domain is None:
            continue
        domain_host = str(domain.get("host") or "").strip()
        domain_id = str(domain.get("domainId") or "").strip()
        if domain_host == preview_host and domain_id:
            existing = domain
            continue
        if domain_id:
            stale_domain_ids.append(domain_id)
    payload: JsonObject = {
        "host": preview_host,
        "path": "/",
        "internalPath": "/",
        "port": runtime_port,
        "https": True,
        "applicationId": application_id,
        "certificateType": "none",
        "customCertResolver": None,
        "composeId": None,
        "serviceName": None,
        "domainType": "application",
        "previewDeploymentId": None,
        "stripPath": False,
    }
    if existing is not None:
        existing_domain_id = str(existing.get("domainId") or "").strip()
        update_payload: JsonObject = {"domainId": existing_domain_id, **payload}
        control_plane_dokploy.dokploy_request(
            host=host,
            token=token,
            path="/api/domain.update",
            method="POST",
            payload=update_payload,
        )
        return "", tuple(stale_domain_ids)
    created = control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/domain.create",
        method="POST",
        payload=payload,
    )
    created_domain = control_plane_dokploy.as_json_object(created)
    return str((created_domain or {}).get("domainId") or "").strip(), tuple(stale_domain_ids)


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
    *, record_store: GenericWebPreviewProfileStore, product: str
) -> LaunchplaneProductProfileRecord:
    profile = record_store.read_product_profile_record(product)
    if not product_profile_uses_generic_web_base(profile):
        raise click.ClickException(
            f"Product {profile.product!r} is configured for driver {profile.driver_id!r}, "
            "not generic-web or a generic-web based driver."
        )
    if not profile.preview.enabled:
        raise click.ClickException(
            f"Product {profile.product!r} does not have generic-web previews enabled."
        )
    if not profile.preview.context.strip():
        raise click.ClickException(
            f"Product {profile.product!r} does not define a preview context."
        )
    return profile


def _template_lane(*, profile: LaunchplaneProductProfileRecord) -> ProductLaneProfile | None:
    template_instance = profile.preview.template_instance.strip()
    for lane in profile.lanes:
        if lane.instance == template_instance:
            return lane
    return None


def _field_value(payload: JsonObject, field_path: str) -> object:
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
    *,
    control_plane_root: Path,
    template_lane: ProductLaneProfile,
) -> tuple[control_plane_dokploy.DokployTargetDefinition | None, JsonObject | None, str]:
    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
        allow_incomplete_target_ids=True,
        allowed_incomplete_target_routes=((template_lane.context, template_lane.instance),),
    )
    target_definition = control_plane_dokploy.find_dokploy_target_definition(
        source_of_truth,
        context_name=template_lane.context,
        instance_name=template_lane.instance,
    )
    if target_definition is None:
        return (
            None,
            None,
            f"No Dokploy target definition found for {template_lane.context}/{template_lane.instance}.",
        )
    if target_definition.target_type == "compose":
        return (
            target_definition,
            None,
            "Generic web preview readiness requires the template lane to be a Dokploy application.",
        )
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


def _transport_summary(
    *, profile: LaunchplaneProductProfileRecord
) -> GenericWebPreviewTransportSummary:
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


def _preview_host(preview_url: str) -> str:
    parsed = urlparse(preview_url.strip())
    if not parsed.hostname:
        raise click.ClickException("Generic web preview URL is missing a hostname.")
    if parsed.port:
        return f"{parsed.hostname}:{parsed.port}"
    return parsed.hostname


def _preview_url_from_base_url(*, preview_slug: str, preview_base_url: str) -> str:
    parsed = urlparse(preview_base_url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise click.ClickException(f"{_PREVIEW_BASE_URL_ENV_KEY} must use http or https.")
    if not parsed.hostname:
        raise click.ClickException(f"{_PREVIEW_BASE_URL_ENV_KEY} requires a hostname.")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise click.ClickException(
            f"{_PREVIEW_BASE_URL_ENV_KEY} must be a root URL without path, query, or fragment."
        )
    host = f"{preview_slug.strip()}.{parsed.hostname}"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme}://{host}"


def resolve_generic_web_preview_url(
    *,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    request: GenericWebPreviewRefreshRequest,
    database_url: str | None = None,
) -> str:
    explicit_preview_url = request.preview_url.strip()
    if explicit_preview_url:
        return explicit_preview_url
    context_values = control_plane_runtime_environments.resolve_runtime_context_values(
        control_plane_root=control_plane_root,
        context_name=profile.preview.context,
        database_url=database_url,
    )
    preview_base_url = str(context_values.get(_PREVIEW_BASE_URL_ENV_KEY) or "").strip()
    if not preview_base_url:
        raise MissingPreviewBaseUrlError(
            f"Missing {_PREVIEW_BASE_URL_ENV_KEY} in Launchplane runtime-environment records for {profile.preview.context}."
        )
    return _preview_url_from_base_url(
        preview_slug=resolve_generic_web_preview_slug(
            profile=profile,
            preview_slug=request.preview_slug,
            anchor_pr_number=request.anchor_pr_number,
            label="Generic web preview refresh",
        ),
        preview_base_url=preview_base_url,
    )


def _render_preview_env_text(
    *,
    profile: LaunchplaneProductProfileRecord,
    template_application: JsonObject,
    preview_url: str,
    runtime_identity: RuntimeIdentity | None = None,
) -> str:
    template_env = control_plane_dokploy.parse_dokploy_env_text(
        str(template_application.get("env") or "")
    )
    preview_host = _preview_host(preview_url)
    updates: dict[str, str] = {}
    for key in profile.preview.copied_env_keys:
        value = template_env.get(key, "")
        if value:
            updates[key] = value
    updates.update(profile.preview.override_env)
    for key in profile.preview.preview_url_env_keys:
        updates[key] = preview_url
    for key in profile.preview.preview_domain_env_keys:
        updates[key] = preview_host
    if runtime_identity is not None:
        updates.update(runtime_identity_env(runtime_identity))
    return control_plane_dokploy.render_dokploy_env_text_with_overrides(
        "",
        updates=updates,
    )


def _runtime_environment_key_requires_secret_store(key_name: str) -> bool:
    return any(
        key_part in _SECRET_SHAPED_RUNTIME_ENV_KEY_PARTS
        for key_part in key_name.strip().upper().split("_")
    )


def _copied_secret_shaped_runtime_keys(
    *, profile: LaunchplaneProductProfileRecord, template_application: JsonObject
) -> tuple[str, ...]:
    template_env = control_plane_dokploy.parse_dokploy_env_text(
        str(template_application.get("env") or "")
    )
    copied_keys: list[str] = []
    for key in profile.preview.copied_env_keys:
        value = template_env.get(key, "")
        if value and _runtime_environment_key_requires_secret_store(key):
            copied_keys.append(key)
    return tuple(dict.fromkeys(copied_keys))


def _retarget_secret_bindings_for_preview_safety(
    *,
    secret_bindings: tuple[SecretBinding, ...],
    preview_context: str,
    preview_slug: str,
) -> tuple[SecretBinding, ...]:
    return tuple(
        binding.model_copy(
            update={
                "context": preview_context,
                "instance": preview_slug,
            }
        )
        for binding in secret_bindings
    )


def _enforce_preview_copied_runtime_key_safety(
    *,
    record_store: GenericWebPreviewProfileStore,
    profile: LaunchplaneProductProfileRecord,
    template_lane: ProductLaneProfile,
    template_application: JsonObject,
    preview_slug: str,
) -> None:
    required_binding_keys = _copied_secret_shaped_runtime_keys(
        profile=profile,
        template_application=template_application,
    )
    if not required_binding_keys:
        return
    if not isinstance(record_store, GenericWebPreviewRuntimeKeySafetyStore):
        raise click.ClickException(
            "Generic web preview copied secret-shaped runtime keys require a "
            "runtime key-safety-capable record store."
        )

    try:
        policy_record = latest_active_runtime_key_safety_policy(record_store)
    except ValueError as exc:
        raise click.ClickException(
            "Generic web preview copied secret-shaped runtime keys require an active "
            "runtime key-safety policy."
        ) from exc

    evaluation = evaluate_runtime_key_safety(
        target=RuntimeKeySafetyTarget(
            context=profile.preview.context,
            instance=preview_slug,
            environment_class="preview",
        ),
        required_binding_keys=required_binding_keys,
        secret_bindings=_retarget_secret_bindings_for_preview_safety(
            secret_bindings=record_store.list_secret_bindings(
                integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                context_name=template_lane.context,
                instance_name=template_lane.instance,
                limit=None,
            ),
            preview_context=profile.preview.context,
            preview_slug=preview_slug,
        ),
        secret_rules=policy_record.rules,
    )
    if evaluation.status == "pass":
        return
    finding_codes = ", ".join(finding.code for finding in evaluation.findings)
    checked_keys = ", ".join(evaluation.checked_binding_keys)
    raise click.ClickException(
        "Generic web preview copied runtime key-safety gate failed for "
        f"{checked_keys}: {finding_codes}."
    )


def _wait_for_preview_health(
    *,
    preview_url: str,
    health_path: str,
    timeout_seconds: int,
    expected_runtime_identity: RuntimeIdentity | None = None,
) -> None:
    parsed = urlparse(preview_url.rstrip("/"))
    health_url = parsed._replace(path=health_path, params="", query="", fragment="").geturl()
    deadline = timeout_seconds
    request = Request(
        health_url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Cache-Control": "no-store",
        },
    )
    last_detail = "timeout"
    while deadline > 0:
        try:
            with urlopen(request, timeout=min(15, deadline)) as response:
                body = response.read().decode("utf-8")
            if 200 <= response.status < 400:
                if not body.strip():
                    if expected_runtime_identity is None:
                        return
                    last_detail = "health endpoint returned an empty response"
                else:
                    try:
                        payload = json.loads(body)
                    except json.JSONDecodeError:
                        if expected_runtime_identity is None:
                            return
                        last_detail = "health endpoint did not return parseable JSON"
                    else:
                        if not isinstance(payload, dict):
                            if expected_runtime_identity is None:
                                return
                            last_detail = "health endpoint returned non-object JSON"
                        elif payload.get("ok") is False:
                            last_detail = "health endpoint reported ok=false"
                        elif expected_runtime_identity is None:
                            return
                        else:
                            status, detail, _observed = health_payload_runtime_identity_status(
                                expected=expected_runtime_identity,
                                payload=payload,
                                json_parse_failed=False,
                            )
                            if status == "match":
                                return
                            if status == "mismatch":
                                raise click.ClickException(
                                    "Preview runtime identity did not match the expected "
                                    f"deployment identity: {detail}."
                                )
                            last_detail = detail
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if "dokploy dead host" in body.lower():
                raise click.ClickException(
                    f"Preview ingress for {health_url} returned Dokploy Dead Host; "
                    "public preview DNS or ingress is not routed to the expected Dokploy target."
                ) from exc
        except (URLError, TimeoutError, ValueError):
            pass
        sleep_seconds = min(5, deadline)
        time.sleep(sleep_seconds)
        deadline -= sleep_seconds
    raise click.ClickException(
        f"Timed out waiting for {health_url} to report healthy: {last_detail}."
    )


def evaluate_generic_web_preview_readiness(
    *,
    control_plane_root: Path,
    record_store: GenericWebPreviewProfileStore,
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
    template_payload: JsonObject | None = None
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
            template_target_type=(
                target_definition.target_type if target_definition is not None else ""
            ),
            template_target_id=(
                target_definition.target_id if target_definition is not None else ""
            ),
            template_target_name=(
                target_definition.target_name if target_definition is not None else ""
            ),
            source=request.source,
            missing_template_env_keys=(),
            missing_provider_fields=(),
            transport=_transport_summary(profile=resolved_profile),
            checks=tuple(checks),
        )

    assert target_definition is not None
    assert template_payload is not None
    env_map = control_plane_dokploy.parse_dokploy_env_text(str(template_payload.get("env") or ""))
    required_env_parts = (
        *resolved_profile.preview.required_template_env_keys,
        *resolved_profile.preview.copied_env_keys,
    )
    required_env_keys = tuple(dict.fromkeys(required_env_parts))
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
                message="Template lane is missing required env keys: "
                + ", ".join(missing_env_keys),
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
    status: Literal["pass", "blocked"] = (
        "blocked" if missing_env_keys or missing_provider_fields else "pass"
    )
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


def execute_generic_web_preview_refresh(
    *,
    control_plane_root: Path,
    record_store: GenericWebPreviewProfileStore,
    request: GenericWebPreviewRefreshRequest,
    profile: LaunchplaneProductProfileRecord | None = None,
) -> GenericWebPreviewRefreshResult:
    resolved_profile = profile
    if resolved_profile is None:
        resolved_profile = resolve_generic_web_preview_profile(
            record_store=record_store,
            product=request.product,
        )
    request = _refresh_request_with_resolved_preview_slug(
        profile=resolved_profile,
        request=request,
    )
    started_at = utc_now_timestamp()
    app_name_prefix = effective_preview_app_name_prefix(profile=resolved_profile)
    application_name = preview_application_name(
        app_name_prefix=app_name_prefix,
        preview_slug=request.preview_slug,
    )
    try:
        preview_url = resolve_generic_web_preview_url(
            control_plane_root=control_plane_root,
            profile=resolved_profile,
            request=request,
        )
    except MissingPreviewBaseUrlError as exc:
        finished_at = utc_now_timestamp()
        return GenericWebPreviewRefreshResult(
            refresh_status="blocked",
            refresh_started_at=started_at,
            refresh_finished_at=finished_at,
            product=resolved_profile.product,
            context=resolved_profile.preview.context,
            preview_slug=request.preview_slug,
            application_name=application_name,
            preview_url="",
            error_message=str(exc),
        )
    readiness = evaluate_generic_web_preview_readiness(
        control_plane_root=control_plane_root,
        record_store=record_store,
        request=GenericWebPreviewReadinessRequest(product=request.product, source=request.source),
        checked_at=started_at,
        profile=resolved_profile,
    )
    if readiness.readiness_status != "pass":
        finished_at = utc_now_timestamp()
        return GenericWebPreviewRefreshResult(
            refresh_status="blocked",
            refresh_started_at=started_at,
            refresh_finished_at=finished_at,
            product=resolved_profile.product,
            context=resolved_profile.preview.context,
            preview_slug=request.preview_slug,
            application_name=application_name,
            preview_url=preview_url,
            readiness=readiness,
            error_message="Generic web preview readiness blocked refresh.",
        )

    template_lane = _template_lane(profile=resolved_profile)
    if template_lane is None:
        raise click.ClickException("Generic web preview readiness passed without a template lane.")
    created_application_id = ""
    created_domain_id = ""
    stale_domain_ids: tuple[str, ...] = ()
    application_id = ""
    host = ""
    token = ""
    try:
        host, token = control_plane_dokploy.read_dokploy_config(
            control_plane_root=control_plane_root
        )
        target_definition, template_application, target_error = _read_template_payload(
            control_plane_root=control_plane_root,
            template_lane=template_lane,
        )
        if target_error or template_application is None or target_definition is None:
            raise click.ClickException(
                target_error or "Generic web preview template payload is unavailable."
            )
        _enforce_preview_copied_runtime_key_safety(
            record_store=record_store,
            profile=resolved_profile,
            template_lane=template_lane,
            template_application=template_application,
            preview_slug=request.preview_slug,
        )
        preview_runtime_identity = _build_preview_runtime_identity(
            profile=resolved_profile,
            preview_slug=request.preview_slug,
            image_reference=request.image_reference,
            anchor_head_sha=request.anchor_head_sha,
        )
        env_text = _render_preview_env_text(
            profile=resolved_profile,
            template_application=template_application,
            preview_url=preview_url,
            runtime_identity=preview_runtime_identity,
        )
        application, created_application = _ensure_application(
            host=host,
            token=token,
            application_name=application_name,
            app_name=f"{resolved_profile.product}-{request.preview_slug}",
            description=f"Preview environment for {resolved_profile.product} {request.preview_slug}",
            template_application=template_application,
        )
        application_id = str(application.get("applicationId") or "").strip()
        if created_application:
            created_application_id = application_id
        _configure_application(
            host=host,
            token=token,
            application=application,
            template_application=template_application,
            image_reference=request.image_reference,
            env_text=env_text,
        )
        created_domain_id, stale_domain_ids = _ensure_domain(
            host=host,
            token=token,
            application_id=application_id,
            preview_host=_preview_host(preview_url),
            runtime_port=resolved_profile.runtime_port,
        )
        latest_before = control_plane_dokploy.latest_deployment_for_target(
            host=host,
            token=token,
            target_type="application",
            target_id=application_id,
        )
        control_plane_dokploy.trigger_deployment(
            host=host,
            token=token,
            target_type="application",
            target_id=application_id,
            no_cache=request.no_cache,
        )
        control_plane_dokploy.wait_for_target_deployment(
            host=host,
            token=token,
            target_type="application",
            target_id=application_id,
            before_key=control_plane_dokploy.deployment_key(latest_before),
            timeout_seconds=request.timeout_seconds,
        )
        _wait_for_preview_health(
            preview_url=preview_url,
            health_path=resolved_profile.health_path,
            timeout_seconds=request.timeout_seconds,
            expected_runtime_identity=preview_runtime_identity,
        )
        for stale_domain_id in stale_domain_ids:
            _delete_domain(host=host, token=token, domain_id=stale_domain_id)
    except click.ClickException as exc:
        rollback_errors: list[str] = []
        if created_domain_id:
            try:
                _delete_domain(host=host, token=token, domain_id=created_domain_id)
            except click.ClickException as rollback_exc:
                rollback_errors.append(f"domain rollback failed: {rollback_exc}")
        if created_application_id:
            try:
                _delete_application(host=host, token=token, application_id=created_application_id)
            except click.ClickException as rollback_exc:
                rollback_errors.append(f"application rollback failed: {rollback_exc}")
        finished_at = utc_now_timestamp()
        message = str(exc)
        if rollback_errors:
            message = f"{message}\n" + "\n".join(rollback_errors)
        return GenericWebPreviewRefreshResult(
            refresh_status="fail",
            refresh_started_at=started_at,
            refresh_finished_at=finished_at,
            product=resolved_profile.product,
            context=resolved_profile.preview.context,
            preview_slug=request.preview_slug,
            application_name=application_name,
            application_id=application_id,
            preview_url=preview_url,
            readiness=readiness,
            error_message=message,
        )

    finished_at = utc_now_timestamp()
    return GenericWebPreviewRefreshResult(
        refresh_status="pass",
        refresh_started_at=started_at,
        refresh_finished_at=finished_at,
        product=resolved_profile.product,
        context=resolved_profile.preview.context,
        preview_slug=request.preview_slug,
        application_name=application_name,
        application_id=application_id,
        preview_url=preview_url,
        readiness=readiness,
    )


def discover_generic_web_preview_desired_state(
    *,
    control_plane_root: Path,
    record_store: GenericWebPreviewProfileStore,
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
    record_store: GenericWebPreviewProfileStore,
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
        application_id = str(
            application.get("applicationId") or application.get("id") or ""
        ).strip()
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
    record_store: GenericWebPreviewProfileStore,
    request: GenericWebPreviewDestroyRequest,
    profile: LaunchplaneProductProfileRecord | None = None,
) -> GenericWebPreviewDestroyResult:
    resolved_profile = profile
    if resolved_profile is None:
        resolved_profile = resolve_generic_web_preview_profile(
            record_store=record_store,
            product=request.product,
        )
    request = _destroy_request_with_resolved_preview_slug(
        profile=resolved_profile,
        request=request,
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
    if application is None:
        finished_at = utc_now_timestamp()
        return GenericWebPreviewDestroyResult(
            destroy_status="pass",
            destroy_started_at=started_at,
            destroy_finished_at=finished_at,
            product=resolved_profile.product,
            context=resolved_profile.preview.context,
            preview_slug=request.preview_slug,
            application_name=application_name,
            application_id="",
        )
    application_id = str(application.get("applicationId") or application.get("id") or "").strip()
    if not application_id:
        finished_at = utc_now_timestamp()
        return GenericWebPreviewDestroyResult(
            destroy_status="fail",
            destroy_started_at=started_at,
            destroy_finished_at=finished_at,
            product=resolved_profile.product,
            context=resolved_profile.preview.context,
            preview_slug=request.preview_slug,
            application_name=application_name,
            application_id="",
            error_message=(
                f"Preview application {application_name!r} exists but does not expose "
                "applicationId."
            ),
        )
    destroy_result = destroy_dokploy_preview_resource(
        host=host,
        token=token,
        resource_type="application",
        resource_id=application_id,
    )
    finished_at = utc_now_timestamp()
    if destroy_result.cleanup_errors:
        return GenericWebPreviewDestroyResult(
            destroy_status="fail",
            destroy_started_at=started_at,
            destroy_finished_at=finished_at,
            product=resolved_profile.product,
            context=resolved_profile.preview.context,
            preview_slug=request.preview_slug,
            application_name=application_name,
            application_id=application_id,
            error_message="; ".join(destroy_result.cleanup_errors),
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
