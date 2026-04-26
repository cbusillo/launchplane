from __future__ import annotations

import base64
import json
import secrets
import shlex
import time
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, unquote, urlparse, urlunparse
from urllib.request import Request, urlopen

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.dokploy import JsonObject
from control_plane.workflows.ship import utc_now_timestamp


DEFAULT_PREVIEW_TIMEOUT_SECONDS = 300
PREVIEW_APP_PREFIX = "ver-preview"
PREVIEW_DATABASE_PREFIX = "verireel_preview_"
PREVIEW_BASE_URL_ENV_KEY = "LAUNCHPLANE_PREVIEW_BASE_URL"


def _expected_preview_slug(anchor_pr_number: int) -> str:
    return f"pr-{anchor_pr_number}"


class VeriReelPreviewRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str = "verireel-testing"
    anchor_repo: str = "verireel"
    anchor_pr_number: int = Field(ge=1)
    anchor_pr_url: str
    anchor_head_sha: str
    preview_slug: str
    preview_url: str = ""
    image_reference: str
    timeout_seconds: int = Field(default=DEFAULT_PREVIEW_TIMEOUT_SECONDS, ge=1)

    @model_validator(mode="after")
    def _validate_request(self) -> "VeriReelPreviewRefreshRequest":
        if self.context != "verireel-testing":
            raise ValueError("VeriReel preview refresh requires context 'verireel-testing'.")
        if self.anchor_repo != "verireel":
            raise ValueError("VeriReel preview refresh requires anchor_repo 'verireel'.")
        if not self.anchor_pr_url.strip():
            raise ValueError("VeriReel preview refresh requires anchor_pr_url.")
        if not self.anchor_head_sha.strip():
            raise ValueError("VeriReel preview refresh requires anchor_head_sha.")
        if not self.preview_slug.strip():
            raise ValueError("VeriReel preview refresh requires preview_slug.")
        if self.preview_slug.strip() != _expected_preview_slug(self.anchor_pr_number):
            raise ValueError(
                "VeriReel preview refresh requires preview_slug to match anchor_pr_number."
            )
        if not self.image_reference.strip():
            raise ValueError("VeriReel preview refresh requires image_reference.")
        if self.preview_url.strip():
            _preview_url_host(self.preview_url)
        return self


class VeriReelPreviewDestroyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str = "verireel-testing"
    anchor_repo: str = "verireel"
    anchor_pr_number: int = Field(ge=1)
    preview_slug: str
    destroy_reason: str
    timeout_seconds: int = Field(default=DEFAULT_PREVIEW_TIMEOUT_SECONDS, ge=1)

    @model_validator(mode="after")
    def _validate_request(self) -> "VeriReelPreviewDestroyRequest":
        if self.context != "verireel-testing":
            raise ValueError("VeriReel preview destroy requires context 'verireel-testing'.")
        if self.anchor_repo != "verireel":
            raise ValueError("VeriReel preview destroy requires anchor_repo 'verireel'.")
        if not self.preview_slug.strip():
            raise ValueError("VeriReel preview destroy requires preview_slug.")
        if self.preview_slug.strip() != _expected_preview_slug(self.anchor_pr_number):
            raise ValueError(
                "VeriReel preview destroy requires preview_slug to match anchor_pr_number."
            )
        if not self.destroy_reason.strip():
            raise ValueError("VeriReel preview destroy requires destroy_reason.")
        return self


class VeriReelPreviewRefreshResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_status: Literal["pass", "fail"]
    refresh_started_at: str
    refresh_finished_at: str
    application_name: str
    application_id: str
    preview_url: str
    error_message: str = ""


class VeriReelPreviewDestroyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destroy_status: Literal["pass", "fail"]
    destroy_started_at: str
    destroy_finished_at: str
    application_name: str
    application_id: str
    preview_url: str
    error_message: str = ""


class VeriReelPreviewInventoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str = "verireel-testing"

    @model_validator(mode="after")
    def _validate_request(self) -> "VeriReelPreviewInventoryRequest":
        if self.context != "verireel-testing":
            raise ValueError("VeriReel preview inventory requires context 'verireel-testing'.")
        return self


class VeriReelPreviewInventoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applicationId: str
    applicationName: str
    previewSlug: str


class VeriReelPreviewInventoryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    previews: tuple[VeriReelPreviewInventoryItem, ...]


class _DatabaseParts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str
    port: int
    database: str
    username: str
    password: str


def _preview_app_name(preview_slug: str) -> str:
    return f"{PREVIEW_APP_PREFIX}-{preview_slug}"


def _preview_application_name(preview_slug: str) -> str:
    return f"{_preview_app_name(preview_slug)}-app"


def _preview_slug_from_application_name(application_name: str) -> str:
    prefix = f"{PREVIEW_APP_PREFIX}-"
    suffix = "-app"
    if not application_name.startswith(prefix) or not application_name.endswith(suffix):
        return ""
    return application_name[len(prefix) : -len(suffix)]


def _preview_database_identifiers(preview_slug: str) -> tuple[str, str]:
    normalized_slug = preview_slug.strip().lower().replace("-", "_")
    identifier = f"{PREVIEW_DATABASE_PREFIX}{normalized_slug}"[:63]
    return identifier, identifier


def _preview_url_host(preview_url: str) -> str:
    parsed = urlparse(preview_url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Preview URL must use http or https.")
    if not parsed.hostname:
        raise ValueError("Preview URL requires a hostname.")
    return parsed.hostname


def _preview_url_from_base_url(*, preview_slug: str, preview_base_url: str) -> str:
    parsed = urlparse(preview_base_url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise click.ClickException(f"{PREVIEW_BASE_URL_ENV_KEY} must use http or https.")
    if not parsed.hostname:
        raise click.ClickException(f"{PREVIEW_BASE_URL_ENV_KEY} requires a hostname.")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise click.ClickException(
            f"{PREVIEW_BASE_URL_ENV_KEY} must be a root URL without path, query, or fragment."
        )
    return urlunparse(
        parsed._replace(
            netloc=f"{preview_slug}.{parsed.hostname}",
            path="",
            params="",
            query="",
            fragment="",
        )
    )


def _resolve_preview_base_url(*, control_plane_root: Path, context_name: str) -> str:
    resolved_values = control_plane_runtime_environments.resolve_runtime_context_values(
        control_plane_root=control_plane_root,
        context_name=context_name,
    )
    preview_base_url = str(resolved_values.get(PREVIEW_BASE_URL_ENV_KEY) or "").strip()
    if not preview_base_url:
        raise click.ClickException(
            f"Missing {PREVIEW_BASE_URL_ENV_KEY} in Launchplane runtime-environment records for {context_name}."
        )
    return preview_base_url


def _resolve_preview_url(*, control_plane_root: Path, request: VeriReelPreviewRefreshRequest) -> str:
    if request.preview_url.strip():
        return request.preview_url.strip()
    return _preview_url_from_base_url(
        preview_slug=request.preview_slug.strip(),
        preview_base_url=_resolve_preview_base_url(
            control_plane_root=control_plane_root,
            context_name=request.context,
        ),
    )


def _resolve_preview_url_for_destroy(
    *, control_plane_root: Path, request: VeriReelPreviewDestroyRequest
) -> str:
    return _preview_url_from_base_url(
        preview_slug=request.preview_slug.strip(),
        preview_base_url=_resolve_preview_base_url(
            control_plane_root=control_plane_root,
            context_name=request.context,
        ),
    )


def _preview_domain_from_url(preview_url: str) -> str:
    host = _preview_url_host(preview_url)
    if "." not in host:
        raise ValueError("Preview URL hostname must include a preview domain suffix.")
    return host.split(".", 1)[1]


def _parse_database_url(database_url: str) -> _DatabaseParts:
    parsed = urlparse(database_url.strip())
    if not parsed.scheme:
        raise click.ClickException("Preview template DATABASE_URL must include a scheme.")
    if not parsed.hostname:
        raise click.ClickException("Preview template DATABASE_URL must include a hostname.")
    database_name = parsed.path.lstrip("/").strip()
    if not database_name:
        raise click.ClickException("Preview template DATABASE_URL must include a database name.")
    username = unquote(parsed.username or "").strip()
    password = unquote(parsed.password or "")
    if not username:
        raise click.ClickException("Preview template DATABASE_URL must include a username.")
    return _DatabaseParts(
        host=parsed.hostname,
        port=parsed.port or 5432,
        database=database_name,
        username=username,
        password=password,
    )


def _build_admin_database_url(database_url: str) -> str:
    parsed = urlparse(database_url.strip())
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "schema"
    ]
    return urlunparse(
        parsed._replace(
            path="/postgres",
            query="&".join(f"{quote(key)}={quote(value)}" for key, value in query_items),
        )
    )


def _build_preview_database_url(
    *, host: str, port: int, database_name: str, role_name: str, password: str
) -> str:
    encoded_role = quote(role_name, safe="")
    encoded_password = quote(password, safe="")
    encoded_database = quote(database_name, safe="")
    return f"postgresql://{encoded_role}:{encoded_password}@{host}:{port}/{encoded_database}?schema=public"


def _random_password(length: int = 32) -> str:
    return secrets.token_hex(length)[:length]


def _template_application_payload(
    *, control_plane_root: Path, host: str, token: str
) -> tuple[control_plane_dokploy.DokployTargetDefinition, JsonObject]:
    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    target_definition = control_plane_dokploy.find_dokploy_target_definition(
        source_of_truth,
        context_name="verireel",
        instance_name="testing",
    )
    if target_definition is None:
        raise click.ClickException("No Dokploy target definition found for verireel/testing.")
    if target_definition.target_type != "application":
        raise click.ClickException(
            "VeriReel preview driver requires the testing target to be a Dokploy application."
        )
    if not target_definition.target_id.strip():
        raise click.ClickException(
            "VeriReel testing target requires a Dokploy target_id before preview execution."
        )
    payload = control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type="application",
        target_id=target_definition.target_id,
    )
    return target_definition, payload


def _find_application_by_name(*, host: str, token: str, application_name: str) -> JsonObject | None:
    raw_projects = control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/project.all",
    )
    if not isinstance(raw_projects, list):
        return None
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
                if application is None:
                    continue
                if str(application.get("name") or "").strip() == application_name:
                    return application
    return None


def execute_verireel_preview_inventory(
    *,
    control_plane_root: Path,
    request: VeriReelPreviewInventoryRequest,
) -> VeriReelPreviewInventoryResult:
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    raw_projects = control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/project.all",
    )
    if not isinstance(raw_projects, list):
        raise click.ClickException("Dokploy project inventory returned an invalid response payload.")
    preview_items: list[VeriReelPreviewInventoryItem] = []
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
                if application is None:
                    continue
                application_name = str(application.get("name") or "").strip()
                preview_slug = _preview_slug_from_application_name(application_name)
                if not preview_slug:
                    continue
                application_id = str(application.get("applicationId") or application.get("id") or "").strip()
                if not application_id:
                    continue
                preview_items.append(
                    VeriReelPreviewInventoryItem(
                        applicationId=application_id,
                        applicationName=application_name,
                        previewSlug=preview_slug,
                    )
                )
    return VeriReelPreviewInventoryResult(
        context=request.context,
        previews=tuple(sorted(preview_items, key=lambda item: item.previewSlug)),
    )


def _fetch_application(*, host: str, token: str, application_id: str) -> JsonObject:
    payload = control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/application.one",
        query={"applicationId": application_id},
    )
    application = control_plane_dokploy.as_json_object(payload)
    if application is None:
        raise click.ClickException(
            f"Dokploy application {application_id!r} returned an invalid payload."
        )
    return application


def _ensure_application(
    *,
    host: str,
    token: str,
    application_name: str,
    app_name: str,
    description: str,
    template_application: JsonObject,
) -> JsonObject:
    existing = _find_application_by_name(host=host, token=token, application_name=application_name)
    if existing is not None:
        application_id = str(existing.get("applicationId") or "").strip()
        if not application_id:
            raise click.ClickException(
                f"Dokploy application {application_name!r} exists but does not expose an applicationId."
            )
        return _fetch_application(host=host, token=token, application_id=application_id)

    environment_id = str(template_application.get("environmentId") or "").strip()
    server_id = str(template_application.get("serverId") or "").strip()
    if not environment_id:
        raise click.ClickException(
            "VeriReel preview driver could not resolve the Dokploy testing environmentId."
        )
    if not server_id:
        raise click.ClickException(
            "VeriReel preview driver could not resolve the Dokploy testing serverId."
        )
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
    return _fetch_application(host=host, token=token, application_id=created_application_id)


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
            "endpointSpecSwarm": template_application.get("endpointSpecSwarm"),
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


def _restore_existing_application(
    *,
    host: str,
    token: str,
    application_snapshot: JsonObject,
    timeout_seconds: int,
) -> None:
    _configure_application(
        host=host,
        token=token,
        application=application_snapshot,
        template_application=application_snapshot,
        image_reference=str(application_snapshot.get("dockerImage") or "").strip(),
        env_text=str(application_snapshot.get("env") or ""),
    )
    application_id = str(application_snapshot.get("applicationId") or "").strip()
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
        no_cache=False,
    )
    control_plane_dokploy.wait_for_target_deployment(
        host=host,
        token=token,
        target_type="application",
        target_id=application_id,
        before_key=control_plane_dokploy.deployment_key(latest_before),
        timeout_seconds=timeout_seconds,
    )


def _ensure_domain(
    *, host: str, token: str, application_id: str, preview_host: str
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
        "port": 3000,
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
        control_plane_dokploy.dokploy_request(
            host=host,
            token=token,
            path="/api/domain.update",
            method="POST",
            payload={"domainId": existing_domain_id, **payload},
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


def _find_application_schedule(
    *, host: str, token: str, application_id: str, schedule_name: str
) -> JsonObject | None:
    for schedule in control_plane_dokploy.list_dokploy_schedules(
        host=host,
        token=token,
        target_id=application_id,
        schedule_type="application",
    ):
        if str(schedule.get("name") or "").strip() == schedule_name:
            return schedule
    return None


def _upsert_application_schedule(
    *,
    host: str,
    token: str,
    application_id: str,
    schedule_name: str,
    command: str,
) -> str:
    existing = _find_application_schedule(
        host=host,
        token=token,
        application_id=application_id,
        schedule_name=schedule_name,
    )
    payload: JsonObject = {
        "name": schedule_name,
        "cronExpression": control_plane_dokploy.DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
        "scheduleType": "application",
        "shellType": "sh",
        "command": command,
        "applicationId": application_id,
        "enabled": False,
        "timezone": "UTC",
    }
    if existing is None:
        control_plane_dokploy.dokploy_request(
            host=host,
            token=token,
            path="/api/schedule.create",
            method="POST",
            payload=payload,
        )
    else:
        control_plane_dokploy.dokploy_request(
            host=host,
            token=token,
            path="/api/schedule.update",
            method="POST",
            payload={"scheduleId": control_plane_dokploy.schedule_key(existing), **payload},
        )
    resolved = _find_application_schedule(
        host=host,
        token=token,
        application_id=application_id,
        schedule_name=schedule_name,
    )
    if resolved is None:
        raise click.ClickException(
            f"Dokploy schedule {schedule_name!r} for preview application {application_id!r} could not be resolved."
        )
    schedule_id = control_plane_dokploy.schedule_key(resolved)
    if not schedule_id:
        raise click.ClickException(
            f"Dokploy schedule {schedule_name!r} for preview application {application_id!r} did not expose a schedule id."
        )
    return schedule_id


def _run_application_command(
    *,
    host: str,
    token: str,
    application_id: str,
    schedule_name: str,
    command: str,
    timeout_seconds: int,
) -> None:
    schedule_id = _upsert_application_schedule(
        host=host,
        token=token,
        application_id=application_id,
        schedule_name=schedule_name,
        command=command,
    )
    latest_before = control_plane_dokploy.latest_deployment_for_schedule(
        host=host,
        token=token,
        schedule_id=schedule_id,
    )
    control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/schedule.runManually",
        method="POST",
        payload={"scheduleId": schedule_id},
        timeout_seconds=timeout_seconds,
    )
    control_plane_dokploy.wait_for_dokploy_schedule_deployment(
        host=host,
        token=token,
        schedule_id=schedule_id,
        before_key=control_plane_dokploy.deployment_key(latest_before),
        timeout_seconds=timeout_seconds,
    )


def _run_application_command_with_retries(
    *,
    host: str,
    token: str,
    application_id: str,
    schedule_name: str,
    command: str,
    timeout_seconds: int,
    attempts: int = 4,
    retry_delay_seconds: float = 5.0,
) -> None:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    for attempt in range(1, attempts + 1):
        try:
            _run_application_command(
                host=host,
                token=token,
                application_id=application_id,
                schedule_name=schedule_name,
                command=command,
                timeout_seconds=timeout_seconds,
            )
            return
        except click.ClickException:
            if attempt >= attempts:
                raise
            time.sleep(retry_delay_seconds)


def _preview_database_admin_module_source() -> str:
    return "".join(
        (
            "#!/usr/bin/env node\n",
            'import { createRequire } from "node:module";\n',
            'import { pathToFileURL } from "node:url";\n',
            "const require = createRequire(`${process.cwd()}/package.json`);\n",
            'const { Client } = require("pg");\n',
            'function quoteIdentifier(value) { return "\\"" + String(value).split("\\"").join("\\"\\"") + "\\""; }\n',
            'function quoteLiteral(value) { return "\'" + String(value).split("\'").join("\'\'") + "\'"; }\n',
            "function shouldFallbackToLegacyDrop(error) {\n",
            '  const message = String(error?.message || "").toLowerCase();\n',
            '  return message.includes("syntax error") || message.includes("with (force)") || message.includes("force)");\n',
            "}\n",
            "function parseArgs(argv) {\n",
            '  const options = { action: "", adminDatabaseUrl: "", databaseName: "", roleName: "", password: "" };\n',
            "  for (let index = 0; index < argv.length; index += 1) {\n",
            "    const arg = argv[index];\n",
            '    const value = argv[index + 1] ?? "";\n',
            '    if (arg === "--action") { options.action = value; index += 1; continue; }\n',
            '    if (arg === "--admin-database-url") { options.adminDatabaseUrl = value; index += 1; continue; }\n',
            '    if (arg === "--database-name") { options.databaseName = value; index += 1; continue; }\n',
            '    if (arg === "--role-name") { options.roleName = value; index += 1; continue; }\n',
            '    if (arg === "--password") { options.password = value; index += 1; continue; }\n',
            "    throw new Error(`Unknown option: ${arg}`);\n",
            "  }\n",
            '  if (!["ensure", "drop"].includes(options.action)) { throw new Error("--action must be one of ensure or drop."); }\n',
            '  if (!options.adminDatabaseUrl) { throw new Error("Missing required --admin-database-url value."); }\n',
            '  if (!options.databaseName) { throw new Error("Missing required --database-name value."); }\n',
            '  if (!options.roleName) { throw new Error("Missing required --role-name value."); }\n',
            '  if (options.action === "ensure" && !options.password) { throw new Error("Missing required --password value for ensure."); }\n',
            "  return options;\n",
            "}\n",
            "async function ensureDatabase(client, databaseName, roleName, password) {\n",
            '  const existingRole = await client.query("SELECT 1 FROM pg_roles WHERE rolname = $1", [roleName]);\n',
            "  if (existingRole.rowCount === 0) {\n",
            "    await client.query(`CREATE ROLE ${quoteIdentifier(roleName)} LOGIN PASSWORD ${quoteLiteral(password)}`);\n",
            "  } else {\n",
            "    await client.query(`ALTER ROLE ${quoteIdentifier(roleName)} WITH LOGIN PASSWORD ${quoteLiteral(password)}`);\n",
            "  }\n",
            '  const existingDatabase = await client.query("SELECT 1 FROM pg_database WHERE datname = $1", [databaseName]);\n',
            "  if (existingDatabase.rowCount === 0) {\n",
            "    await client.query(`CREATE DATABASE ${quoteIdentifier(databaseName)} OWNER ${quoteIdentifier(roleName)}`);\n",
            "  }\n",
            "  await client.query(`GRANT ALL PRIVILEGES ON DATABASE ${quoteIdentifier(databaseName)} TO ${quoteIdentifier(roleName)}`);\n",
            "}\n",
            "async function dropDatabase(client, databaseName, roleName) {\n",
            "  try {\n",
            "    await client.query(`DROP DATABASE IF EXISTS ${quoteIdentifier(databaseName)} WITH (FORCE)`);\n",
            "  } catch (error) {\n",
            "    if (!shouldFallbackToLegacyDrop(error)) {\n",
            "      throw error;\n",
            "    }\n",
            '    await client.query("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()", [databaseName]);\n',
            "    await client.query(`DROP DATABASE IF EXISTS ${quoteIdentifier(databaseName)}`);\n",
            "  }\n",
            "  await client.query(`DROP ROLE IF EXISTS ${quoteIdentifier(roleName)}`);\n",
            "}\n",
            "export async function main(argv = process.argv.slice(2)) {\n",
            "  const options = parseArgs(argv);\n",
            "  const client = new Client({ connectionString: options.adminDatabaseUrl });\n",
            "  await client.connect();\n",
            "  try {\n",
            '    if (options.action === "ensure") {\n',
            "      await ensureDatabase(client, options.databaseName, options.roleName, options.password);\n",
            "      return;\n",
            "    }\n",
            "    await dropDatabase(client, options.databaseName, options.roleName);\n",
            "  } finally {\n",
            "    await client.end();\n",
            "  }\n",
            "}\n",
            "if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {\n",
            "  main().catch((error) => {\n",
            "    console.error(error instanceof Error ? error.message : String(error));\n",
            "    process.exit(1);\n",
            "  });\n",
            "}\n",
        )
    )


def _preview_database_admin_runner_source() -> str:
    return "".join(
        (
            'import { pathToFileURL } from "node:url";\n',
            'const argv = JSON.parse(Buffer.from(process.env.PREVIEW_DB_ARGS_BASE64 || "", "base64").toString("utf8"));\n',
            "const bundledScriptPath = process.argv[2];\n",
            'process.argv[1] = "";\n',
            "const bundled = await import(pathToFileURL(bundledScriptPath).href);\n",
            "process.argv[1] = bundledScriptPath;\n",
            "await bundled.main(argv);\n",
        )
    )


def _build_preview_database_command(
    *,
    action: Literal["ensure", "drop"],
    admin_database_url: str,
    database_name: str,
    role_name: str,
    password: str = "",
) -> str:
    suffix = secrets.token_hex(6)
    temp_script = f"/tmp/.preview-db-admin-{suffix}.mjs"
    temp_runner = f"/tmp/.preview-db-admin-runner-{suffix}.mjs"
    module_source = _preview_database_admin_module_source().encode("utf-8")
    runner_source = _preview_database_admin_runner_source().encode("utf-8")
    module_b64 = base64.b64encode(module_source).decode("ascii")
    runner_b64 = base64.b64encode(runner_source).decode("ascii")
    argv = [
        "--action",
        action,
        "--admin-database-url",
        admin_database_url,
        "--database-name",
        database_name,
        "--role-name",
        role_name,
    ]
    if action == "ensure":
        argv.extend(["--password", password])
    argv_b64 = base64.b64encode(json.dumps(argv, separators=(",", ":")).encode("utf-8")).decode(
        "ascii"
    )
    return (
        f'temp_script="{temp_script}"; '
        f'temp_runner="{temp_runner}"; '
        f"status=0; "
        f'printf "%s" {shlex.quote(module_b64)} | base64 -d > "$temp_script"; '
        f"status=$?; "
        f'if [ "$status" -eq 0 ]; then printf "%s" {shlex.quote(runner_b64)} | base64 -d > "$temp_runner"; status=$?; fi; '
        f'if [ "$status" -eq 0 ]; then PREVIEW_DB_ARGS_BASE64={shlex.quote(argv_b64)} node "$temp_runner" "$temp_script"; status=$?; fi; '
        f'rm -f "$temp_script" "$temp_runner" || true; '
        f'exit "$status"'
    )


def _wait_for_preview_health(*, preview_url: str, timeout_seconds: int) -> None:
    health_url = f"{preview_url.rstrip('/')}/api/health"
    deadline = timeout_seconds
    request = Request(
        health_url,
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-store",
        },
    )
    while deadline > 0:
        try:
            with urlopen(request, timeout=min(15, deadline)) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("ok") is True:
                return
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ValueError):
            pass
        sleep_seconds = min(5, deadline)
        time.sleep(sleep_seconds)
        deadline -= sleep_seconds
    raise click.ClickException(f"Timed out waiting for {health_url} to report ok=true.")


def _resolve_existing_preview_database(
    existing_application: JsonObject | None,
) -> _DatabaseParts | None:
    if existing_application is None:
        return None
    database_url = control_plane_dokploy.parse_dokploy_env_text(
        str(existing_application.get("env") or "")
    ).get(
        "DATABASE_URL",
        "",
    )
    if not database_url:
        return None
    return _parse_database_url(database_url)


def execute_verireel_preview_refresh(
    *,
    control_plane_root: Path,
    request: VeriReelPreviewRefreshRequest,
) -> VeriReelPreviewRefreshResult:
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    template_target, template_application = _template_application_payload(
        control_plane_root=control_plane_root,
        host=host,
        token=token,
    )
    template_env_map = control_plane_dokploy.parse_dokploy_env_text(
        str(template_application.get("env") or "")
    )
    template_database_url = str(template_env_map.get("DATABASE_URL") or "").strip()
    if not template_database_url:
        raise click.ClickException("VeriReel testing template application is missing DATABASE_URL.")
    template_database = _parse_database_url(template_database_url)

    started_at = utc_now_timestamp()
    preview_url = _resolve_preview_url(control_plane_root=control_plane_root, request=request)
    application_name = _preview_application_name(request.preview_slug)
    app_name = _preview_app_name(request.preview_slug)
    preview_host = _preview_url_host(preview_url)
    preview_domain = _preview_domain_from_url(preview_url)
    existing_application = _find_application_by_name(
        host=host, token=token, application_name=application_name
    )
    existing_snapshot = None
    if existing_application is not None:
        application_id = str(existing_application.get("applicationId") or "").strip()
        existing_snapshot = _fetch_application(
            host=host, token=token, application_id=application_id
        )
    existing_database = _resolve_existing_preview_database(existing_snapshot)
    database_name, role_name = _preview_database_identifiers(request.preview_slug)
    database_parts = existing_database or _DatabaseParts(
        host=template_database.host,
        port=template_database.port,
        database=database_name,
        username=role_name,
        password=_random_password(),
    )
    created_preview_database = existing_database is None
    created_application_id = ""
    created_domain_id = ""
    stale_domain_ids: tuple[str, ...] = ()
    migration_started = False

    try:
        admin_database_url = _build_admin_database_url(template_database_url)
        _run_application_command(
            host=host,
            token=token,
            application_id=template_target.target_id,
            schedule_name=f"{template_target.target_name}-preview-db-bootstrap",
            command=_build_preview_database_command(
                action="ensure",
                admin_database_url=admin_database_url,
                database_name=database_parts.database,
                role_name=database_parts.username,
                password=database_parts.password,
            ),
            timeout_seconds=request.timeout_seconds,
        )
        env_text = control_plane_dokploy.render_dokploy_env_text_with_overrides(
            str(template_application.get("env") or ""),
            updates={
                "VERIREEL_APP_URL": preview_url,
                "BETTER_AUTH_URL": preview_url,
                "DOKPLOY_PREVIEW_DOMAIN": preview_domain,
                "NEXT_PUBLIC_DOKPLOY_PREVIEW_DOMAIN": preview_domain,
                "DATABASE_URL": _build_preview_database_url(
                    host=database_parts.host,
                    port=database_parts.port,
                    database_name=database_parts.database,
                    role_name=database_parts.username,
                    password=database_parts.password,
                ),
            },
        )
        application = _ensure_application(
            host=host,
            token=token,
            application_name=application_name,
            app_name=app_name,
            description=f"Preview environment for {request.preview_slug}",
            template_application=template_application,
        )
        application_id = str(application.get("applicationId") or "").strip()
        if existing_snapshot is None:
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
            preview_host=preview_host,
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
            no_cache=False,
        )
        control_plane_dokploy.wait_for_target_deployment(
            host=host,
            token=token,
            target_type="application",
            target_id=application_id,
            before_key=control_plane_dokploy.deployment_key(latest_before),
            timeout_seconds=request.timeout_seconds,
        )
        migration_started = True
        _run_application_command_with_retries(
            host=host,
            token=token,
            application_id=application_id,
            schedule_name=f"{app_name}-migrate",
            command="npx prisma migrate deploy --config prisma.config.ts",
            timeout_seconds=request.timeout_seconds,
        )
        _run_application_command_with_retries(
            host=host,
            token=token,
            application_id=application_id,
            schedule_name=f"{app_name}-seed",
            command="node prisma/seed.mjs",
            timeout_seconds=request.timeout_seconds,
        )
        _wait_for_preview_health(preview_url=preview_url, timeout_seconds=request.timeout_seconds)
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
        if existing_snapshot is not None and not migration_started:
            try:
                _restore_existing_application(
                    host=host,
                    token=token,
                    application_snapshot=existing_snapshot,
                    timeout_seconds=request.timeout_seconds,
                )
            except click.ClickException as rollback_exc:
                rollback_errors.append(f"existing preview rollback failed: {rollback_exc}")
        if created_preview_database:
            try:
                _run_application_command(
                    host=host,
                    token=token,
                    application_id=template_target.target_id,
                    schedule_name=f"{template_target.target_name}-preview-db-rollback",
                    command=_build_preview_database_command(
                        action="drop",
                        admin_database_url=_build_admin_database_url(template_database_url),
                        database_name=database_parts.database,
                        role_name=database_parts.username,
                    ),
                    timeout_seconds=request.timeout_seconds,
                )
            except click.ClickException as rollback_exc:
                rollback_errors.append(f"database rollback failed: {rollback_exc}")
        finished_at = utc_now_timestamp()
        message = str(exc)
        if rollback_errors:
            message = f"{message}\n" + "\n".join(rollback_errors)
        return VeriReelPreviewRefreshResult(
            refresh_status="fail",
            refresh_started_at=started_at,
            refresh_finished_at=finished_at,
            application_name=application_name,
            application_id=created_application_id
            or str((existing_snapshot or {}).get("applicationId") or "").strip(),
            preview_url=preview_url,
            error_message=message,
        )

    finished_at = utc_now_timestamp()
    resolved_application = _find_application_by_name(
        host=host, token=token, application_name=application_name
    )
    return VeriReelPreviewRefreshResult(
        refresh_status="pass",
        refresh_started_at=started_at,
        refresh_finished_at=finished_at,
        application_name=application_name,
        application_id=str((resolved_application or {}).get("applicationId") or "").strip(),
        preview_url=preview_url,
    )


def execute_verireel_preview_destroy(
    *,
    control_plane_root: Path,
    request: VeriReelPreviewDestroyRequest,
) -> VeriReelPreviewDestroyResult:
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    template_target, template_application = _template_application_payload(
        control_plane_root=control_plane_root,
        host=host,
        token=token,
    )
    template_env_map = control_plane_dokploy.parse_dokploy_env_text(
        str(template_application.get("env") or "")
    )
    template_database_url = str(template_env_map.get("DATABASE_URL") or "").strip()
    if not template_database_url:
        raise click.ClickException("VeriReel testing template application is missing DATABASE_URL.")

    started_at = utc_now_timestamp()
    preview_url = _resolve_preview_url_for_destroy(control_plane_root=control_plane_root, request=request)
    application_name = _preview_application_name(request.preview_slug)
    application = _find_application_by_name(
        host=host, token=token, application_name=application_name
    )
    application_id = str((application or {}).get("applicationId") or "").strip()
    database_name, role_name = _preview_database_identifiers(request.preview_slug)
    existing_database = None
    if application_id:
        application_payload = _fetch_application(
            host=host, token=token, application_id=application_id
        )
        existing_database = _resolve_existing_preview_database(application_payload)
    database_parts = existing_database or _DatabaseParts(
        host=_parse_database_url(template_database_url).host,
        port=_parse_database_url(template_database_url).port,
        database=database_name,
        username=role_name,
        password="",
    )

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
    try:
        _run_application_command_with_retries(
            host=host,
            token=token,
            application_id=template_target.target_id,
            schedule_name=f"{template_target.target_name}-preview-db-drop",
            command=_build_preview_database_command(
                action="drop",
                admin_database_url=_build_admin_database_url(template_database_url),
                database_name=database_parts.database,
                role_name=database_parts.username,
            ),
            timeout_seconds=request.timeout_seconds,
        )
    except click.ClickException as exc:
        cleanup_errors.append(f"database cleanup failed: {exc}")

    finished_at = utc_now_timestamp()
    if cleanup_errors:
        return VeriReelPreviewDestroyResult(
            destroy_status="fail",
            destroy_started_at=started_at,
            destroy_finished_at=finished_at,
            application_name=application_name,
            application_id=application_id,
            preview_url=preview_url,
            error_message="; ".join(cleanup_errors),
        )
    return VeriReelPreviewDestroyResult(
        destroy_status="pass",
        destroy_started_at=started_at,
        destroy_finished_at=finished_at,
        application_name=application_name,
        application_id=application_id,
        preview_url=preview_url,
    )
