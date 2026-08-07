import json
import re
import time

from collections.abc import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import click


DEFAULT_DOKPLOY_LOG_LINE_COUNT = 200
MAX_DOKPLOY_LOG_LINE_COUNT = 1000
_DOKPLOY_SCHEDULE_DIAGNOSTIC_LINE_COUNT = 20
_MAX_DOKPLOY_ERROR_DETAIL_LENGTH = 2000
_MAX_DOKPLOY_SCHEDULE_DIAGNOSTIC_LENGTH = 4000
_SECRET_LOG_KEY_PATTERN = (
    r"[A-Z0-9_]*(?:PASSWORD|PASS|TOKEN|SECRET|API_KEY|ACCESS_KEY|PRIVATE_KEY|"
    r"DATABASE_URL|CREDENTIAL|ARGS_BASE64|PAYLOAD_BASE64)[A-Z0-9_]*"
)
_LIKELY_SECRET_LOG_VALUE_PATTERN = re.compile(
    rf"(?i)(\b{_SECRET_LOG_KEY_PATTERN}\s*[=:]\s*)([^\s,;]+)"
)
_DOUBLE_QUOTED_SECRET_LOG_VALUE_PATTERN = re.compile(
    rf'(?i)("?\b{_SECRET_LOG_KEY_PATTERN}"?\s*[=:]\s*)"[^"\r\n]*"'
)
_SINGLE_QUOTED_SECRET_LOG_VALUE_PATTERN = re.compile(
    rf"(?i)('?\b{_SECRET_LOG_KEY_PATTERN}'?\s*[=:]\s*)'[^'\r\n]*'"
)
_BEARER_LOG_VALUE_PATTERN = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_DATABASE_URI_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b((?:postgres(?:ql)?|mysql|mariadb)://[^:\s/@]+:)[^@\s]+(@)"
)
_DOKPLOY_LOG_SINCE_PATTERN = re.compile(r"^(all|\d+[smhd])$")
_DOKPLOY_LOG_SEARCH_PATTERN = re.compile(r"^[a-zA-Z0-9 ._-]{0,500}$")
_DOKPLOY_COMPOSE_SERVICE_PATTERN = re.compile(r"^$|^[a-z0-9][a-z0-9._-]{0,127}$")
type JsonPrimitive = str | int | float | bool | None
type JsonValue = JsonPrimitive | dict[str, "JsonValue"] | list["JsonValue"]
type JsonObject = dict[str, JsonValue]


class DokployDeploymentFailed(click.ClickException):
    def __init__(
        self,
        *,
        deployment_id: str,
        deployment_status: str,
        message_prefix: str,
    ) -> None:
        self.deployment_id = deployment_id
        self.deployment_status = deployment_status
        super().__init__(f"{message_prefix}: deployment={deployment_id} status={deployment_status}")


class DokployRequestFailed(click.ClickException):
    def __init__(
        self,
        *,
        method: str,
        path: str,
        detail: str,
        status_code: int | None = None,
        remote_command_failed: bool = False,
    ) -> None:
        self.method = method
        self.path = path
        self.status_code = status_code
        self.remote_command_failed = remote_command_failed
        self.retryable = status_code is None or status_code in {408, 425, 429} or status_code >= 500
        if status_code is None:
            message = f"Dokploy API {method} {path} request failed: {detail}"
        else:
            message = f"Dokploy API {method} {path} failed ({status_code}): {detail}"
        super().__init__(message)


class DokployScheduleExecutionFailed(click.ClickException):
    def __init__(
        self,
        *,
        schedule_id: str,
        deployment_id: str,
        deployment_status: str,
        cause: str,
        provider_detail: str = "",
        recent_output: tuple[str, ...] = (),
    ) -> None:
        self.schedule_id = schedule_id
        self.deployment_id = deployment_id
        self.deployment_status = deployment_status
        self.cause = cause
        self.provider_detail = provider_detail
        self.recent_output = recent_output
        message = (
            "Dokploy schedule execution failed: "
            f"schedule={schedule_id} deployment={deployment_id or 'unavailable'} "
            f"status={deployment_status or 'unknown'} cause={cause}."
        )
        if provider_detail:
            message = f"{message} Provider detail: {provider_detail}"
        if recent_output:
            message = f"{message}\nRecent redacted deployment output:\n" + "\n".join(recent_output)
        super().__init__(message)


def is_transient_dokploy_error(error: BaseException) -> bool:
    return isinstance(error, DokployRequestFailed) and error.retryable


def trigger_deployment(
    *,
    host: str,
    token: str,
    target_type: str,
    target_id: str,
    no_cache: bool,
    title: str = "",
) -> None:
    if target_type == "compose":
        endpoint_path = "/api/compose.redeploy" if no_cache else "/api/compose.deploy"
        payload: JsonObject = {"composeId": target_id}
    elif target_type == "application":
        endpoint_path = "/api/application.redeploy" if no_cache else "/api/application.deploy"
        payload = {"applicationId": target_id}
    else:
        raise click.ClickException(f"Unsupported Dokploy target type: {target_type}")
    normalized_title = title.strip()
    if normalized_title:
        payload["title"] = normalized_title
    elif no_cache:
        payload["title"] = "Manual redeploy (no-cache requested)"
    dokploy_request(host=host, token=token, path=endpoint_path, method="POST", payload=payload)


def latest_deployment_for_target(
    *, host: str, token: str, target_type: str, target_id: str
) -> JsonObject | None:
    return _latest_deployment_from_list(
        list_deployments_for_target(
            host=host,
            token=token,
            target_type=target_type,
            target_id=target_id,
        )
    )


def list_deployments_for_target(
    *, host: str, token: str, target_type: str, target_id: str
) -> list[JsonObject]:
    if target_type == "compose":
        compose_payload = dokploy_request(
            host=host,
            token=token,
            path="/api/compose.one",
            query={"composeId": target_id},
        )
        compose_payload_as_object = as_json_object(compose_payload)
        if compose_payload_as_object is None:
            return []
        deployments_payload = compose_payload_as_object.get("deployments")
        if not isinstance(deployments_payload, list):
            return []
        return _collect_object_items(deployments_payload)

    if target_type == "application":
        payload = dokploy_request(
            host=host,
            token=token,
            path="/api/deployment.all",
            query={"applicationId": target_id},
        )
        return extract_deployments(payload)

    raise click.ClickException(f"Unsupported Dokploy target type: {target_type}")


def deployment_for_target_by_title(
    *,
    host: str,
    token: str,
    target_type: str,
    target_id: str,
    title: str,
) -> JsonObject | None:
    normalized_title = title.strip()
    if not normalized_title:
        raise click.ClickException("Dokploy deployment observation requires a title.")
    matching_deployments = [
        deployment
        for deployment in list_deployments_for_target(
            host=host,
            token=token,
            target_type=target_type,
            target_id=target_id,
        )
        if str(deployment.get("title") or "").strip() == normalized_title
    ]
    return _latest_deployment_from_list(matching_deployments)


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
        raise click.ClickException(
            f"Dokploy {target_type}.one returned an invalid response payload."
        )
    return payload_as_object


def normalize_dokploy_log_line_count(line_count: int) -> int:
    if line_count < 1:
        raise click.ClickException("Dokploy log line count must be at least 1.")
    if line_count > MAX_DOKPLOY_LOG_LINE_COUNT:
        raise click.ClickException(
            f"Dokploy log line count cannot exceed {MAX_DOKPLOY_LOG_LINE_COUNT}."
        )
    return line_count


def normalize_dokploy_log_since(raw_since: str) -> str:
    since = raw_since.strip() or "all"
    if not _DOKPLOY_LOG_SINCE_PATTERN.fullmatch(since):
        raise click.ClickException(
            "Dokploy log --since must be 'all' or a duration like 5m, 2h, or 1d."
        )
    return since


def normalize_dokploy_log_search(raw_search: str) -> str:
    search = raw_search.strip()
    if not _DOKPLOY_LOG_SEARCH_PATTERN.fullmatch(search):
        raise click.ClickException(
            "Dokploy log --search may contain only letters, numbers, spaces, dots, underscores, and dashes."
        )
    return search


def normalize_dokploy_compose_service_name(raw_service_name: str) -> str:
    service_name = raw_service_name.strip().lower()
    if not _DOKPLOY_COMPOSE_SERVICE_PATTERN.fullmatch(service_name):
        raise click.ClickException(
            "Dokploy compose log service must contain only lowercase letters, numbers, dots, "
            "underscores, and dashes."
        )
    return service_name


def redact_dokploy_log_line(raw_line: str) -> str:
    redacted_line = _DOUBLE_QUOTED_SECRET_LOG_VALUE_PATTERN.sub(r'\1"[redacted]"', raw_line)
    redacted_line = _SINGLE_QUOTED_SECRET_LOG_VALUE_PATTERN.sub(r"\1'[redacted]'", redacted_line)
    redacted_line = _LIKELY_SECRET_LOG_VALUE_PATTERN.sub(r"\1[redacted]", redacted_line)
    redacted_line = _BEARER_LOG_VALUE_PATTERN.sub(r"\1[redacted]", redacted_line)
    return _DATABASE_URI_CREDENTIAL_PATTERN.sub(r"\1[redacted]\2", redacted_line)


def normalize_dokploy_log_payload(payload: JsonValue) -> tuple[str, ...]:
    raw_lines: list[str] = []
    if isinstance(payload, str):
        raw_lines.extend(payload.splitlines())
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, str):
                raw_lines.extend(item.splitlines())
            elif isinstance(item, dict):
                message = item.get("message") or item.get("log") or item.get("line")
                if message is not None:
                    raw_lines.extend(str(message).splitlines())
    elif isinstance(payload, dict):
        for key_name in ("message", "line"):
            value = payload.get(key_name)
            if value is not None:
                raw_lines.extend(str(value).splitlines())
                break
        for key_name in ("logs", "log", "lines", "output", "raw"):
            value = payload.get(key_name)
            if isinstance(value, str):
                raw_lines.extend(value.splitlines())
                break
            if isinstance(value, list):
                raw_lines.extend(
                    line for item in value for line in normalize_dokploy_log_payload(item)
                )
                break
    return tuple(redact_dokploy_log_line(line) for line in raw_lines)


def fetch_dokploy_application_logs(
    *,
    host: str,
    token: str,
    application_id: str,
    line_count: int = DEFAULT_DOKPLOY_LOG_LINE_COUNT,
    since: str = "all",
    search: str = "",
) -> tuple[str, ...]:
    normalized_application_id = application_id.strip()
    if not normalized_application_id:
        raise click.ClickException("Dokploy application logs require an application id.")
    normalized_line_count = normalize_dokploy_log_line_count(line_count)
    normalized_since = normalize_dokploy_log_since(since)
    normalized_search = normalize_dokploy_log_search(search)
    query: dict[str, str | int] = {
        "applicationId": normalized_application_id,
        "tail": normalized_line_count,
        "since": normalized_since,
    }
    if normalized_search:
        query["search"] = normalized_search
    payload = dokploy_request(
        host=host,
        token=token,
        path="/api/application.readLogs",
        query=query,
    )
    lines = normalize_dokploy_log_payload(payload)
    return lines[-normalized_line_count:]


def fetch_dokploy_compose_logs(
    *,
    host: str,
    token: str,
    compose_id: str,
    app_name: str = "",
    server_id: str = "",
    service_name: str = "",
    line_count: int = DEFAULT_DOKPLOY_LOG_LINE_COUNT,
    since: str = "all",
    search: str = "",
) -> tuple[str, ...]:
    normalized_compose_id = compose_id.strip()
    if not normalized_compose_id:
        raise click.ClickException("Dokploy compose logs require a compose id.")
    normalized_line_count = normalize_dokploy_log_line_count(line_count)
    normalized_since = normalize_dokploy_log_since(since)
    normalized_search = normalize_dokploy_log_search(search)
    normalized_app_name = app_name.strip()
    normalized_server_id = server_id.strip()
    normalized_service_name = normalize_dokploy_compose_service_name(service_name)
    if normalized_service_name and not normalized_app_name:
        raise click.ClickException(
            "Dokploy compose service log selection requires tracked application metadata."
        )
    query: dict[str, str | int] = {
        "composeId": normalized_compose_id,
        "tail": normalized_line_count,
        "since": normalized_since,
    }
    if normalized_app_name:
        container_query: dict[str, str | int] = {
            "appName": normalized_app_name,
            "appType": "docker-compose",
        }
        if normalized_server_id:
            container_query["serverId"] = normalized_server_id
        containers_payload = dokploy_request(
            host=host,
            token=token,
            path="/api/docker.getContainersByAppNameMatch",
            query=container_query,
        )
        containers = (
            _collect_object_items(containers_payload)
            if isinstance(containers_payload, list)
            else []
        )
        selected_container = _select_compose_log_container(
            containers,
            app_name=normalized_app_name,
            service_name=normalized_service_name,
        )
        container_id = str(selected_container.get("containerId") or "").strip()
        if normalized_service_name and not container_id:
            raise click.ClickException(
                f"Dokploy compose log service {normalized_service_name!r} has no container id."
            )
        if container_id:
            query["containerId"] = container_id
    if normalized_search:
        query["search"] = normalized_search
    payload = dokploy_request(
        host=host,
        token=token,
        path="/api/compose.readLogs",
        query=query,
    )
    lines = normalize_dokploy_log_payload(payload)
    return lines[-normalized_line_count:]


def fetch_dokploy_deployment_logs(
    *,
    host: str,
    token: str,
    deployment_id: str,
    line_count: int = DEFAULT_DOKPLOY_LOG_LINE_COUNT,
) -> tuple[str, ...]:
    normalized_deployment_id = deployment_id.strip()
    if not normalized_deployment_id:
        raise click.ClickException("Dokploy deployment logs require a deployment id.")
    normalized_line_count = normalize_dokploy_log_line_count(line_count)
    payload = dokploy_request(
        host=host,
        token=token,
        path="/api/deployment.readLogs",
        query={"deploymentId": normalized_deployment_id, "tail": normalized_line_count},
    )
    lines = normalize_dokploy_log_payload(payload)
    return lines[-normalized_line_count:]


def _select_compose_log_container(
    containers: list[JsonObject],
    *,
    app_name: str = "",
    service_name: str = "",
) -> JsonObject:
    normalized_service_name = normalize_dokploy_compose_service_name(service_name)
    if normalized_service_name:
        service_matches = [
            container
            for container in containers
            if _compose_log_container_matches_service(
                container,
                app_name=app_name,
                service_name=normalized_service_name,
            )
        ]
        if len(service_matches) != 1:
            qualifier = "not found" if not service_matches else "ambiguous"
            raise click.ClickException(
                f"Dokploy compose log service {normalized_service_name!r} is {qualifier}."
            )
        return service_matches[0]
    for container in containers:
        if _compose_log_container_has_web_service(container):
            return container
    for container in containers:
        if _compose_log_container_has_web_name(container):
            return container
    for container in containers:
        if str(container.get("containerId") or "").strip():
            return container
    return {}


def _compose_log_container_matches_service(
    container: JsonObject,
    *,
    app_name: str,
    service_name: str,
) -> bool:
    service_names = _compose_log_container_service_names(container)
    if service_names:
        return service_name in service_names
    normalized_app_name = app_name.strip().lower()
    if not normalized_app_name:
        return False
    for container_name in _compose_log_container_names(container):
        normalized_name = container_name.strip().lower().strip("/")
        for separator in ("-", "_", "."):
            prefix = f"{normalized_app_name}{separator}"
            replica_match = re.search(rf"{re.escape(separator)}\d+$", normalized_name)
            if not normalized_name.startswith(prefix) or replica_match is None:
                continue
            matched_service_name = normalized_name[len(prefix) : replica_match.start()]
            if matched_service_name == service_name:
                return True
    return False


def _compose_log_container_is_web(container: JsonObject) -> bool:
    return _compose_log_container_has_web_service(container) or _compose_log_container_has_web_name(
        container
    )


def _compose_log_container_has_web_service(container: JsonObject) -> bool:
    for service_name in _compose_log_container_service_names(container):
        if service_name == "web":
            return True
    return False


def _compose_log_container_has_web_name(container: JsonObject) -> bool:
    for container_name in _compose_log_container_names(container):
        if _compose_log_container_name_is_web(container_name):
            return True
    return False


def _compose_log_container_service_names(container: JsonObject) -> tuple[str, ...]:
    service_names: list[str] = []
    for key in ("serviceName", "Service", "composeServiceName"):
        value = str(container.get(key) or "").strip().lower()
        if value:
            service_names.append(value)
    for labels_key in ("labels", "Labels"):
        labels = container.get(labels_key)
        if isinstance(labels, dict):
            for label_key, label_value in labels.items():
                if str(label_key).strip().lower() == "com.docker.compose.service":
                    value = str(label_value or "").strip().lower()
                    if value:
                        service_names.append(value)
    return tuple(service_names)


def _compose_log_container_names(container: JsonObject) -> tuple[str, ...]:
    names: list[str] = []
    for key in ("name", "Name", "containerName", "container_name"):
        value = str(container.get(key) or "").strip()
        if value:
            names.append(value)
    for key in ("names", "Names"):
        values = container.get(key)
        if isinstance(values, list):
            for raw_name in values:
                if raw_name is None:
                    continue
                normalized_value = str(raw_name).strip()
                if normalized_value:
                    names.append(normalized_value)
    return tuple(names)


def _compose_log_container_name_is_web(container_name: str) -> bool:
    normalized_name = container_name.strip().lower().strip("/")
    if not normalized_name:
        return False
    name_parts = tuple(part for part in re.split(r"[-_.]+", normalized_name) if part)
    if not name_parts:
        return False
    service_part = (
        name_parts[-2] if len(name_parts) > 1 and name_parts[-1].isdigit() else name_parts[-1]
    )
    return service_part == "web"


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


def render_dokploy_env_text_with_overrides(
    raw_env_text: str,
    *,
    updates: Mapping[str, str] | None = None,
    removals: tuple[str, ...] = (),
) -> str:
    env_map = parse_dokploy_env_text(raw_env_text)
    for env_key in removals:
        env_map.pop(env_key, None)
    if updates is not None:
        for env_key, env_value in updates.items():
            env_map[env_key] = env_value
    return serialize_dokploy_env_text(env_map)


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
        payload["buildArgs"] = build_args if isinstance(build_args, str) else ""
        payload["buildSecrets"] = build_secrets if isinstance(build_secrets, str) else ""
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
    deployment_title: str = "",
) -> str:
    failure_message_prefix = (
        "Dokploy compose deployment failed"
        if target_type == "compose"
        else "Dokploy deployment failed"
    )
    normalized_deployment_title = deployment_title.strip()

    def fetch_deployment() -> JsonObject | None:
        if normalized_deployment_title:
            return deployment_for_target_by_title(
                host=host,
                token=token,
                target_type=target_type,
                target_id=target_id,
                title=normalized_deployment_title,
            )
        return latest_deployment_for_target(
            host=host,
            token=token,
            target_type=target_type,
            target_id=target_id,
        )

    return _wait_for_deployment_status(
        fetch_latest_deployment=fetch_deployment,
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


def run_dokploy_schedule(
    *,
    host: str,
    token: str,
    schedule_id: str,
    timeout_seconds: int,
) -> str:
    latest_before = latest_deployment_for_schedule(
        host=host,
        token=token,
        schedule_id=schedule_id,
    )
    before_key = deployment_key(latest_before)
    try:
        dokploy_request(
            host=host,
            token=token,
            path="/api/schedule.runManually",
            method="POST",
            payload={"scheduleId": schedule_id},
            timeout_seconds=timeout_seconds,
        )
    except DokployRequestFailed as error:
        deployment = _latest_new_schedule_deployment(
            host=host,
            token=token,
            schedule_id=schedule_id,
            before_key=before_key,
        )
        cause = "remote_command_exit" if error.remote_command_failed else "trigger_request_failed"
        raise _schedule_execution_failure(
            host=host,
            token=token,
            schedule_id=schedule_id,
            deployment=deployment,
            cause=cause,
            provider_detail=str(error),
        ) from error

    try:
        return wait_for_dokploy_schedule_deployment(
            host=host,
            token=token,
            schedule_id=schedule_id,
            before_key=before_key,
            timeout_seconds=timeout_seconds,
        )
    except DokployDeploymentFailed as error:
        deployment = _latest_new_schedule_deployment(
            host=host,
            token=token,
            schedule_id=schedule_id,
            before_key=before_key,
        )
        if deployment_key(deployment) != error.deployment_id:
            deployment = {
                "deploymentId": error.deployment_id,
                "status": error.deployment_status,
            }
        raise _schedule_execution_failure(
            host=host,
            token=token,
            schedule_id=schedule_id,
            deployment=deployment,
            cause="remote_command_exit",
        ) from error
    except click.ClickException as error:
        deployment = _latest_new_schedule_deployment(
            host=host,
            token=token,
            schedule_id=schedule_id,
            before_key=before_key,
        )
        raise _schedule_execution_failure(
            host=host,
            token=token,
            schedule_id=schedule_id,
            deployment=deployment,
            cause="deployment_observation_failed",
            provider_detail=str(error),
        ) from error


def _latest_new_schedule_deployment(
    *,
    host: str,
    token: str,
    schedule_id: str,
    before_key: str,
) -> JsonObject | None:
    try:
        deployment = latest_deployment_for_schedule(
            host=host,
            token=token,
            schedule_id=schedule_id,
        )
    except click.ClickException:
        return None
    deployment_id = deployment_key(deployment)
    if not deployment_id or deployment_id == before_key:
        return None
    return deployment


def _schedule_execution_failure(
    *,
    host: str,
    token: str,
    schedule_id: str,
    deployment: JsonObject | None,
    cause: str,
    provider_detail: str = "",
) -> DokployScheduleExecutionFailed:
    deployment_id = deployment_key(deployment)
    recent_output: tuple[str, ...] = ()
    if deployment_id:
        try:
            recent_output = fetch_dokploy_deployment_logs(
                host=host,
                token=token,
                deployment_id=deployment_id,
                line_count=_DOKPLOY_SCHEDULE_DIAGNOSTIC_LINE_COUNT,
            )
        except click.ClickException:
            recent_output = ()
    redacted_provider_detail = redact_dokploy_log_line(provider_detail).strip()
    redacted_recent_output = _bounded_redacted_log_lines(recent_output)
    return DokployScheduleExecutionFailed(
        schedule_id=schedule_id,
        deployment_id=deployment_id,
        deployment_status=deployment_status(deployment),
        cause=cause,
        provider_detail=redacted_provider_detail[:_MAX_DOKPLOY_ERROR_DETAIL_LENGTH],
        recent_output=redacted_recent_output,
    )


def _bounded_redacted_log_lines(lines: tuple[str, ...]) -> tuple[str, ...]:
    remaining_length = _MAX_DOKPLOY_SCHEDULE_DIAGNOSTIC_LENGTH
    redacted_lines: list[str] = []
    for line in lines:
        if remaining_length <= 0:
            break
        redacted_line = redact_dokploy_log_line(line)
        redacted_lines.append(redacted_line[:remaining_length])
        remaining_length -= len(redacted_lines[-1])
    return tuple(redacted_lines)


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


def deployment_key(deployment: JsonObject | None) -> str:
    if deployment is None:
        return ""
    for key_name in ("deploymentId", "deployment_id", "id", "uuid"):
        value = deployment.get(key_name)
        if value:
            return str(value)
    return ""


def deployment_key_from_wait_result(wait_result: str) -> str:
    for token in wait_result.split():
        key, separator, value = token.partition("=")
        if key == "deployment" and separator and value:
            return value
    return ""


def deployment_status(deployment: JsonObject | None) -> str:
    if deployment is None:
        return ""
    return _deployment_status(deployment)


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
        redacted_error_body = redact_dokploy_log_line(error_body).strip()
        raise DokployRequestFailed(
            method=method,
            path=normalized_path,
            status_code=error.code,
            detail=redacted_error_body[:_MAX_DOKPLOY_ERROR_DETAIL_LENGTH],
            remote_command_failed=_is_remote_command_failure(error_body),
        ) from error
    except URLError as error:
        raise DokployRequestFailed(
            method=method,
            path=normalized_path,
            detail=redact_dokploy_log_line(str(error.reason)).strip()[
                :_MAX_DOKPLOY_ERROR_DETAIL_LENGTH
            ],
        ) from error

    if not raw_payload:
        return {}
    try:
        return _normalize_json_value(json.loads(raw_payload))
    except json.JSONDecodeError:
        return {"raw": raw_payload.decode("utf-8", errors="replace")}


def _is_remote_command_failure(error_body: str) -> bool:
    try:
        payload = json.loads(error_body)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        message = str(payload.get("message") or "")
    else:
        message = error_body
    return "remote command failed with exit code" in message.lower()


def _string_items(value: JsonValue | None) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _normalize_json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_normalize_json_value(item) for item in value]
    if isinstance(value, dict):
        normalized: JsonObject = {}
        for key_name, item in value.items():
            if isinstance(key_name, str):
                normalized[key_name] = _normalize_json_value(item)
        return normalized
    return str(value)


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
    failure_statuses = {
        "failed",
        "error",
        "canceled",
        "cancelled",
        "killed",
        "unhealthy",
        "timeout",
    }

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
                raise DokployDeploymentFailed(
                    deployment_id=latest_key,
                    deployment_status=latest_status,
                    message_prefix=failure_message_prefix,
                )
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
