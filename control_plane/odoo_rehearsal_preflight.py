"""Read source sizes and allocation backing without preparing a rehearsal."""

from pathlib import Path

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.odoo_upgrade_rehearsal import (
    RehearsalPreflightObservation,
    RehearsalPreflightResult,
)
from control_plane.dokploy import api, source
from control_plane.dokploy.native_copy_stream import (
    read_odoo_rehearsal_preflight,
    require_registered_remote_server,
)
from control_plane.odoo_upgrade_rehearsal import _source_values
from control_plane.storage.postgres import PostgresRecordStore


def rehearsal_preflight(
    root: Path, store: PostgresRecordStore, product: str, source_context: str
) -> RehearsalPreflightResult:
    values, _ = _source_values(store, product, source_context)
    reader = store.read_dokploy_target_id_record(
        context_name=source_context, instance_name="testing"
    )
    host, token = source.read_dokploy_config(control_plane_root=root)
    placement = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=reader.target_id
    )
    server = require_registered_remote_server(placement.get("serverId"))
    frames: list[dict[str, object]] = []
    read_odoo_rehearsal_preflight(
        host=host, token=token, server_id=server, source=values, receive=frames.append
    )
    if len(frames) != 1 or frames[0].get("type") != "complete":
        raise ValueError("Preflight has no unique completed observation.")
    observation = RehearsalPreflightObservation.model_validate(frames[0].get("result"))
    return RehearsalPreflightResult(
        **observation.model_dump(),
        product=product,
        source_context=source_context,
        reader_server_id=server,
        source_binding_sha256=canonical_json_sha256(values),
    )
