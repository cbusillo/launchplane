from __future__ import annotations

import json
import re
from typing import Literal, TypedDict

import click

from control_plane.dokploy import api as dokploy_api

_STRUCTURED_EVENT_PATTERN = re.compile(r"^$|^[a-z][a-z0-9_]{0,127}$")
_IMAGE_ID_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
_IMMUTABLE_IMAGE_REFERENCE_PATTERN = re.compile(r"^[^\s@]+@sha256:[a-f0-9]{64}$")
_MAX_RUNTIME_TEXT_LENGTH = 500
_ALLOWED_STRUCTURED_EVENTS = frozenset(
    {
        "privileged_operation_worker_entrypoint_started",
        "privileged_operation_worker_entrypoint_probe_succeeded",
        "privileged_operation_worker_started",
        "privileged_operation_worker_store_build_started",
        "privileged_operation_worker_schema_probe_succeeded",
        "privileged_operation_worker_store_initialized",
        "privileged_operation_worker_first_poll_attempted",
        "privileged_operation_worker_poll_succeeded",
    }
)
_ACTIVATION_PROOF_STRUCTURED_EVENTS = frozenset({"privileged_operation_worker_poll_succeeded"})
_RUNTIME_LOG_PROVIDER_ERROR_MARKERS = (
    ("configured logging driver does not support reading", "unsupported_logging_driver"),
    ("no such container", "container_not_found"),
    ("error response from daemon:", "docker_daemon_error"),
    ("remote command failed", "provider_command_failed"),
    ("command execution failed", "provider_command_failed"),
)
type DokployEvidenceProviderOperation = Literal[
    "provider-config",
    "target-inspect",
    "container-list",
    "service-select",
    "container-config",
    "image-identity",
    "runtime-log-read",
]


class RuntimeLogClassification(TypedDict):
    structured_event_line_count: int
    json_line_count: int
    non_json_line_count: int
    provider_error_line_count: int
    provider_error_kind: str


class DokployEvidenceProviderError(RuntimeError):
    def __init__(self, operation: DokployEvidenceProviderOperation) -> None:
        self.operation = operation
        super().__init__(operation)


def normalize_structured_event_name(raw_event_name: str) -> str:
    event_name = raw_event_name.strip().lower()
    if not _STRUCTURED_EVENT_PATTERN.fullmatch(event_name):
        raise click.ClickException(
            "Dokploy structured event names may contain only lowercase letters, numbers, and underscores."
        )
    if event_name and event_name not in _ALLOWED_STRUCTURED_EVENTS:
        raise click.ClickException(
            "Dokploy runtime evidence supports only allow-listed structured events."
        )
    return event_name


def structured_event_is_activation_proof(event_name: str) -> bool:
    return event_name in _ACTIVATION_PROOF_STRUCTURED_EVENTS


def normalize_expected_image_reference(raw_image_reference: str) -> str:
    image_reference = raw_image_reference.strip()
    if not image_reference:
        return ""
    if len(image_reference) > _MAX_RUNTIME_TEXT_LENGTH or not (
        _IMMUTABLE_IMAGE_REFERENCE_PATTERN.fullmatch(image_reference)
    ):
        raise click.ClickException(
            "Expected Dokploy runtime image must be an immutable repository@sha256 reference."
        )
    return image_reference


def fetch_compose_service_runtime(
    *,
    host: str,
    token: str,
    compose_id: str,
    app_name: str,
    server_id: str = "",
    service_name: str,
) -> dokploy_api.JsonObject:
    normalized_compose_id = compose_id.strip()
    if not normalized_compose_id:
        raise DokployEvidenceProviderError("target-inspect")
    normalized_app_name = app_name.strip()
    if not normalized_app_name:
        raise DokployEvidenceProviderError("target-inspect")
    normalized_server_id = server_id.strip()
    try:
        normalized_service_name = dokploy_api.normalize_dokploy_compose_service_name(service_name)
    except click.ClickException as error:
        raise DokployEvidenceProviderError("service-select") from error
    if not normalized_service_name:
        raise DokployEvidenceProviderError("service-select")

    container_query: dict[str, str | int] = {
        "appName": normalized_app_name,
        "appType": "docker-compose",
    }
    if normalized_server_id:
        container_query["serverId"] = normalized_server_id
    try:
        containers_payload = dokploy_api.dokploy_request(
            host=host,
            token=token,
            path="/api/docker.getContainersByAppNameMatch",
            query=container_query,
        )
    except click.ClickException as error:
        raise DokployEvidenceProviderError("container-list") from error
    if not isinstance(containers_payload, list):
        raise DokployEvidenceProviderError("container-list")
    try:
        selected_container = dokploy_api.select_dokploy_compose_container(
            dokploy_api.collect_dokploy_object_items(containers_payload),
            app_name=normalized_app_name,
            service_name=normalized_service_name,
        )
    except click.ClickException as error:
        raise DokployEvidenceProviderError("service-select") from error
    container_id = str(selected_container.get("containerId") or "").strip()
    if not container_id:
        raise DokployEvidenceProviderError("service-select")

    config_query: dict[str, str | int] = {"containerId": container_id}
    if normalized_server_id:
        config_query["serverId"] = normalized_server_id
    try:
        config_payload = dokploy_api.dokploy_request(
            host=host,
            token=token,
            path="/api/docker.getConfig",
            query=config_query,
        )
    except click.ClickException as error:
        raise DokployEvidenceProviderError("container-config") from error
    container_config = _normalize_container_config(config_payload)
    if container_config is None:
        raise DokployEvidenceProviderError("container-config")
    raw_config = container_config.get("Config")
    configured_image = (
        str(raw_config.get("Image") or "").strip() if isinstance(raw_config, dict) else ""
    )
    if not configured_image:
        raise DokployEvidenceProviderError("image-identity")
    image_id = str(container_config.get("Image") or "").strip().lower()
    if not _IMAGE_ID_PATTERN.fullmatch(image_id):
        raise DokployEvidenceProviderError("image-identity")
    state = dokploy_api.redact_dokploy_log_line(
        str(selected_container.get("state") or selected_container.get("State") or "")
    )
    status = dokploy_api.redact_dokploy_log_line(
        str(selected_container.get("status") or selected_container.get("Status") or "")
    )
    immutable_image_reference = (
        configured_image
        if len(configured_image) <= _MAX_RUNTIME_TEXT_LENGTH
        and _IMMUTABLE_IMAGE_REFERENCE_PATTERN.fullmatch(configured_image)
        else ""
    )
    return {
        "compose_id": normalized_compose_id,
        "container_id": container_id,
        "service": normalized_service_name,
        "state": state.strip()[:_MAX_RUNTIME_TEXT_LENGTH],
        "status": status.strip()[:_MAX_RUNTIME_TEXT_LENGTH],
        "running": state.strip().lower() == "running",
        "configured_image": configured_image[:_MAX_RUNTIME_TEXT_LENGTH],
        "image_id": image_id,
        "immutable_image_reference": immutable_image_reference,
        "image_reference_immutable": bool(immutable_image_reference),
    }


def fetch_compose_container_logs(
    *,
    host: str,
    token: str,
    compose_id: str,
    container_id: str,
    line_count: int,
    search_text: str = "",
) -> tuple[str, ...]:
    normalized_compose_id = compose_id.strip()
    normalized_container_id = container_id.strip()
    if not normalized_compose_id or not normalized_container_id:
        raise DokployEvidenceProviderError("runtime-log-read")
    normalized_line_count = dokploy_api.normalize_dokploy_log_line_count(line_count)
    normalized_search_text = normalize_structured_event_name(search_text)
    query: dict[str, str | int] = {
        "composeId": normalized_compose_id,
        "containerId": normalized_container_id,
        "tail": normalized_line_count,
        "since": "1d",
    }
    if normalized_search_text:
        query["search"] = normalized_search_text
    try:
        payload = dokploy_api.dokploy_request(
            host=host,
            token=token,
            path="/api/compose.readLogs",
            query=query,
        )
    except click.ClickException as error:
        raise DokployEvidenceProviderError("runtime-log-read") from error
    lines = dokploy_api.normalize_dokploy_log_payload(payload)
    return lines[-normalized_line_count:]


def count_structured_log_events(lines: tuple[str, ...], *, event_name: str) -> int:
    normalized_event_name = normalize_structured_event_name(event_name)
    if not normalized_event_name:
        return 0
    matching_events = 0
    for line in lines:
        object_start = line.find("{")
        if object_start < 0:
            continue
        try:
            payload, _ = json.JSONDecoder().raw_decode(line[object_start:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("event") == normalized_event_name:
            matching_events += 1
    return matching_events


def summarize_runtime_log_lines(lines: tuple[str, ...]) -> RuntimeLogClassification:
    structured_event_line_count = 0
    json_line_count = 0
    non_json_line_count = 0
    provider_error_line_count = 0
    provider_error_kind = ""
    for line in lines:
        object_start = line.find("{")
        if object_start >= 0:
            try:
                payload, _ = json.JSONDecoder().raw_decode(line[object_start:])
            except json.JSONDecodeError:
                pass
            else:
                json_line_count += 1
                if isinstance(payload, dict) and isinstance(payload.get("event"), str):
                    structured_event_line_count += 1
                continue
        non_json_line_count += 1
        normalized_line = line.lower()
        for marker, error_kind in _RUNTIME_LOG_PROVIDER_ERROR_MARKERS:
            if marker in normalized_line:
                provider_error_line_count += 1
                if not provider_error_kind:
                    provider_error_kind = error_kind
                break
    return {
        "structured_event_line_count": structured_event_line_count,
        "json_line_count": json_line_count,
        "non_json_line_count": non_json_line_count,
        "provider_error_line_count": provider_error_line_count,
        "provider_error_kind": provider_error_kind,
    }


def _normalize_container_config(
    raw_config: dokploy_api.JsonValue,
) -> dokploy_api.JsonObject | None:
    container_config = dokploy_api.as_json_object(raw_config)
    if container_config is not None:
        return container_config
    if isinstance(raw_config, list) and len(raw_config) == 1:
        return dokploy_api.as_json_object(raw_config[0])
    return None
