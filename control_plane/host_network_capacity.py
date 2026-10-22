"""Read the authorized lane's current host; no fleet enumeration or mutation."""

from pathlib import Path

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

    try:
        target = store.read_provider_target_record(context_name=context, instance_name=instance)
        if target.provider_id != "dokploy" or target.target_category not in {
            "compose",
            "application",
        }:
            return unavailable()
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
        observation = network_capacity.read_bridge_network_capacity(
            host=host,
            token=token,
            server_id=server_id,
        )
        # A target can move during a long host read. Never display the previous
        # host's observation as the capacity of its new placement.
        current = store.read_provider_target_record(context_name=context, instance_name=instance)
        fresh = api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type=target.target_category,
            target_id=target.target_id,
        )
        if current != target or fresh.get("serverId") != server_id:
            return unavailable()
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
