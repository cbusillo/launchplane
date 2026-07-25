from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol, cast

import click

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import source as dokploy_source


TrackedTargetLogSource = Literal["runtime", "deployment"]
TrackedTargetLogProviderOperation = Literal[
    "provider-config",
    "target-inspect",
    "deployment-list",
    "deployment-log-read",
    "runtime-log-read",
]
_MAX_PROVIDER_ERROR_DETAIL_LENGTH = 1000


class TrackedTargetLogsProviderError(RuntimeError):
    def __init__(
        self,
        *,
        operation: TrackedTargetLogProviderOperation,
        detail: str,
    ) -> None:
        self.operation = operation
        self.detail = detail
        super().__init__(f"{operation}: {detail}")


class TrackedTargetLogsStore(Protocol):
    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord: ...


def build_tracked_target_logs_payload(
    *,
    record_store: TrackedTargetLogsStore,
    control_plane_root: Path,
    context_name: str,
    instance_name: str,
    line_count: int,
    since: str = "all",
    search: str = "",
    service: str = "",
    source: str = "runtime",
) -> dict[str, object]:
    normalized_context = context_name.strip().lower()
    normalized_instance = instance_name.strip().lower()
    if not normalized_context or not normalized_instance:
        raise ValueError("Tracked target logs require non-empty context and instance.")
    try:
        target_record = record_store.read_dokploy_target_record(
            context_name=normalized_context,
            instance_name=normalized_instance,
        )
        target_id_record = record_store.read_dokploy_target_id_record(
            context_name=normalized_context,
            instance_name=normalized_instance,
        )
    except FileNotFoundError as error:
        raise ValueError(
            "Missing DB-backed tracked Dokploy target records for requested context/instance."
        ) from error
    if target_record.target_type not in {"application", "compose"}:
        raise ValueError(
            "Tracked target logs currently support Dokploy application and compose targets only. "
            f"Configured target_type={target_record.target_type}."
        )

    normalized_line_count = dokploy_api.normalize_dokploy_log_line_count(line_count)
    normalized_since = dokploy_api.normalize_dokploy_log_since(since)
    normalized_search = dokploy_api.normalize_dokploy_log_search(search)
    normalized_service = dokploy_api.normalize_dokploy_compose_service_name(service)
    normalized_source = normalize_tracked_target_log_source(source)
    if normalized_source == "deployment" and normalized_since != "all":
        raise ValueError("Tracked deployment logs require since='all'.")
    if normalized_source == "deployment" and normalized_search:
        raise ValueError("Tracked deployment logs do not support search.")
    if normalized_source == "deployment" and normalized_service:
        raise ValueError("Tracked deployment logs do not support compose service selection.")
    if target_record.target_type == "application" and normalized_service:
        raise ValueError("Tracked application logs do not support compose service selection.")
    try:
        host, token = dokploy_source.read_dokploy_config(control_plane_root=control_plane_root)
    except click.ClickException as error:
        raise _provider_error(operation="provider-config", error=error) from error
    try:
        target_payload = dokploy_api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type=target_record.target_type,
            target_id=target_id_record.target_id,
        )
    except click.ClickException as error:
        raise _provider_error(operation="target-inspect", error=error) from error
    app_name = str(target_payload.get("appName") or "").strip()
    server_id = str(target_payload.get("serverId") or "").strip()
    deployment: dict[str, object] | None = None
    if normalized_source == "deployment":
        try:
            latest_deployment = dokploy_api.latest_deployment_for_target(
                host=host,
                token=token,
                target_type=target_record.target_type,
                target_id=target_id_record.target_id,
            )
        except click.ClickException as error:
            raise _provider_error(operation="deployment-list", error=error) from error
        if latest_deployment is None:
            raise ValueError("No Dokploy deployment is available for the requested target.")
        deployment_id = dokploy_api.deployment_key(latest_deployment)
        if not deployment_id:
            raise ValueError("No Dokploy deployment is available for the requested target.")
        deployment_target_key = (
            "applicationId" if target_record.target_type == "application" else "composeId"
        )
        deployment_target_id = str(latest_deployment.get(deployment_target_key) or "").strip()
        if deployment_target_id != target_id_record.target_id:
            raise ValueError(
                "Latest Dokploy deployment is not bound to the requested tracked target."
            )
        deployment = _deployment_metadata(
            deployment=latest_deployment,
            deployment_id=deployment_id,
        )
        try:
            logs = dokploy_api.fetch_dokploy_deployment_logs(
                host=host,
                token=token,
                deployment_id=deployment_id,
                line_count=normalized_line_count,
            )
        except click.ClickException as error:
            raise _provider_error(operation="deployment-log-read", error=error) from error
    elif target_record.target_type == "application":
        try:
            logs = dokploy_api.fetch_dokploy_application_logs(
                host=host,
                token=token,
                application_id=target_id_record.target_id,
                line_count=normalized_line_count,
                since=normalized_since,
                search=normalized_search,
            )
        except click.ClickException as error:
            raise _provider_error(operation="runtime-log-read", error=error) from error
    else:
        try:
            logs = dokploy_api.fetch_dokploy_compose_logs(
                host=host,
                token=token,
                compose_id=target_id_record.target_id,
                app_name=app_name,
                server_id=server_id,
                service_name=normalized_service,
                line_count=normalized_line_count,
                since=normalized_since,
                search=normalized_search,
            )
        except click.ClickException as error:
            raise _provider_error(operation="runtime-log-read", error=error) from error
    logs = tuple(
        dokploy_api.redact_dokploy_log_line(line) for line in logs[-normalized_line_count:]
    )
    result: dict[str, object] = {
        "context": normalized_context,
        "instance": normalized_instance,
        "target": {
            "target_id": target_id_record.target_id,
            "target_type": target_record.target_type,
            "target_name": target_record.target_name,
            "app_name": app_name,
            "server_id": server_id,
            "source_label": target_record.source_label,
        },
        "request": {
            "source": normalized_source,
            "line_count": normalized_line_count,
            "since": normalized_since,
            "search": normalized_search,
            "service": normalized_service,
        },
        "logs": {
            "line_count": len(logs),
            "lines": list(logs),
            "redacted": True,
        },
    }
    if deployment is not None:
        result["deployment"] = deployment
    return result


def normalize_tracked_target_log_source(value: str) -> TrackedTargetLogSource:
    normalized_value = value.strip().lower()
    if normalized_value not in {"runtime", "deployment"}:
        raise ValueError("Tracked target log source must be 'runtime' or 'deployment'.")
    return cast(TrackedTargetLogSource, normalized_value)


def _deployment_metadata(
    *,
    deployment: dict[str, dokploy_api.JsonValue],
    deployment_id: str,
) -> dict[str, object]:
    return {
        "deployment_id": deployment_id,
        "status": _deployment_text(deployment, "status", "state", "deploymentStatus").lower(),
        "error_message": _redacted_text(
            _deployment_text(deployment, "errorMessage", "error_message")
        ),
        "created_at": _deployment_text(deployment, "createdAt", "created_at"),
        "started_at": _deployment_text(deployment, "startedAt", "started_at"),
        "finished_at": _deployment_text(deployment, "finishedAt", "finished_at"),
        "log_path_present": bool(_deployment_text(deployment, "logPath", "log_path")),
    }


def _deployment_text(
    deployment: dict[str, dokploy_api.JsonValue],
    *key_names: str,
) -> str:
    for key_name in key_names:
        value = deployment.get(key_name)
        if value is not None:
            return str(value).strip()
    return ""


def _provider_error(
    *,
    operation: TrackedTargetLogProviderOperation,
    error: click.ClickException,
) -> TrackedTargetLogsProviderError:
    redacted_detail = _redacted_text(str(error)) or "Provider request failed."
    return TrackedTargetLogsProviderError(
        operation=operation,
        detail=redacted_detail,
    )


def _redacted_text(value: str) -> str:
    return dokploy_api.redact_dokploy_log_line(value).strip()[:_MAX_PROVIDER_ERROR_DETAIL_LENGTH]
