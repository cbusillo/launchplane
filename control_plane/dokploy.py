import json
import os
import shlex
import time
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS = 600
DEFAULT_DOKPLOY_HEALTH_TIMEOUT_SECONDS = 180
DEFAULT_DOKPLOY_HEALTHCHECK_PATH = "/web/health"
CONTROL_PLANE_ENV_FILE_ENV_VAR = "ODOO_CONTROL_PLANE_ENV_FILE"
CONTROL_PLANE_DOKPLOY_SOURCE_FILE_ENV_VAR = "ODOO_CONTROL_PLANE_DOKPLOY_SOURCE_FILE"
DEFAULT_CONTROL_PLANE_DOKPLOY_SOURCE_FILE = Path("config/dokploy.toml")
DOKPLOY_DATA_WORKFLOW_SCHEDULE_NAME = "platform-data-workflow"
DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION = "0 0 31 2 *"
DOKPLOY_RUNNING_DEPLOYMENT_STATUSES = {"pending", "queued", "running", "in_progress", "starting"}
DOKPLOY_CANCELLED_DEPLOYMENT_STATUSES = {"cancelled", "canceled"}
DOKPLOY_SUCCESS_DEPLOYMENT_STATUSES = {"done", "success", "succeeded", "completed", "finished", "healthy"}
POST_DEPLOY_UPDATE_IGNORED_ENV_KEYS = {
    "DOKPLOY_HOST",
    "DOKPLOY_TOKEN",
    CONTROL_PLANE_ENV_FILE_ENV_VAR,
    CONTROL_PLANE_DOKPLOY_SOURCE_FILE_ENV_VAR,
}
POST_DEPLOY_UPDATE_ALLOWED_ENV_KEYS = {
    "ODOO_DB_NAME",
    "ODOO_FILESTORE_PATH",
    "ODOO_DATA_WORKFLOW_LOCK_FILE",
}
DEFAULT_DATA_WORKFLOW_LOCK_PATH = "/volumes/data/.data_workflow_in_progress"


type JsonPrimitive = str | int | float | bool | None
type JsonValue = JsonPrimitive | dict[str, "JsonValue"] | list["JsonValue"]
type JsonObject = dict[str, JsonValue]


class DokployTargetDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    instance: str
    project_name: str = ""
    target_type: Literal["compose", "application"] = "compose"
    target_id: str = ""
    target_name: str = ""
    git_branch: str = ""
    source_git_ref: str = "origin/main"
    require_test_gate: bool = False
    require_prod_gate: bool = False
    deploy_timeout_seconds: int | None = Field(default=None, ge=1)
    healthcheck_enabled: bool = True
    healthcheck_path: str = DEFAULT_DOKPLOY_HEALTHCHECK_PATH
    healthcheck_timeout_seconds: int | None = Field(default=None, ge=1)
    env: dict[str, str] = Field(default_factory=dict)
    domains: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_identity_fields(self) -> "DokployTargetDefinition":
        if not self.context.strip():
            raise ValueError("Dokploy target requires non-empty context")
        if not self.instance.strip():
            raise ValueError("Dokploy target requires non-empty instance")
        if not self.target_id.strip():
            raise ValueError(f"Dokploy target {self.context}/{self.instance} requires non-empty target_id")
        return self


class DokploySourceOfTruth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1)
    targets: tuple[DokployTargetDefinition, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _normalize_inherited_targets(cls, raw_value: object) -> object:
        return _normalize_dokploy_source_payload(raw_value)

    @model_validator(mode="after")
    def _validate_unique_target_routes(self) -> "DokploySourceOfTruth":
        seen_targets: set[tuple[str, str]] = set()
        for target_definition in self.targets:
            target_route = (target_definition.context.strip(), target_definition.instance.strip())
            if target_route in seen_targets:
                context_name, instance_name = target_route
                raise ValueError(
                    f"Duplicate Dokploy target definition for {context_name}/{instance_name} in source-of-truth"
                )
            seen_targets.add(target_route)
        return self


def load_dokploy_source_of_truth(source_file_path: Path) -> DokploySourceOfTruth:
    try:
        payload = tomllib.loads(source_file_path.read_text(encoding="utf-8"))
        return DokploySourceOfTruth.model_validate(payload)
    except FileNotFoundError as error:
        raise click.ClickException(f"Dokploy source-of-truth file not found: {source_file_path}") from error
    except ValueError as error:
        raise click.ClickException(f"Invalid dokploy source-of-truth file {source_file_path}: {error}") from error


def find_dokploy_target_definition(
    source_of_truth: DokploySourceOfTruth,
    *,
    context_name: str,
    instance_name: str,
) -> DokployTargetDefinition | None:
    for target in source_of_truth.targets:
        if target.context == context_name and target.instance == instance_name:
            return target
    return None


def resolve_ship_timeout_seconds(
    *,
    timeout_override_seconds: int | None,
    target_definition: DokployTargetDefinition,
) -> int:
    if timeout_override_seconds is not None:
        if timeout_override_seconds <= 0:
            raise click.ClickException("Ship timeout must be greater than zero seconds.")
        return timeout_override_seconds
    if target_definition.deploy_timeout_seconds is not None:
        return target_definition.deploy_timeout_seconds
    return DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS


def resolve_ship_health_timeout_seconds(
    *,
    health_timeout_override_seconds: int | None,
    target_definition: DokployTargetDefinition | None,
) -> int:
    if health_timeout_override_seconds is not None:
        if health_timeout_override_seconds <= 0:
            raise click.ClickException("Ship health timeout must be greater than zero seconds.")
        return health_timeout_override_seconds
    if target_definition is not None and target_definition.healthcheck_timeout_seconds is not None:
        return target_definition.healthcheck_timeout_seconds
    return DEFAULT_DOKPLOY_HEALTH_TIMEOUT_SECONDS


def resolve_dokploy_ship_mode(context_name: str, instance_name: str, environment_values: dict[str, str]) -> str:
    specific_key = f"DOKPLOY_SHIP_MODE_{context_name}_{instance_name}".upper()
    configured_mode = environment_values.get(specific_key, "").strip().lower()
    if not configured_mode:
        configured_mode = environment_values.get("DOKPLOY_SHIP_MODE", "auto").strip().lower() or "auto"
    if configured_mode not in {"auto", "compose", "application"}:
        raise click.ClickException(f"Invalid Dokploy ship mode '{configured_mode}'. Expected auto, compose, or application.")
    return configured_mode


def normalize_healthcheck_path(raw_healthcheck_path: str) -> str:
    normalized_path = raw_healthcheck_path.strip() or DEFAULT_DOKPLOY_HEALTHCHECK_PATH
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"
    return normalized_path


def resolve_healthcheck_base_urls(
    *,
    target_definition: DokployTargetDefinition | None,
    environment_values: dict[str, str],
) -> tuple[str, ...]:
    raw_base_urls: list[str] = []
    if target_definition is not None:
        raw_base_urls.extend(domain for domain in target_definition.domains if domain)
        configured_base_url = target_definition.env.get("ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL", "").strip()
        if configured_base_url:
            raw_base_urls.append(configured_base_url)

    if not raw_base_urls:
        fallback_base_url = environment_values.get("ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL", "").strip()
        if fallback_base_url:
            raw_base_urls.append(fallback_base_url)

    normalized_base_urls: list[str] = []
    for raw_base_url in raw_base_urls:
        stripped_base_url = raw_base_url.strip()
        if not stripped_base_url:
            continue
        parsed_base_url = urlparse(stripped_base_url)
        if not parsed_base_url.scheme:
            stripped_base_url = f"https://{stripped_base_url}"
        stripped_base_url = stripped_base_url.rstrip("/")
        if stripped_base_url and stripped_base_url not in normalized_base_urls:
            normalized_base_urls.append(stripped_base_url)

    return tuple(normalized_base_urls)


def resolve_ship_healthcheck_urls(
    *,
    target_definition: DokployTargetDefinition | None,
    environment_values: dict[str, str],
) -> tuple[str, ...]:
    if target_definition is not None and not target_definition.healthcheck_enabled:
        return ()

    healthcheck_path = normalize_healthcheck_path(
        target_definition.healthcheck_path if target_definition is not None else DEFAULT_DOKPLOY_HEALTHCHECK_PATH
    )
    base_urls = resolve_healthcheck_base_urls(target_definition=target_definition, environment_values=environment_values)
    return tuple(f"{base_url}{healthcheck_path}" for base_url in base_urls)


def load_runtime_environment_values(
    *,
    default_env_file: Path | None = None,
    env_file: Path | None = None,
) -> dict[str, str]:
    environment_values: dict[str, str] = {}
    selected_env_file = env_file if env_file is not None else default_env_file
    if selected_env_file is not None and selected_env_file.exists():
        environment_values.update(_parse_env_file(selected_env_file))
    for environment_key, environment_value in os.environ.items():
        environment_values[environment_key] = environment_value
    return environment_values


def read_control_plane_environment_values(*, control_plane_root: Path) -> dict[str, str]:
    control_plane_env_file = resolve_control_plane_env_file(control_plane_root)
    return load_runtime_environment_values(default_env_file=control_plane_env_file)


def resolve_control_plane_dokploy_source_file(control_plane_root: Path) -> Path:
    configured_source_file = os.environ.get(CONTROL_PLANE_DOKPLOY_SOURCE_FILE_ENV_VAR, "").strip()
    if configured_source_file:
        candidate_path = Path(configured_source_file)
        if not candidate_path.is_absolute():
            candidate_path = control_plane_root / candidate_path
        return candidate_path
    return control_plane_root / DEFAULT_CONTROL_PLANE_DOKPLOY_SOURCE_FILE


def read_control_plane_dokploy_source_of_truth(*, control_plane_root: Path) -> DokploySourceOfTruth:
    return load_dokploy_source_of_truth(resolve_control_plane_dokploy_source_file(control_plane_root))


def read_dokploy_config(*, control_plane_root: Path) -> tuple[str, str]:
    environment_values = read_control_plane_environment_values(control_plane_root=control_plane_root)

    host = environment_values.get("DOKPLOY_HOST", "").strip()
    token = environment_values.get("DOKPLOY_TOKEN", "").strip()
    if not host or not token:
        raise click.ClickException(
            "Missing DOKPLOY_HOST or DOKPLOY_TOKEN for control-plane Dokploy execution. "
            "Define them in the control-plane .env, the file pointed to by ODOO_CONTROL_PLANE_ENV_FILE, "
            "or the current process environment."
        )
    return host, token


def resolve_control_plane_env_file(control_plane_root: Path) -> Path:
    configured_env_file = os.environ.get(CONTROL_PLANE_ENV_FILE_ENV_VAR, "").strip()
    if configured_env_file:
        candidate_path = Path(configured_env_file)
        if not candidate_path.is_absolute():
            candidate_path = control_plane_root / candidate_path
        return candidate_path
    return control_plane_root / ".env"


def trigger_deployment(*, host: str, token: str, target_type: str, target_id: str, no_cache: bool) -> None:
    if target_type == "compose":
        endpoint_path = "/api/compose.redeploy" if no_cache else "/api/compose.deploy"
        payload: JsonObject = {"composeId": target_id}
    elif target_type == "application":
        endpoint_path = "/api/application.redeploy" if no_cache else "/api/application.deploy"
        payload = {"applicationId": target_id}
    else:
        raise click.ClickException(f"Unsupported Dokploy target type: {target_type}")
    if no_cache:
        payload["title"] = "Manual redeploy (no-cache requested)"
    dokploy_request(host=host, token=token, path=endpoint_path, method="POST", payload=payload)


def latest_deployment_for_target(*, host: str, token: str, target_type: str, target_id: str) -> JsonObject | None:
    if target_type == "compose":
        compose_payload = dokploy_request(
            host=host,
            token=token,
            path="/api/compose.one",
            query={"composeId": target_id},
        )
        compose_payload_as_object = as_json_object(compose_payload)
        if compose_payload_as_object is None:
            return None
        deployments_payload = compose_payload_as_object.get("deployments")
        if not isinstance(deployments_payload, list):
            return None
        return _latest_deployment_from_list(_collect_object_items(deployments_payload))

    if target_type == "application":
        payload = dokploy_request(
            host=host,
            token=token,
            path="/api/deployment.all",
            query={"applicationId": target_id},
        )
        return _latest_deployment_from_list(extract_deployments(payload))

    raise click.ClickException(f"Unsupported Dokploy target type: {target_type}")


def fetch_dokploy_target_payload(
    *,
    host: str,
    token: str,
    target_type: str,
    target_id: str,
) -> JsonObject:
    if target_type == "compose":
        payload = dokploy_request(
            host=host,
            token=token,
            path="/api/compose.one",
            query={"composeId": target_id},
        )
    elif target_type == "application":
        payload = dokploy_request(
            host=host,
            token=token,
            path="/api/application.one",
            query={"applicationId": target_id},
        )
    else:
        raise click.ClickException(f"Unsupported target type: {target_type}")

    payload_as_object = as_json_object(payload)
    if payload_as_object is None:
        raise click.ClickException(f"Dokploy {target_type}.one returned an invalid response payload.")
    return payload_as_object


def parse_dokploy_env_text(raw_env_text: str) -> dict[str, str]:
    env_map: dict[str, str] = {}
    for raw_line in raw_env_text.splitlines():
        stripped_line = raw_line.strip()
        if not stripped_line or stripped_line.startswith("#"):
            continue
        if stripped_line.startswith("export "):
            stripped_line = stripped_line[7:].strip()
        if "=" not in stripped_line:
            continue
        key_part, value_part = stripped_line.split("=", 1)
        env_map[key_part.strip()] = value_part
    return env_map


def serialize_dokploy_env_text(env_map: dict[str, str]) -> str:
    if not env_map:
        return ""
    rendered_lines = [f"{env_key}={env_value}" for env_key, env_value in env_map.items()]
    return "\n".join(rendered_lines)


def update_dokploy_target_env(
    *,
    host: str,
    token: str,
    target_type: str,
    target_id: str,
    target_payload: JsonObject,
    env_text: str,
) -> None:
    if target_type == "compose":
        dokploy_request(
            host=host,
            token=token,
            path="/api/compose.update",
            method="POST",
            payload={"composeId": target_id, "env": env_text},
        )
        return

    if target_type == "application":
        build_args = target_payload.get("buildArgs")
        build_secrets = target_payload.get("buildSecrets")
        create_env_file = target_payload.get("createEnvFile")
        payload: JsonObject = {
            "applicationId": target_id,
            "env": env_text,
            "createEnvFile": bool(create_env_file) if isinstance(create_env_file, bool) else True,
        }
        if isinstance(build_args, str):
            payload["buildArgs"] = build_args
        if isinstance(build_secrets, str):
            payload["buildSecrets"] = build_secrets
        dokploy_request(
            host=host,
            token=token,
            path="/api/application.saveEnvironment",
            method="POST",
            payload=payload,
        )
        return

    raise click.ClickException(f"Unsupported target type: {target_type}")


def wait_for_target_deployment(
    *,
    host: str,
    token: str,
    target_type: str,
    target_id: str,
    before_key: str,
    timeout_seconds: int,
) -> str:
    failure_message_prefix = "Dokploy compose deployment failed" if target_type == "compose" else "Dokploy deployment failed"
    return _wait_for_deployment_status(
        fetch_latest_deployment=lambda: latest_deployment_for_target(
            host=host,
            token=token,
            target_type=target_type,
            target_id=target_id,
        ),
        before_key=before_key,
        timeout_seconds=timeout_seconds,
        failure_message_prefix=failure_message_prefix,
    )


def resolve_dokploy_user_id(*, host: str, token: str) -> str:
    payload = dokploy_request(host=host, token=token, path="/api/user.session")
    payload_as_object = as_json_object(payload)
    if payload_as_object is None:
        raise click.ClickException("Dokploy user.session returned an invalid response payload.")
    user_payload = as_json_object(payload_as_object.get("user"))
    if user_payload is None:
        raise click.ClickException("Dokploy user.session returned no user payload.")
    user_id = str(user_payload.get("id") or "").strip()
    if not user_id:
        raise click.ClickException("Dokploy user.session returned no user id.")
    return user_id


def latest_deployment_for_schedule(*, host: str, token: str, schedule_id: str) -> JsonObject | None:
    payload = dokploy_request(
        host=host,
        token=token,
        path="/api/deployment.allByType",
        query={"id": schedule_id, "type": "schedule"},
    )
    return _latest_deployment_from_list(extract_deployments(payload))


def wait_for_dokploy_schedule_deployment(
    *,
    host: str,
    token: str,
    schedule_id: str,
    before_key: str,
    timeout_seconds: int,
) -> str:
    return _wait_for_deployment_status(
        fetch_latest_deployment=lambda: latest_deployment_for_schedule(
            host=host,
            token=token,
            schedule_id=schedule_id,
        ),
        before_key=before_key,
        timeout_seconds=timeout_seconds,
        failure_message_prefix="Dokploy schedule deployment failed",
    )


def list_dokploy_schedules(
    *,
    host: str,
    token: str,
    target_id: str,
    schedule_type: str,
) -> tuple[JsonObject, ...]:
    payload = dokploy_request(
        host=host,
        token=token,
        path="/api/schedule.list",
        query={"id": target_id, "scheduleType": schedule_type},
    )
    return tuple(extract_schedules(payload))


def find_matching_dokploy_schedule(
    *,
    host: str,
    token: str,
    target_id: str,
    schedule_type: str,
    schedule_name: str,
    app_name: str,
) -> JsonObject | None:
    for schedule in list_dokploy_schedules(
        host=host,
        token=token,
        target_id=target_id,
        schedule_type=schedule_type,
    ):
        if str(schedule.get("name") or "").strip() != schedule_name:
            continue
        if str(schedule.get("appName") or "").strip() != app_name:
            continue
        return schedule
    return None


def schedule_key(schedule: JsonObject) -> str:
    for key_name in ("scheduleId", "schedule_id", "id", "uuid"):
        value = schedule.get(key_name)
        if value:
            return str(value)
    return ""


def upsert_dokploy_schedule(
    *,
    host: str,
    token: str,
    target_id: str,
    schedule_type: str,
    schedule_name: str,
    app_name: str,
    schedule_payload: JsonObject,
) -> JsonObject:
    existing_schedule = find_matching_dokploy_schedule(
        host=host,
        token=token,
        target_id=target_id,
        schedule_type=schedule_type,
        schedule_name=schedule_name,
        app_name=app_name,
    )
    if existing_schedule is not None:
        updated_payload = dict(schedule_payload)
        updated_payload["scheduleId"] = schedule_key(existing_schedule)
        dokploy_request(
            host=host,
            token=token,
            path="/api/schedule.update",
            method="POST",
            payload=updated_payload,
        )
    else:
        dokploy_request(
            host=host,
            token=token,
            path="/api/schedule.create",
            method="POST",
            payload=schedule_payload,
        )

    resolved_schedule = find_matching_dokploy_schedule(
        host=host,
        token=token,
        target_id=target_id,
        schedule_type=schedule_type,
        schedule_name=schedule_name,
        app_name=app_name,
    )
    if resolved_schedule is None:
        raise click.ClickException(
            f"Dokploy schedule {schedule_name!r} for {schedule_type} target {target_id!r} could not be resolved after upsert."
        )
    return resolved_schedule


def run_compose_post_deploy_update(
    *,
    host: str,
    token: str,
    target_definition: DokployTargetDefinition,
    env_file: Path | None,
) -> None:
    compose_id = target_definition.target_id.strip()
    compose_name = target_definition.target_name.strip() or f"{target_definition.context}-{target_definition.instance}"
    if not compose_id:
        raise click.ClickException(
            f"Dokploy compose target {target_definition.context}/{target_definition.instance} requires target_id for post-deploy update."
        )
    target_payload = fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type="compose",
        target_id=compose_id,
    )
    current_env_map = parse_dokploy_env_text(str(target_payload.get("env") or ""))
    desired_env_map = _apply_post_deploy_env_file_overrides(
        current_env_map=current_env_map,
        env_file=env_file,
    )
    schedule_timeout_seconds = target_definition.deploy_timeout_seconds or DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS
    if desired_env_map != current_env_map:
        update_dokploy_target_env(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
            target_payload=target_payload,
            env_text=serialize_dokploy_env_text(desired_env_map),
        )
        latest_compose_deployment = latest_deployment_for_target(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
        )
        trigger_deployment(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
            no_cache=False,
        )
        wait_for_target_deployment(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
            before_key=deployment_key(latest_compose_deployment),
            timeout_seconds=schedule_timeout_seconds,
        )

    database_name = desired_env_map.get("ODOO_DB_NAME", "").strip()
    if not database_name:
        raise click.ClickException(
            "Compose post-deploy update requires ODOO_DB_NAME in the live target environment or explicit env file."
        )
    filestore_path = (desired_env_map.get("ODOO_FILESTORE_PATH") or "/volumes/data/filestore").strip() or "/volumes/data/filestore"
    data_workflow_lock_path = (
        desired_env_map.get("ODOO_DATA_WORKFLOW_LOCK_FILE") or DEFAULT_DATA_WORKFLOW_LOCK_PATH
    ).strip() or DEFAULT_DATA_WORKFLOW_LOCK_PATH
    schedule_type, schedule_lookup_id, compose_app_name, schedule_server_id = _resolve_dokploy_schedule_runtime(
        host=host,
        token=token,
        compose_id=compose_id,
        compose_name=compose_name,
        target_payload=target_payload,
    )
    schedule_name = DOKPLOY_DATA_WORKFLOW_SCHEDULE_NAME
    schedule_app_name = _build_dokploy_data_workflow_schedule_app_name(
        context_name=target_definition.context,
        instance_name=target_definition.instance,
    )
    existing_schedule = find_matching_dokploy_schedule(
        host=host,
        token=token,
        target_id=schedule_lookup_id,
        schedule_type=schedule_type,
        schedule_name=schedule_name,
        app_name=schedule_app_name,
    )
    if _has_running_schedule_deployment(existing_schedule):
        raise click.ClickException(
            "Dokploy-managed post-deploy update already has a running schedule deployment for "
            f"{target_definition.context}/{target_definition.instance}."
        )
    schedule_script = _build_dokploy_data_workflow_script(
        compose_app_name=compose_app_name,
        database_name=database_name,
        filestore_path=filestore_path,
        clear_stale_lock=_should_clear_stale_data_workflow_lock(existing_schedule),
        data_workflow_lock_path=data_workflow_lock_path,
    )
    schedule_payload: JsonObject = {
        "name": schedule_name,
        "cronExpression": DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
        "appName": schedule_app_name,
        "shellType": "bash",
        "scheduleType": schedule_type,
        "command": "control-plane post-deploy update",
        "script": schedule_script,
        "serverId": schedule_server_id,
        "userId": schedule_lookup_id if schedule_type == "dokploy-server" else None,
        "enabled": False,
        "timezone": "UTC",
    }
    schedule = upsert_dokploy_schedule(
        host=host,
        token=token,
        target_id=schedule_lookup_id,
        schedule_type=schedule_type,
        schedule_name=schedule_name,
        app_name=schedule_app_name,
        schedule_payload=schedule_payload,
    )
    schedule_id = schedule_key(schedule)
    if not schedule_id:
        raise click.ClickException(
            f"Dokploy schedule {schedule_name!r} for {target_definition.context}/{target_definition.instance} did not expose a schedule id."
        )
    latest_schedule_deployment = latest_deployment_for_schedule(
        host=host,
        token=token,
        schedule_id=schedule_id,
    )
    dokploy_request(
        host=host,
        token=token,
        path="/api/schedule.runManually",
        method="POST",
        payload={"scheduleId": schedule_id},
        timeout_seconds=schedule_timeout_seconds,
    )
    wait_for_dokploy_schedule_deployment(
        host=host,
        token=token,
        schedule_id=schedule_id,
        before_key=deployment_key(latest_schedule_deployment),
        timeout_seconds=schedule_timeout_seconds,
    )


def deployment_key(deployment: JsonObject | None) -> str:
    if deployment is None:
        return ""
    for key_name in ("deploymentId", "deployment_id", "id", "uuid"):
        value = deployment.get(key_name)
        if value:
            return str(value)
    return ""


def dokploy_request(
    *,
    host: str,
    token: str,
    path: str,
    method: str = "GET",
    payload: JsonObject | None = None,
    query: dict[str, str | int] | None = None,
    timeout_seconds: int | float = 60,
) -> JsonValue:
    normalized_host = host.rstrip("/")
    normalized_path = path if path.startswith("/") else f"/{path}"
    request_url = f"{normalized_host}{normalized_path}"
    if query:
        request_url = f"{request_url}?{urlencode(query)}"
    request_headers = {"x-api-key": token}
    request_body: bytes | None = None
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
        request_body = json.dumps(payload).encode()
    request = Request(request_url, data=request_body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw_payload = response.read()
    except HTTPError as error:
        error_body = error.read().decode(errors="replace").strip()
        raise click.ClickException(
            f"Dokploy API {method} {normalized_path} failed ({error.code}): {error_body}"
        ) from error
    except URLError as error:
        raise click.ClickException(f"Dokploy API {method} {normalized_path} request failed: {error.reason}") from error

    if not raw_payload:
        return {}
    try:
        return json.loads(raw_payload)
    except json.JSONDecodeError:
        return {"raw": raw_payload.decode("utf-8", errors="replace")}


def as_json_object(value: JsonValue) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    if not all(isinstance(key_name, str) for key_name in value):
        return None
    return value


def extract_schedules(raw_payload: JsonValue) -> list[JsonObject]:
    if isinstance(raw_payload, list):
        return _collect_object_items(raw_payload)
    if isinstance(raw_payload, dict):
        for key_name in ("data", "schedules", "items", "result"):
            nested_items = raw_payload.get(key_name)
            if isinstance(nested_items, list):
                return _collect_object_items(nested_items)
    return []


def extract_deployments(raw_payload: JsonValue) -> list[JsonObject]:
    if isinstance(raw_payload, list):
        return _collect_object_items(raw_payload)
    if isinstance(raw_payload, dict):
        for key_name in ("data", "deployments", "items", "result"):
            nested_items = raw_payload.get(key_name)
            if isinstance(nested_items, list):
                return _collect_object_items(nested_items)
    return []


def _collect_object_items(raw_items: list[JsonValue]) -> list[JsonObject]:
    object_items: list[JsonObject] = []
    for raw_item in raw_items:
        item_as_object = as_json_object(raw_item)
        if item_as_object is not None:
            object_items.append(item_as_object)
    return object_items


def _wait_for_deployment_status(
    *,
    fetch_latest_deployment: Callable[[], JsonObject | None],
    before_key: str,
    timeout_seconds: int,
    failure_message_prefix: str,
) -> str:
    success_statuses = {"success", "succeeded", "done", "completed", "healthy", "finished"}
    failure_statuses = {"failed", "error", "canceled", "cancelled", "killed", "unhealthy", "timeout"}

    start_time = time.monotonic()
    while time.monotonic() - start_time <= timeout_seconds:
        latest_deployment = fetch_latest_deployment()
        if latest_deployment is None:
            time.sleep(3)
            continue

        latest_key = deployment_key(latest_deployment)
        latest_status = _deployment_status(latest_deployment)
        if latest_key and latest_key != before_key:
            if latest_status in success_statuses:
                return f"deployment={latest_key} status={latest_status}"
            if latest_status in failure_statuses:
                raise click.ClickException(f"{failure_message_prefix}: deployment={latest_key} status={latest_status}")
            if not latest_status:
                return f"deployment={latest_key} status=unknown"
        time.sleep(3)

    raise click.ClickException("Timed out waiting for Dokploy deployment status.")


def _latest_deployment_from_list(deployments: list[JsonObject]) -> JsonObject | None:
    if not deployments:
        return None
    return max(deployments, key=_deployment_sort_key)


def _deployment_sort_key(deployment: JsonObject) -> str:
    for key_name in ("createdAt", "created_at", "updatedAt", "updated_at"):
        value = deployment.get(key_name)
        if value:
            return str(value)
    return deployment_key(deployment)


def _deployment_status(deployment: JsonObject) -> str:
    for key_name in ("status", "state", "deploymentStatus"):
        value = deployment.get(key_name)
        if value:
            return str(value).strip().lower()
    return ""


def _build_dokploy_data_workflow_schedule_app_name(*, context_name: str, instance_name: str) -> str:
    return f"platform-{context_name}-{instance_name}-data-workflow"


def _resolve_dokploy_schedule_runtime(
    *,
    host: str,
    token: str,
    compose_id: str,
    compose_name: str,
    target_payload: JsonObject,
) -> tuple[str, str, str, str | None]:
    compose_app_name = str(target_payload.get("appName") or "").strip()
    if not compose_app_name:
        raise click.ClickException(f"Dokploy compose {compose_name!r} ({compose_id}) has no appName in API response.")
    compose_server_id = str(target_payload.get("serverId") or "").strip()
    if compose_server_id:
        return "server", compose_server_id, compose_app_name, compose_server_id
    user_id = resolve_dokploy_user_id(host=host, token=token)
    return "dokploy-server", user_id, compose_app_name, None


def _build_dokploy_data_workflow_script(
    *,
    compose_app_name: str,
    database_name: str,
    filestore_path: str,
    clear_stale_lock: bool,
    data_workflow_lock_path: str,
) -> str:
    normalized_filestore_path = filestore_path.strip() or "/volumes/data/filestore"
    quoted_compose_app_name = shlex.quote(compose_app_name)
    quoted_database_name = shlex.quote(database_name)
    quoted_filestore_path = shlex.quote(normalized_filestore_path)
    quoted_lock_path = shlex.quote(data_workflow_lock_path)
    return f"""#!/usr/bin/env bash
set -euo pipefail

compose_project={quoted_compose_app_name}
database_name={quoted_database_name}
filestore_root={quoted_filestore_path}
workflow_ssh_dir=/tmp/platform-data-workflow-ssh
workflow_arguments=(--update-only)
clear_stale_lock={'1' if clear_stale_lock else '0'}
data_workflow_lock_path={quoted_lock_path}

resolve_container_id() {{
    local service_name="$1"
    local container_id
    container_id=$(docker ps -aq \
        --filter "label=com.docker.compose.project=${{compose_project}}" \
        --filter "label=com.docker.compose.service=${{service_name}}" | head -n 1)
    if [ -z "${{container_id}}" ]; then
        echo "Missing container for service '${{service_name}}' in project '${{compose_project}}'." >&2
        exit 1
    fi
    printf '%s' "${{container_id}}"
}}

ensure_running() {{
    local container_id="$1"
    local service_name="$2"
    local current_status
    current_status=$(docker inspect -f '{{{{.State.Status}}}}' "${{container_id}}")
    if [ "${{current_status}}" != "running" ]; then
        echo "Starting ${{service_name}} container ${{container_id}}"
        docker start "${{container_id}}" >/dev/null
    fi
}}

start_web_container() {{
    local current_status
    current_status=$(docker inspect -f '{{{{.State.Status}}}}' "${{web_container_id}}" 2>/dev/null || true)
    if [ "${{current_status}}" != "running" ]; then
        echo "Starting web container ${{web_container_id}}"
        docker start "${{web_container_id}}" >/dev/null || true
    fi
}}

database_container_id=$(resolve_container_id "database")
script_runner_container_id=$(resolve_container_id "script-runner")
web_container_id=$(resolve_container_id "web")

ensure_running "${{database_container_id}}" "database"
ensure_running "${{script_runner_container_id}}" "script-runner"
workflow_uid=$(docker exec "${{script_runner_container_id}}" id -u)
workflow_gid=$(docker exec "${{script_runner_container_id}}" id -g)

if [ "${{clear_stale_lock}}" = "1" ]; then
    echo "Clearing stale data workflow lock ${{data_workflow_lock_path}}"
    docker exec -u root "${{script_runner_container_id}}" rm -f "${{data_workflow_lock_path}}"
fi

trap start_web_container EXIT

web_status=$(docker inspect -f '{{{{.State.Status}}}}' "${{web_container_id}}")
if [ "${{web_status}}" = "running" ]; then
    echo "Stopping web container ${{web_container_id}}"
    docker stop "${{web_container_id}}" >/dev/null
fi

echo "Normalizing filestore ownership for ${{database_name}}"
workflow_identity_key=$(docker exec -u root \
    -e ODOO_DATABASE_NAME="${{database_name}}" \
    -e ODOO_FILESTORE_ROOT="${{filestore_root}}" \
    -e DATA_WORKFLOW_SSH_DIR="${{DATA_WORKFLOW_SSH_DIR:-/root/.ssh}}" \
    -e DATA_WORKFLOW_SSH_KEY="${{DATA_WORKFLOW_SSH_KEY:-}}" \
    -e WORKFLOW_UID="${{workflow_uid}}" \
    -e WORKFLOW_GID="${{workflow_gid}}" \
    -e WORKFLOW_SSH_DIR="${{workflow_ssh_dir}}" \
    "${{script_runner_container_id}}" \
    /bin/bash -lc '
        set -euo pipefail
        target_owner=$(stat -c "%u:%g" /volumes/data)
        filestore_database_path="$ODOO_FILESTORE_ROOT"
        if [ "$(basename "$filestore_database_path")" != "$ODOO_DATABASE_NAME" ]; then
            filestore_database_path="$filestore_database_path/$ODOO_DATABASE_NAME"
        fi
        mkdir -p "$ODOO_FILESTORE_ROOT" "$filestore_database_path"
        chown -R "$target_owner" "$filestore_database_path"
        chmod -R ug+rwX "$filestore_database_path"

        rm -rf "$WORKFLOW_SSH_DIR"
        install -d -m 700 -o "$WORKFLOW_UID" -g "$WORKFLOW_GID" "$WORKFLOW_SSH_DIR"

        if [ -f "$DATA_WORKFLOW_SSH_DIR/known_hosts" ]; then
            install -m 600 -o "$WORKFLOW_UID" -g "$WORKFLOW_GID" \
                "$DATA_WORKFLOW_SSH_DIR/known_hosts" "$WORKFLOW_SSH_DIR/known_hosts"
        fi

        source_key_path="$DATA_WORKFLOW_SSH_KEY"
        if [ -z "$source_key_path" ]; then
            for candidate_key in id_ed25519 id_ecdsa id_rsa id_dsa; do
                if [ -f "$DATA_WORKFLOW_SSH_DIR/$candidate_key" ]; then
                    source_key_path="$DATA_WORKFLOW_SSH_DIR/$candidate_key"
                    break
                fi
            done
        fi
        workflow_identity_key=""
        if [ -n "$source_key_path" ] && [ -f "$source_key_path" ]; then
            workflow_identity_key="$WORKFLOW_SSH_DIR/$(basename "$source_key_path")"
            install -m 600 -o "$WORKFLOW_UID" -g "$WORKFLOW_GID" \
                "$source_key_path" "$workflow_identity_key"
        fi
        printf "%s" "$workflow_identity_key"
    ')

echo "Running post-deploy update in container ${{script_runner_container_id}}"
docker exec \
    -e DATA_WORKFLOW_SSH_DIR="${{workflow_ssh_dir}}" \
    -e DATA_WORKFLOW_SSH_KEY="$workflow_identity_key" \
    "${{script_runner_container_id}}" \
    python3 -u /volumes/scripts/run_odoo_data_workflows.py "${{workflow_arguments[@]}}"

start_web_container
trap - EXIT
"""


def _schedule_deployments(schedule: JsonObject | None) -> tuple[JsonObject, ...]:
    if not isinstance(schedule, dict):
        return ()
    raw_deployments = schedule.get("deployments")
    if not isinstance(raw_deployments, list):
        return ()
    return tuple(_collect_object_items(raw_deployments))


def _has_running_schedule_deployment(schedule: JsonObject | None) -> bool:
    return any(
        _deployment_status(deployment) in DOKPLOY_RUNNING_DEPLOYMENT_STATUSES
        for deployment in _schedule_deployments(schedule)
    )


def _should_clear_stale_data_workflow_lock(schedule: JsonObject | None) -> bool:
    deployments = _schedule_deployments(schedule)
    if not deployments or _has_running_schedule_deployment(schedule):
        return False
    for deployment in deployments:
        deployment_status_value = _deployment_status(deployment)
        if deployment_status_value in DOKPLOY_CANCELLED_DEPLOYMENT_STATUSES:
            return True
        if deployment_status_value in DOKPLOY_SUCCESS_DEPLOYMENT_STATUSES:
            return False
    return False


def _apply_post_deploy_env_file_overrides(*, current_env_map: dict[str, str], env_file: Path | None) -> dict[str, str]:
    if env_file is None:
        return dict(current_env_map)
    desired_env_map = dict(current_env_map)
    unsupported_keys: list[str] = []
    for env_key, env_value in _parse_env_file(env_file).items():
        if env_key in POST_DEPLOY_UPDATE_IGNORED_ENV_KEYS:
            continue
        if env_key not in POST_DEPLOY_UPDATE_ALLOWED_ENV_KEYS:
            unsupported_keys.append(env_key)
            continue
        desired_env_map[env_key] = env_value
    if unsupported_keys:
        allowed_key_list = ", ".join(sorted(POST_DEPLOY_UPDATE_ALLOWED_ENV_KEYS))
        unsupported_key_list = ", ".join(sorted(unsupported_keys))
        raise click.ClickException(
            "Compose post-deploy env overlay only supports "
            f"{allowed_key_list}. Unsupported keys: {unsupported_key_list}."
        )
    return desired_env_map


def _parse_env_file(env_file: Path) -> dict[str, str]:
    env_values: dict[str, str] = {}
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        stripped_line = raw_line.strip()
        if not stripped_line or stripped_line.startswith("#"):
            continue
        if stripped_line.startswith("export "):
            stripped_line = stripped_line[7:].strip()
        if "=" not in stripped_line:
            continue
        key_name, value = stripped_line.split("=", 1)
        env_values[key_name.strip()] = value
    return env_values


def _normalize_dokploy_source_payload(raw_value: object) -> object:
    if not isinstance(raw_value, Mapping):
        return raw_value

    normalized_payload = dict(raw_value)
    allowed_top_level_keys = {"defaults", "profiles", "projects", "schema_version", "targets"}
    unknown_keys = sorted(key_name for key_name in normalized_payload if key_name not in allowed_top_level_keys)
    if unknown_keys:
        unknown_key_list = ", ".join(unknown_keys)
        raise ValueError(f"Unknown top-level dokploy keys: {unknown_key_list}")

    raw_targets = normalized_payload.get("targets")
    if not isinstance(raw_targets, list):
        return raw_value

    defaults = _expect_mapping(normalized_payload.get("defaults"), label="defaults")
    raw_profiles = _expect_mapping(normalized_payload.get("profiles"), label="profiles")
    raw_projects = _expect_mapping(normalized_payload.get("projects"), label="projects")

    resolved_profiles: dict[str, dict[str, object]] = {}
    targets: list[object] = []
    for target_index, raw_target in enumerate(raw_targets, start=1):
        if not isinstance(raw_target, Mapping):
            targets.append(raw_target)
            continue

        target_payload = dict(raw_target)
        profile_name = str(target_payload.pop("profile", "") or "").strip()
        merged_target = dict(defaults)
        if profile_name:
            merged_target = _merge_dokploy_settings(
                merged_target,
                _resolve_dokploy_profile(
                    profile_name,
                    raw_profiles=raw_profiles,
                    raw_projects=raw_projects,
                    resolved_profiles=resolved_profiles,
                    active_profiles=(),
                ),
            )
        merged_target = _merge_dokploy_settings(merged_target, target_payload)
        targets.append(
            _resolve_dokploy_project_reference(
                merged_target,
                raw_projects=raw_projects,
                label=f"targets[{target_index}]",
            )
        )

    return {
        "schema_version": normalized_payload.get("schema_version"),
        "targets": targets,
    }


def _resolve_dokploy_profile(
    profile_name: str,
    *,
    raw_profiles: Mapping[str, object],
    raw_projects: Mapping[str, object],
    resolved_profiles: dict[str, dict[str, object]],
    active_profiles: tuple[str, ...],
) -> dict[str, object]:
    if profile_name in resolved_profiles:
        return dict(resolved_profiles[profile_name])
    if profile_name in active_profiles:
        profile_chain = " -> ".join((*active_profiles, profile_name))
        raise ValueError(f"Dokploy profile inheritance cycle detected: {profile_chain}")

    raw_profile = raw_profiles.get(profile_name)
    if raw_profile is None:
        raise ValueError(f"Unknown dokploy profile: {profile_name}")
    if not isinstance(raw_profile, Mapping):
        raise ValueError(f"Dokploy profile '{profile_name}' must be a table/object")

    profile_payload = dict(raw_profile)
    parent_profile_name = str(profile_payload.pop("extends", "") or "").strip()
    merged_profile: dict[str, object] = {}
    if parent_profile_name:
        merged_profile = _resolve_dokploy_profile(
            parent_profile_name,
            raw_profiles=raw_profiles,
            raw_projects=raw_projects,
            resolved_profiles=resolved_profiles,
            active_profiles=(*active_profiles, profile_name),
        )
    merged_profile = _merge_dokploy_settings(merged_profile, profile_payload)
    merged_profile = _resolve_dokploy_project_reference(
        merged_profile,
        raw_projects=raw_projects,
        label=f"profiles.{profile_name}",
    )
    resolved_profiles[profile_name] = dict(merged_profile)
    return merged_profile


def _resolve_dokploy_project_reference(
    payload: dict[str, object],
    *,
    raw_projects: Mapping[str, object],
    label: str,
) -> dict[str, object]:
    resolved_payload = dict(payload)
    raw_project_alias = resolved_payload.pop("project", None)
    if raw_project_alias in (None, ""):
        return resolved_payload

    project_alias = str(raw_project_alias).strip()
    if not project_alias:
        return resolved_payload
    if str(resolved_payload.get("project_name") or "").strip():
        raise ValueError(f"{label} cannot define both project and project_name")

    raw_project_value = raw_projects.get(project_alias)
    if raw_project_value is None:
        raise ValueError(f"Unknown dokploy project alias '{project_alias}' in {label}")
    if isinstance(raw_project_value, str):
        project_name = raw_project_value.strip()
    elif isinstance(raw_project_value, Mapping):
        project_name = str(raw_project_value.get("project_name") or "").strip()
    else:
        raise ValueError(f"Dokploy project alias '{project_alias}' in {label} must be a string or table")
    if not project_name:
        raise ValueError(f"Dokploy project alias '{project_alias}' in {label} is missing project_name")

    resolved_payload["project_name"] = project_name
    return resolved_payload


def _expect_mapping(raw_value: object, *, label: str) -> dict[str, object]:
    if raw_value in (None, ""):
        return {}
    if not isinstance(raw_value, Mapping):
        raise ValueError(f"Dokploy {label} must be a table/object")
    if not all(isinstance(key_name, str) for key_name in raw_value):
        raise ValueError(f"Dokploy {label} keys must be strings")
    return dict(raw_value)


def _merge_dokploy_settings(base: Mapping[str, object], overlay: Mapping[str, object]) -> dict[str, object]:
    merged_settings = dict(base)
    for key_name, key_value in overlay.items():
        base_env = merged_settings.get("env")
        if key_name == "env" and isinstance(base_env, Mapping) and isinstance(key_value, Mapping):
            merged_env: dict[str, object] = {}
            for env_key, env_value in base_env.items():
                if isinstance(env_key, str):
                    merged_env[env_key] = env_value
            for env_key, env_value in key_value.items():
                if isinstance(env_key, str):
                    merged_env[env_key] = env_value
            merged_settings["env"] = merged_env
            continue
        merged_settings[key_name] = key_value
    return merged_settings
