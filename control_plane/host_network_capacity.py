"""Read the authorized lane's current host; no fleet enumeration or mutation."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256
from control_plane.contracts.deploy_target import ProviderTargetRecord

import click

from control_plane.contracts.host_network_capacity import (
    HOST_BRIDGE_NETWORK_FAILURE_MESSAGES,
    HostNetworkCapacityObservation,
)
from control_plane.dokploy import api, source, network_capacity
from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.ship import utc_now_timestamp


def read_lane_host_network_capacity(
    *, root: Path, store: PostgresRecordStore, context: str, instance: str
) -> HostNetworkCapacityObservation:
    server_id = ""
    required = 1
    margin = network_capacity.BRIDGE_NETWORK_CAPACITY_MARGIN

    def unavailable(*, denied: bool = False) -> HostNetworkCapacityObservation:
        return HostNetworkCapacityObservation(
            server_id=server_id,
            observed_at=utc_now_timestamp(),
            status="denied" if denied else "unavailable",
            required_networks=required,
            safety_margin=margin,
            reason="The provider denied the capacity read."
            if denied
            else HOST_BRIDGE_NETWORK_FAILURE_MESSAGES["host_bridge_network_capacity_unavailable"],
        )

    def observe(host: str, token: str, server: str) -> network_capacity.BridgeNetworkCapacity:
        nonlocal server_id
        server_id = server
        return network_capacity.read_bridge_network_capacity(
            host=host, token=token, server_id=server
        )

    try:
        bound = read_bound_lane_host(
            root=root,
            store=store,
            context=context,
            instance=instance,
            read=observe,
        )
        server_id, observation = bound.server_id, bound.observation
        short = observation.free_slots < required + margin
        return HostNetworkCapacityObservation(
            server_id=server_id,
            observed_at=utc_now_timestamp(),
            status="short" if short else "available",
            capacity=observation,
            required_networks=required,
            safety_margin=margin,
            reason=HOST_BRIDGE_NETWORK_FAILURE_MESSAGES["host_bridge_network_capacity_short"]
            if short
            else "",
        )
    except NativeSourceAuthorizationDenied:
        return unavailable(denied=True)
    except api.DokployRequestFailed as error:
        return unavailable(denied=error.status_code in {401, 403})
    except (ValueError, FileNotFoundError, OSError, click.ClickException):
        return unavailable()


@dataclass(frozen=True)
class BoundHostObservation[T]:
    server_id: str
    target_sha256: str
    observation: T


def read_bound_lane_host[T](
    *,
    root: Path,
    store: PostgresRecordStore,
    context: str,
    instance: str,
    read: Callable[[str, str, str], T],
) -> BoundHostObservation[T]:
    target = store.read_provider_target_record(context_name=context, instance_name=instance)
    if target.provider_id != "dokploy" or target.target_category not in {"compose", "application"}:
        raise ValueError("The lane has no supported provider host.")
    host, token = source.read_dokploy_config(
        control_plane_root=root, database_url=store.database_url
    )
    payload = api.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=target.target_category,
        target_id=target.target_id,
    )
    server_id = str(payload.get("serverId") or "").strip()
    observation = read(host, token, server_id)
    current = store.read_provider_target_record(context_name=context, instance_name=instance)
    fresh = api.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=target.target_category,
        target_id=target.target_id,
    )
    if current != target or fresh.get("serverId") != server_id:
        raise ValueError("The lane host changed during observation.")
    return BoundHostObservation(
        server_id=server_id,
        target_sha256=host_target_sha256(target),
        observation=observation,
    )


def host_target_sha256(target: ProviderTargetRecord) -> str:
    return nonvalidating_compact_sorted_json_sha256(
        target.model_dump(include={"provider_id", "target_category", "target_id"})
    )
