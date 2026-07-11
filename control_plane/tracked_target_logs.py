from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol, cast

import click

from control_plane import dokploy as control_plane_dokploy
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord


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

    normalized_line_count = control_plane_dokploy.normalize_dokploy_log_line_count(line_count)
    normalized_since = control_plane_dokploy.normalize_dokploy_log_since(since)
    normalized_search = control_plane_dokploy.normalize_dokploy_log_search(search)
    normalized_source = normalize_tracked_target_log_source(source)
    if normalized_source == "deployment" and normalized_since != "all":
        raise ValueError("Tracked deployment logs require since='all'.")
    if normalized_source == "deployment" and normalized_search:
        raise ValueError("Tracked deployment logs do not support search.")
    try:
        host, token = control_plane_dokploy.read_dokploy_config(
            control_plane_root=control_plane_root
        )
    except click.ClickException as error:
        raise _provider_error(operation="provider-config", error=error) from error
    try:
        target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
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
            latest_deployment = control_plane_dokploy.latest_deployment_for_target(
                host=host,
                token=token,
                target_type=target_record.target_type,
                target_id=target_id_record.target_id,
            )
        except click.ClickException as error:
            raise _provider_error(operation="deployment-list", error=error) from error
        if latest_deployment is None:
            raise ValueError("No Dokploy deployment is available for the requested target.")
        deployment_id = control_plane_dokploy.deployment_log_id(latest_deployment)
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
        try:
            logs = control_plane_dokploy.fetch_dokploy_deployment_logs(
                host=host,
                token=token,
                deployment_id=deployment_id,
                line_count=normalized_line_count,
            )
        except click.ClickException as error:
            raise _provider_error(operation="deployment-log-read", error=error) from error
        deployment = {"deployment_id": deployment_id}
    elif target_record.target_type == "application":
        try:
            logs = control_plane_dokploy.fetch_dokploy_application_logs(
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
            logs = control_plane_dokploy.fetch_dokploy_compose_logs(
                host=host,
                token=token,
                compose_id=target_id_record.target_id,
                app_name=app_name,
                server_id=server_id,
                line_count=normalized_line_count,
                since=normalized_since,
                search=normalized_search,
            )
        except click.ClickException as error:
            raise _provider_error(operation="runtime-log-read", error=error) from error
    logs = tuple(
        control_plane_dokploy.redact_dokploy_log_line(line)
        for line in logs[-normalized_line_count:]
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


def _provider_error(
    *,
    operation: TrackedTargetLogProviderOperation,
    error: click.ClickException,
) -> TrackedTargetLogsProviderError:
    redacted_detail = control_plane_dokploy.redact_dokploy_log_line(str(error)).strip()
    if not redacted_detail:
        redacted_detail = "Provider request failed."
    return TrackedTargetLogsProviderError(
        operation=operation,
        detail=redacted_detail[:_MAX_PROVIDER_ERROR_DETAIL_LENGTH],
    )
