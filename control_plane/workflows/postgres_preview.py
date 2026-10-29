from __future__ import annotations

from typing import cast

import base64
import json
import hashlib
import secrets
import shlex
import time
from pathlib import Path
from typing import Callable, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, unquote, urlparse
from urllib.request import Request, urlopen

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.runtime_platform_credentials import looks_like_credential
from control_plane.contracts.runtime_identity import (
    RuntimeIdentity,
    health_payload_runtime_identity_status,
    runtime_identity_env,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.postgres_preview import effective_postgres_preview_profile
from control_plane.workflows.preview_resource_destroy import (
    PREVIEW_CLEANUP_SAFETY_UNKNOWN,
    destroy_dokploy_preview_resource,
)
from control_plane.workflows.generic_web_application_lifecycle import (
    DokployApplicationLifecycle,
    preview_application_lifecycle,
    recover_preview_application_lifecycle,
)
from control_plane.workflows.ship import generate_deployment_record_id, utc_now_timestamp
from control_plane.workflows.generic_web_preview import (
    _configure_application,
    _ensure_domain,
    _delete_application,
    _delete_domain,
)
from control_plane.preview_namespaces import require_unique_preview_namespaces
from control_plane.contracts.snapshot_protocol import snapshot_protocol_default
from control_plane import runtime_platform_credentials
from control_plane.dokploy import memory_capacity
from control_plane.runtime_environments import RuntimeEnvironmentRecordStore

from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import source as dokploy_source
from control_plane.dokploy import post_deploy as dokploy_post_deploy
from control_plane.dokploy.api import JsonObject


DEFAULT_PREVIEW_TIMEOUT_SECONDS = 300
PREVIEW_BASE_URL_ENV_KEY = "LAUNCHPLANE_PREVIEW_BASE_URL"
_HARD_RUNTIME_IDENTITY_FIELDS = frozenset({"product", "context", "instance", "environment_kind"})


def _build_preview_runtime_identity(
    *,
    request: "PostgresPreviewRefreshRequest",
    preview_id: str = "",
    preview_generation_id: str = "",
    deployed_at: str = "",
    product: str,
) -> RuntimeIdentity:
    return RuntimeIdentity(
        product=product,
        context=request.context,
        instance=request.preview_slug,
        environment_kind="preview",
        deployment_record_id=generate_deployment_record_id(
            context_name=request.context,
            instance_name=request.preview_slug,
        ),
        artifact_id=request.image_reference,
        source_git_ref=request.anchor_head_sha,
        image_reference=request.image_reference,
        preview_id=preview_id.strip() or request.preview_slug,
        preview_generation_id=preview_generation_id.strip(),
        deployed_at=deployed_at,
    )


class PostgresPreviewRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    anchor_repo: str
    anchor_pr_number: int = Field(ge=1)
    anchor_pr_url: str
    anchor_head_sha: str
    preview_slug: str
    slug_template: str = snapshot_protocol_default("pr-{number}")
    preview_url: str = ""
    image_reference: str
    timeout_seconds: int = Field(default=DEFAULT_PREVIEW_TIMEOUT_SECONDS, ge=1)

    @model_validator(mode="after")
    def _validate_request(self) -> "PostgresPreviewRefreshRequest":
        if not self.context.strip():
            raise ValueError("PostgreSQL preview refresh requires context.")
        if not self.anchor_repo.strip():
            raise ValueError("PostgreSQL preview refresh requires anchor_repo.")
        if not self.anchor_pr_url.strip():
            raise ValueError("PostgreSQL preview refresh requires anchor_pr_url.")
        if not self.anchor_head_sha.strip():
            raise ValueError("PostgreSQL preview refresh requires anchor_head_sha.")
        if not self.preview_slug.strip():
            raise ValueError("PostgreSQL preview refresh requires preview_slug.")
        if self.preview_slug.strip() != self.slug_template.strip().replace(
            "{number}", str(self.anchor_pr_number)
        ):
            raise ValueError(
                "PostgreSQL preview refresh requires preview_slug to match anchor_pr_number."
            )
        if not self.image_reference.strip():
            raise ValueError("PostgreSQL preview refresh requires image_reference.")
        if self.preview_url.strip():
            _preview_url_host(self.preview_url)
        return self


class PostgresPreviewDestroyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    anchor_repo: str
    anchor_pr_url: str = ""
    anchor_pr_number: int = Field(ge=1)
    preview_slug: str
    slug_template: str = snapshot_protocol_default("pr-{number}")
    destroy_reason: str
    timeout_seconds: int = Field(default=DEFAULT_PREVIEW_TIMEOUT_SECONDS, ge=1)

    @model_validator(mode="after")
    def _validate_request(self) -> "PostgresPreviewDestroyRequest":
        if not self.context.strip():
            raise ValueError("PostgreSQL preview destroy requires context.")
        if not self.anchor_repo.strip():
            raise ValueError("PostgreSQL preview destroy requires anchor_repo.")
        if not self.preview_slug.strip():
            raise ValueError("PostgreSQL preview destroy requires preview_slug.")
        if self.preview_slug.strip() != self.slug_template.strip().replace(
            "{number}", str(self.anchor_pr_number)
        ):
            raise ValueError(
                "PostgreSQL preview destroy requires preview_slug to match anchor_pr_number."
            )
        if not self.destroy_reason.strip():
            raise ValueError("PostgreSQL preview destroy requires destroy_reason.")
        return self


class PostgresPreviewRefreshResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_status: Literal["pass", "fail"]
    refresh_started_at: str
    refresh_finished_at: str
    application_name: str
    application_id: str
    preview_url: str
    runtime_identity: RuntimeIdentity | None = None
    error_message: str = ""


class PostgresPreviewRefreshConfigError(click.ClickException):
    pass


class PostgresPreviewRefreshTransportError(click.ClickException):
    pass


class PostgresPreviewDestroyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destroy_status: Literal["pass", "fail"]
    destroy_started_at: str
    destroy_finished_at: str
    application_name: str
    application_id: str
    preview_url: str
    error_message: str = ""


class _DatabaseParts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str
    port: int
    database: str
    username: str
    password: str


class _PreviewNamespaceStore(Protocol):
    def list_product_profile_records(self) -> tuple[LaunchplaneProductProfileRecord, ...]: ...
    def list_preview_records(self) -> tuple[PreviewRecord, ...]: ...


def _require_unique_preview_namespaces(
    record_store: object | None, profile: LaunchplaneProductProfileRecord
) -> None:
    if not callable(getattr(record_store, "list_product_profile_records", None)):
        raise PostgresPreviewRefreshConfigError(
            "Preview namespace ownership requires product profile inventory."
        )
    if not callable(getattr(record_store, "list_preview_records", None)):
        raise PostgresPreviewRefreshConfigError(
            "Preview namespace ownership requires preview inventory."
        )
    store = cast(_PreviewNamespaceStore, record_store)
    profiles = store.list_product_profile_records()
    if not any(other.product == profile.product for other in profiles):
        raise PostgresPreviewRefreshConfigError(
            "Preview namespace ownership requires the stored product profile."
        )
    try:
        require_unique_preview_namespaces(
            profile,
            profiles,
            retained_contexts={
                record.context
                for record in store.list_preview_records()
                if record.state != "destroyed"
            },
        )
    except ValueError as error:
        raise PostgresPreviewRefreshConfigError(str(error)) from error


def _preview_app_name(preview_slug: str, prefix: str) -> str:
    return f"{prefix}-{preview_slug}"


def _preview_application_name(preview_slug: str, prefix: str, suffix: str = "") -> str:
    return f"{_preview_app_name(preview_slug, prefix)}{suffix}"


def _preview_database_identifiers(preview_slug: str, prefix: str) -> tuple[str, str]:
    normalized_slug = preview_slug.strip().lower().replace("-", "_")
    identifier = f"{prefix}{normalized_slug}"
    if len(identifier.encode()) > 63:
        suffix = hashlib.sha256(identifier.encode()).hexdigest()[:12]
        identifier = f"{identifier.encode()[:50].decode(errors='ignore')}_{suffix}"
    return identifier, identifier


def _preview_url_host(preview_url: str) -> str:
    parsed = urlparse(preview_url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Preview URL must use http or https.")
    if not parsed.hostname:
        raise ValueError("Preview URL requires a hostname.")
    return parsed.hostname


def _preview_url_from_base_url(*, preview_slug: str, preview_base_url: str) -> str:
    parsed = urlparse(str(preview_base_url).strip())
    if parsed.scheme not in {"http", "https"}:
        raise PostgresPreviewRefreshConfigError(
            f"{PREVIEW_BASE_URL_ENV_KEY} must use http or https."
        )
    if not parsed.hostname:
        raise PostgresPreviewRefreshConfigError(f"{PREVIEW_BASE_URL_ENV_KEY} requires a hostname.")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise PostgresPreviewRefreshConfigError(
            f"{PREVIEW_BASE_URL_ENV_KEY} must be a root URL without path, query, or fragment."
        )
    return parsed._replace(
        netloc=f"{preview_slug}.{parsed.hostname}",
        path="",
        params="",
        query="",
        fragment="",
    ).geturl()


def _resolve_preview_base_url(*, control_plane_root: Path, context_name: str) -> str:
    resolved_values = control_plane_runtime_environments.resolve_runtime_context_values(
        control_plane_root=control_plane_root,
        context_name=context_name,
    )
    preview_base_url = str(resolved_values.get(PREVIEW_BASE_URL_ENV_KEY) or "").strip()
    if not preview_base_url:
        raise PostgresPreviewRefreshConfigError(
            f"Missing {PREVIEW_BASE_URL_ENV_KEY} in Launchplane runtime-environment records for {context_name}."
        )
    return preview_base_url


def _resolve_preview_url(
    *, control_plane_root: Path, request: PostgresPreviewRefreshRequest
) -> str:
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
    *, control_plane_root: Path, request: PostgresPreviewDestroyRequest
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
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise PostgresPreviewRefreshConfigError(
            "Preview template DATABASE_URL must use PostgreSQL."
        )
    if not parsed.hostname:
        raise PostgresPreviewRefreshConfigError(
            "Preview template DATABASE_URL must include a hostname."
        )
    database_name = parsed.path.lstrip("/").strip()
    if not database_name:
        raise PostgresPreviewRefreshConfigError(
            "Preview template DATABASE_URL must include a database name."
        )
    username = unquote(parsed.username or "").strip()
    password = unquote(parsed.password or "")
    if not username:
        raise PostgresPreviewRefreshConfigError(
            "Preview template DATABASE_URL must include a username."
        )
    return _DatabaseParts(
        host=parsed.hostname,
        port=parsed.port or 5432,
        database=database_name,
        username=username,
        password=password,
    )


def _template_runtime_secret_keys(
    template_env_map: dict[str, str],
    generated_keys: frozenset[str] = frozenset({"DATABASE_URL"}),
) -> tuple[str, ...]:
    required_keys: list[str] = []
    for key, value in template_env_map.items():
        normalized_key = key.strip()
        if not normalized_key or not str(value).strip():
            continue
        if normalized_key in generated_keys:
            continue
        if looks_like_credential(normalized_key, str(value)):
            required_keys.append(normalized_key)
    return tuple(dict.fromkeys(required_keys))


def _build_admin_database_url(database_url: str) -> str:
    parsed = urlparse(str(database_url).strip())
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "schema"
    ]
    return parsed._replace(
        path="/postgres",
        query="&".join(f"{quote(key)}={quote(value)}" for key, value in query_items),
    ).geturl()


def _build_preview_database_url(
    *, host: str, port: int, database_name: str, role_name: str, password: str
) -> str:
    encoded_role = quote(role_name, safe="")
    encoded_password = quote(password, safe="")
    encoded_database = quote(database_name, safe="")
    return f"postgresql://{encoded_role}:{encoded_password}@{host}:{port}/{encoded_database}?schema=public"


def _random_password(length: int = 32) -> str:
    return secrets.token_hex(length)[:length]


def _random_base64_secret(byte_count: int = 32) -> str:
    return base64.b64encode(secrets.token_bytes(byte_count)).decode("ascii")


def _random_urlsafe_secret(byte_count: int = 32) -> str:
    return secrets.token_urlsafe(byte_count)


def _resolve_preview_secret(
    *,
    existing_env_map: dict[str, str],
    key: str,
    generate: Callable[[], str],
    template_env_map: dict[str, str] | None = None,
) -> str:
    existing_value = existing_env_map.get(key, "").strip()
    template_value = (template_env_map or {}).get(key, "").strip()
    if existing_value and existing_value != template_value:
        return existing_value
    return generate()


def _render_postgres_preview_env(
    *,
    profile: LaunchplaneProductProfileRecord,
    template_env_map: dict[str, str],
    existing_env_map: dict[str, str],
    database_parts: _DatabaseParts,
    preview_url: str,
    runtime_identity: RuntimeIdentity,
) -> str:
    generated_keys = frozenset({"DATABASE_URL", *profile.preview.generated_secret_env})
    omitted = _template_runtime_secret_keys(template_env_map, generated_keys)
    updates = {key: value for key, value in template_env_map.items() if key not in omitted}
    for key, encoding in profile.preview.generated_secret_env.items():
        updates[key] = _resolve_preview_secret(
            existing_env_map=existing_env_map,
            template_env_map=template_env_map,
            key=key,
            generate=_random_base64_secret if encoding == "base64" else _random_urlsafe_secret,
        )
    updates["DATABASE_URL"] = _build_preview_database_url(
        host=database_parts.host,
        port=database_parts.port,
        database_name=database_parts.database,
        role_name=database_parts.username,
        password=database_parts.password,
    )
    for key in profile.preview.preview_url_env_keys:
        updates[key] = preview_url
    for key in profile.preview.preview_domain_env_keys:
        updates[key] = _preview_domain_from_url(preview_url)
    updates.update(runtime_identity_env(runtime_identity))
    runtime_platform_credentials.refuse_platform_credentials(
        updates, target=f"{profile.product} preview", source="preview environment settings"
    )
    return dokploy_api.render_dokploy_env_text_with_overrides("", updates=updates)


def _template_application_payload(
    *,
    control_plane_root: Path,
    host: str,
    token: str,
    template_context: str,
    template_instance: str,
) -> tuple[dokploy_source.DokployTargetDefinition, JsonObject]:
    try:
        source_of_truth = dokploy_source.read_control_plane_dokploy_source_of_truth(
            control_plane_root=control_plane_root,
        )
    except click.ClickException as exc:
        if _is_source_of_truth_backend_failure(exc):
            raise PostgresPreviewRefreshTransportError(str(exc)) from exc
        raise PostgresPreviewRefreshConfigError(str(exc)) from exc
    target_definition = dokploy_source.find_dokploy_target_definition(
        source_of_truth,
        context_name=template_context,
        instance_name=template_instance,
    )
    if target_definition is None:
        raise PostgresPreviewRefreshConfigError(
            f"No Dokploy target definition found for {template_context}/{template_instance}."
        )
    if target_definition.target_type != "application":
        raise PostgresPreviewRefreshConfigError(
            "PostgreSQL preview driver requires the testing target to be a Dokploy application."
        )
    if not target_definition.target_id.strip():
        raise PostgresPreviewRefreshConfigError(
            "PostgreSQL testing target requires a Dokploy target_id before preview execution."
        )
    try:
        payload = dokploy_api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="application",
            target_id=target_definition.target_id,
        )
    except click.ClickException as exc:
        if _is_dokploy_provider_failure(exc):
            raise PostgresPreviewRefreshTransportError(str(exc)) from exc
        raise PostgresPreviewRefreshConfigError(str(exc)) from exc
    return target_definition, payload


def _declared_template_options(
    profile: LaunchplaneProductProfileRecord,
) -> dict[str, str]:
    if (
        profile.driver_id != "generic-web"
        or profile.preview.transport_extension != "postgres"
        or not profile.preview.transport_database_prefix
        or not profile.preview.app_name_prefix
    ):
        raise PostgresPreviewRefreshConfigError(
            "Declared preview transport configuration is incomplete."
        )
    template = next(
        (lane for lane in profile.lanes if lane.instance == profile.preview.template_instance),
        None,
    )
    if template is None:
        raise PostgresPreviewRefreshConfigError("Declared preview transport has no template lane.")
    if template.instance != "testing":
        raise PostgresPreviewRefreshConfigError(
            "PostgreSQL previews require a testing template lane."
        )
    return {"template_context": template.context, "template_instance": template.instance}


def _is_source_of_truth_backend_failure(error: click.ClickException) -> bool:
    message = str(error).strip()
    return message.startswith(
        "Could not load tracked Dokploy targets from Launchplane Postgres storage:"
    )


def _is_dokploy_provider_failure(error: click.ClickException) -> bool:
    cause = error.__cause__
    if isinstance(cause, (HTTPError, URLError, TimeoutError)):
        return True
    message = str(error).strip()
    return message.startswith("Dokploy API ") or message.endswith(
        "returned an invalid response payload."
    )


def _find_application_by_name(*, host: str, token: str, application_name: str) -> JsonObject | None:
    raw_projects = dokploy_api.dokploy_request(
        host=host,
        token=token,
        path="/api/project.all",
    )
    for application in tuple(dokploy_api.iter_dokploy_applications(raw_projects)):
        if str(application.get("name") or "").strip() == application_name:
            application_id = str(
                application.get("applicationId") or application.get("id") or ""
            ).strip()
            if not application_id:
                raise click.ClickException(
                    "Dokploy project inventory has incomplete application identity."
                )
            return {**application, "applicationId": application_id}
    return None


def _fetch_application(*, host: str, token: str, application_id: str) -> JsonObject:
    payload = dokploy_api.dokploy_request(
        host=host,
        token=token,
        path="/api/application.one",
        query={"applicationId": application_id},
    )
    application = dokploy_api.as_json_object(payload)
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
            "PostgreSQL preview driver could not resolve the Dokploy testing environmentId."
        )
    application_payload: JsonObject = {
        "name": application_name,
        "appName": app_name,
        "description": description,
        "environmentId": environment_id,
    }
    if server_id:
        application_payload["serverId"] = server_id
    created = dokploy_api.dokploy_request(
        host=host,
        token=token,
        path="/api/application.create",
        method="POST",
        payload=application_payload,
    )
    created_application = dokploy_api.as_json_object(created)
    created_application_id = str((created_application or {}).get("applicationId") or "").strip()
    if not created_application_id:
        raise click.ClickException(
            f"Dokploy did not return an applicationId for preview app {application_name!r}."
        )
    return _fetch_application(host=host, token=token, application_id=created_application_id)


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
    latest_before = dokploy_api.latest_deployment_for_target(
        host=host,
        token=token,
        target_type="application",
        target_id=application_id,
    )
    dokploy_api.trigger_deployment(
        host=host,
        token=token,
        target_type="application",
        target_id=application_id,
        no_cache=False,
    )
    dokploy_api.wait_for_target_deployment(
        host=host,
        token=token,
        target_type="application",
        target_id=application_id,
        before_key=dokploy_api.deployment_key(latest_before),
        timeout_seconds=timeout_seconds,
    )


def _upsert_application_schedule(
    *,
    host: str,
    token: str,
    application_id: str,
    schedule_name: str,
    command: str,
) -> str:
    payload: JsonObject = {
        "name": schedule_name,
        "cronExpression": dokploy_post_deploy.DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
        "scheduleType": "application",
        "shellType": "sh",
        "command": command,
        "applicationId": application_id,
        "enabled": False,
        "timezone": "UTC",
    }
    resolved = dokploy_api.upsert_dokploy_application_schedule(
        host=host,
        token=token,
        application_id=application_id,
        schedule_payload=payload,
    )
    schedule_id = dokploy_api.schedule_key(resolved)
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
    dokploy_api.run_dokploy_schedule(
        host=host,
        token=token,
        schedule_id=schedule_id,
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
        except click.ClickException as error:
            if attempt >= attempts or not dokploy_api.is_transient_dokploy_error(error):
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


def _wait_for_preview_health(
    *,
    preview_url: str,
    timeout_seconds: int,
    expected_runtime_identity: RuntimeIdentity,
    health_path: str = "/api/health",
) -> RuntimeIdentity:
    health_url = f"{preview_url.rstrip('/')}{health_path}"
    started_at = time.monotonic()
    request = Request(
        health_url,
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-store",
        },
    )
    last_detail = "health endpoint did not return matching runtime identity"
    while time.monotonic() - started_at <= timeout_seconds:
        remaining_seconds = max(1, timeout_seconds - int(time.monotonic() - started_at))
        try:
            with urlopen(request, timeout=min(15, remaining_seconds)) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                last_detail = "health endpoint returned non-object JSON"
            elif payload.get("ok") is not True:
                last_detail = "health endpoint did not report ok=true"
            else:
                status, detail, observed = health_payload_runtime_identity_status(
                    expected=expected_runtime_identity,
                    payload=payload,
                    json_parse_failed=False,
                )
                if status == "match" and observed is not None:
                    return observed
                if status == "mismatch" and _runtime_identity_mismatch_is_hard(detail):
                    raise click.ClickException(
                        "Preview runtime identity did not match the expected deployment identity: "
                        f"{detail}."
                    )
                last_detail = detail
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ValueError):
            last_detail = "health endpoint request failed or returned invalid JSON"
        elapsed_seconds = time.monotonic() - started_at
        if elapsed_seconds >= timeout_seconds:
            break
        sleep_seconds = min(5, timeout_seconds - elapsed_seconds)
        time.sleep(sleep_seconds)
    raise click.ClickException(
        f"Timed out waiting for {health_url} to report the expected runtime identity: {last_detail}."
    )


def _runtime_identity_mismatch_is_hard(detail: str) -> bool:
    prefix = "Runtime identity mismatched fields: "
    if not detail.startswith(prefix):
        return False
    fields = {field.strip() for field in detail.removeprefix(prefix).split(",") if field.strip()}
    return bool(fields & _HARD_RUNTIME_IDENTITY_FIELDS)


def _resolve_existing_preview_database(
    existing_application: JsonObject | None,
    *,
    database_name: str,
    role_name: str,
    template_database: _DatabaseParts,
) -> _DatabaseParts | None:
    if database_name == template_database.database or role_name == template_database.username:
        raise PostgresPreviewRefreshConfigError(
            "Preview database ownership is unverified; no database or application changes allowed."
        )
    if existing_application is None:
        return None
    database_url = dokploy_api.parse_dokploy_env_text(
        str(existing_application.get("env") or "")
    ).get(
        "DATABASE_URL",
        "",
    )
    if not database_url:
        return None
    database = _parse_database_url(database_url)
    if (
        database.database != database_name
        or database.username != role_name
        or (database.host, database.port) != (template_database.host, template_database.port)
    ):
        raise PostgresPreviewRefreshConfigError(
            "Preview database ownership is unverified; no database or application changes allowed."
        )
    return database


def execute_postgres_preview_refresh(
    *,
    control_plane_root: Path,
    request: PostgresPreviewRefreshRequest,
    prepare_preview: Callable[[], None],
    preview_id: str = "",
    preview_generation_id: str = "",
    record_store: object | None = None,
    profile: LaunchplaneProductProfileRecord,
) -> PostgresPreviewRefreshResult:
    legacy_https = profile.preview.transport_extension == "verireel-postgres"
    profile = effective_postgres_preview_profile(profile)
    _require_unique_preview_namespaces(record_store, profile)
    try:
        host, token = dokploy_source.read_dokploy_config(control_plane_root=control_plane_root)
    except click.ClickException as exc:
        raise PostgresPreviewRefreshConfigError(str(exc)) from exc
    template_options = _declared_template_options(profile)
    template_target, template_application = _template_application_payload(
        control_plane_root=control_plane_root,
        host=host,
        token=token,
        **template_options,
    )
    template_env_map = dokploy_api.parse_dokploy_env_text(
        str(template_application.get("env") or "")
    )
    template_database_url = str(template_env_map.get("DATABASE_URL") or "").strip()
    if not template_database_url:
        raise PostgresPreviewRefreshConfigError(
            "PostgreSQL testing template application is missing DATABASE_URL."
        )
    try:
        runtime_platform_credentials.refuse_platform_credentials(
            template_env_map,
            target=f"PostgreSQL preview {request.preview_slug}",
            source=f"the {template_target.context}/{template_target.instance} template application env",
        )
    except runtime_platform_credentials.PlatformCredentialRefusedError as exc:
        raise PostgresPreviewRefreshConfigError(str(exc.message)) from exc
    template_database = _parse_database_url(template_database_url)

    started_at = utc_now_timestamp()
    preview_url = _resolve_preview_url(control_plane_root=control_plane_root, request=request)
    app_prefix = profile.preview.app_name_prefix
    database_prefix = profile.preview.transport_database_prefix
    application_name = _preview_application_name(
        request.preview_slug,
        app_prefix,
        profile.preview.app_name_suffix,
    )
    app_name = _preview_app_name(request.preview_slug, app_prefix)
    preview_host = _preview_url_host(preview_url)
    try:
        existing_application = _find_application_by_name(
            host=host, token=token, application_name=application_name
        )
    except click.ClickException as exc:
        raise PostgresPreviewRefreshTransportError(str(exc)) from exc
    existing_snapshot = None
    if existing_application is not None:
        application_id = str(existing_application.get("applicationId") or "").strip()
        try:
            existing_snapshot = _fetch_application(
                host=host, token=token, application_id=application_id
            )
        except click.ClickException as exc:
            if _is_dokploy_provider_failure(exc):
                raise PostgresPreviewRefreshTransportError(str(exc)) from exc
            raise PostgresPreviewRefreshConfigError(str(exc)) from exc
    if existing_snapshot is None:
        memory_capacity.require_preview_host_memory_capacity(
            host=host,
            token=token,
            server_id=str(template_application.get("serverId") or "").strip(),
            record_store=cast(RuntimeEnvironmentRecordStore | None, record_store),
        )
    database_name, role_name = _preview_database_identifiers(request.preview_slug, database_prefix)
    existing_database = _resolve_existing_preview_database(
        existing_snapshot,
        database_name=database_name,
        role_name=role_name,
        template_database=template_database,
    )
    existing_env_map = dokploy_api.parse_dokploy_env_text(
        str((existing_snapshot or {}).get("env") or "")
    )
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
    migration_started = False
    lifecycle: DokployApplicationLifecycle | None = None
    image_effect_attempted = False
    previous_application_restored = False

    expected_runtime_identity = _build_preview_runtime_identity(
        request=request,
        preview_id=preview_id,
        preview_generation_id=preview_generation_id,
        product=profile.product,
    )
    try:
        env_text = _render_postgres_preview_env(
            profile=profile,
            template_env_map=template_env_map,
            existing_env_map=existing_env_map,
            database_parts=database_parts,
            preview_url=preview_url,
            runtime_identity=expected_runtime_identity,
        )
    except runtime_platform_credentials.PlatformCredentialRefusedError as error:
        raise PostgresPreviewRefreshConfigError(error.message) from error

    prepare_preview()

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
        lifecycle = preview_application_lifecycle(
            record_store=record_store,
            host=host,
            token=token,
            application_id=application_id,
            product=profile.product,
            context=request.context,
            instance=request.preview_slug,
            artifact_id=request.image_reference,
            source_git_ref=request.anchor_head_sha,
            extensions=profile.preview.generic_web_extensions,
        )
        lifecycle.capture()
        lifecycle.prepare()
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
            runtime_port=profile.runtime_port,
            certificate_type=profile.preview.domain_certificate_type,
            https=legacy_https or urlparse(preview_url).scheme == "https",
        )
        latest_before = dokploy_api.latest_deployment_for_target(
            host=host,
            token=token,
            target_type="application",
            target_id=application_id,
        )
        image_effect_attempted = True
        dokploy_api.trigger_deployment(
            host=host,
            token=token,
            target_type="application",
            target_id=application_id,
            no_cache=False,
        )
        dokploy_api.wait_for_target_deployment(
            host=host,
            token=token,
            target_type="application",
            target_id=application_id,
            before_key=dokploy_api.deployment_key(latest_before),
            timeout_seconds=request.timeout_seconds,
        )
        migration_started = True
        lifecycle.maintain()
        observed_runtime_identity = _wait_for_preview_health(
            preview_url=preview_url,
            timeout_seconds=request.timeout_seconds,
            expected_runtime_identity=expected_runtime_identity,
            health_path=profile.health_path,
        )
        lifecycle.complete()
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
                previous_application_restored = True
            except click.ClickException as rollback_exc:
                rollback_errors.append(f"existing preview rollback failed: {rollback_exc}")
        if lifecycle is not None and not created_application_id:
            try:
                recover_preview_application_lifecycle(
                    lifecycle,
                    image_effect_attempted=image_effect_attempted,
                    target_restored=previous_application_restored,
                )
            except click.ClickException as rollback_exc:
                rollback_errors.append(f"application schedule recovery failed: {rollback_exc}")
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
        return PostgresPreviewRefreshResult(
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
    return PostgresPreviewRefreshResult(
        refresh_status="pass",
        refresh_started_at=started_at,
        refresh_finished_at=finished_at,
        application_name=application_name,
        application_id=application_id,
        preview_url=preview_url,
        runtime_identity=observed_runtime_identity,
    )


def execute_postgres_preview_destroy(
    *,
    control_plane_root: Path,
    request: PostgresPreviewDestroyRequest,
    prepare_destroy: Callable[[], None],
    record_store: object | None = None,
    profile: LaunchplaneProductProfileRecord,
) -> PostgresPreviewDestroyResult:
    profile = effective_postgres_preview_profile(profile)
    _require_unique_preview_namespaces(record_store, profile)
    host, token = dokploy_source.read_dokploy_config(control_plane_root=control_plane_root)
    template_target, template_application = _template_application_payload(
        control_plane_root=control_plane_root,
        host=host,
        token=token,
        **_declared_template_options(profile),
    )
    template_env_map = dokploy_api.parse_dokploy_env_text(
        str(template_application.get("env") or "")
    )
    template_database_url = str(template_env_map.get("DATABASE_URL") or "").strip()
    if not template_database_url:
        raise click.ClickException(
            "PostgreSQL testing template application is missing DATABASE_URL."
        )

    started_at = utc_now_timestamp()
    preview_url = _resolve_preview_url_for_destroy(
        control_plane_root=control_plane_root, request=request
    )
    app_prefix = profile.preview.app_name_prefix
    database_prefix = profile.preview.transport_database_prefix
    application_name = _preview_application_name(
        request.preview_slug,
        app_prefix,
        profile.preview.app_name_suffix,
    )
    try:
        application = _find_application_by_name(
            host=host, token=token, application_name=application_name
        )
    except click.ClickException as exc:
        return PostgresPreviewDestroyResult(
            destroy_status="fail",
            destroy_started_at=started_at,
            destroy_finished_at=utc_now_timestamp(),
            application_name=application_name,
            application_id="",
            preview_url=preview_url,
            error_message=str(exc),
        )
    application_id = str((application or {}).get("applicationId") or "").strip()
    database_name, role_name = _preview_database_identifiers(request.preview_slug, database_prefix)
    template_database = _parse_database_url(template_database_url)
    application_payload = None
    if application_id:
        application_payload = _fetch_application(
            host=host, token=token, application_id=application_id
        )
    existing_database = _resolve_existing_preview_database(
        application_payload,
        database_name=database_name,
        role_name=role_name,
        template_database=template_database,
    )
    database_parts = existing_database or _DatabaseParts(
        host=template_database.host,
        port=template_database.port,
        database=database_name,
        username=role_name,
        password="",
    )

    prepare_destroy()

    cleanup_errors: list[str] = []
    if application_id:
        destroy_result = destroy_dokploy_preview_resource(
            record_store=record_store,
            context=profile.preview.context,
            instance=request.preview_slug,
            host=host,
            token=token,
            resource_type="application",
            resource_id=application_id,
        )
        cleanup_errors.extend(destroy_result.cleanup_errors)
        if cleanup_errors and cleanup_errors[0].startswith(f"{PREVIEW_CLEANUP_SAFETY_UNKNOWN}:"):
            return PostgresPreviewDestroyResult(
                destroy_status="fail",
                destroy_started_at=started_at,
                destroy_finished_at=utc_now_timestamp(),
                application_name=application_name,
                application_id=application_id,
                preview_url=preview_url,
                error_message=cleanup_errors[0],
            )
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
        return PostgresPreviewDestroyResult(
            destroy_status="fail",
            destroy_started_at=started_at,
            destroy_finished_at=finished_at,
            application_name=application_name,
            application_id=application_id,
            preview_url=preview_url,
            error_message="; ".join(cleanup_errors),
        )
    return PostgresPreviewDestroyResult(
        destroy_status="pass",
        destroy_started_at=started_at,
        destroy_finished_at=finished_at,
        application_name=application_name,
        application_id=application_id,
        preview_url=preview_url,
    )
