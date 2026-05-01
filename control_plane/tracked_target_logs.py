from __future__ import annotations

from pathlib import Path
from typing import Protocol

from control_plane import dokploy as control_plane_dokploy
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord


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
    if target_record.target_type != "application":
        raise ValueError(
            "Tracked target logs currently support Dokploy application targets only. "
            f"Configured target_type={target_record.target_type}."
        )

    normalized_line_count = control_plane_dokploy.normalize_dokploy_log_line_count(line_count)
    normalized_since = control_plane_dokploy.normalize_dokploy_log_since(since)
    normalized_search = control_plane_dokploy.normalize_dokploy_log_search(search)
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=target_record.target_type,
        target_id=target_id_record.target_id,
    )
    logs = control_plane_dokploy.fetch_dokploy_application_logs(
        host=host,
        token=token,
        application_id=target_id_record.target_id,
        line_count=normalized_line_count,
        since=normalized_since,
        search=normalized_search,
    )
    app_name = str(target_payload.get("appName") or "").strip()
    server_id = str(target_payload.get("serverId") or "").strip()
    return {
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
