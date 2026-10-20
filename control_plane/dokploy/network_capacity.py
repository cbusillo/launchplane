"""Read bridge IPAM headroom through the existing registered-server transport."""

from collections.abc import Mapping, Sequence
import inspect
from typing import Literal

from control_plane.contracts.host_network_capacity import (
    BridgeNetworkCapacity as BridgeNetworkCapacity,
    HOST_BRIDGE_NETWORK_FAILURE_MESSAGES,
)
from control_plane.dokploy import native_copy_stream


BRIDGE_NETWORK_CAPACITY_MARGIN = 2


class BridgeNetworkCapacityRefused(ValueError):
    def __init__(
        self,
        code: Literal[
            "host_bridge_network_capacity_short", "host_bridge_network_capacity_unavailable"
        ],
    ) -> None:
        self.code = code
        super().__init__(HOST_BRIDGE_NETWORK_FAILURE_MESSAGES[code])


def calculate_capacity(
    pools: Sequence[Mapping[str, object]], occupied: Sequence[str]
) -> dict[str, int]:
    """Count unavailable subnet slots by union, without enumerating a large pool.

    This function also runs in the fixed host program. All input comes from
    daemon info, complete network inspection and the host's IPv4 route tables.
    """
    from ipaddress import IPv4Network, ip_network

    if not pools or len(pools) > 256 or len(occupied) > 100000:
        raise ValueError("Incomplete or excessive capacity inventory.")
    parsed: list[tuple[IPv4Network, int]] = []
    for pool in pools:
        base, size = pool.get("Base"), pool.get("Size")
        if not isinstance(base, str) or type(size) is not int:
            raise ValueError("Unverified daemon address pool.")
        network = ip_network(base)
        if not isinstance(network, IPv4Network) or not network.prefixlen <= size <= 32:
            raise ValueError("Unsupported daemon address pool.")
        if any(network.overlaps(other) for other, _ in parsed):
            raise ValueError("Overlapping daemon address pools.")
        parsed.append((network, size))
    excluded = []
    for subnet in occupied:
        network = ip_network(subnet)
        if isinstance(network, IPv4Network):
            excluded.append(network)
    total = unavailable = 0
    for network, size in parsed:
        start = int(network.network_address)
        end = int(network.broadcast_address)
        width = 1 << (32 - size)
        total += 1 << (size - network.prefixlen)
        intervals = []
        for exclusion in excluded:
            low = max(start, int(exclusion.network_address))
            high = min(end, int(exclusion.broadcast_address))
            if low <= high:
                intervals.append(((low - start) // width, (high - start) // width))
        previous_end = -1
        for low, high in sorted(intervals):
            unavailable += max(0, high - max(previous_end, low - 1))
            previous_end = max(previous_end, high)
    return {"total_slots": total, "free_slots": total - unavailable}


# Only fixed metadata reads. No schedules, containers, networks or files are created.
BRIDGE_CAPACITY_PROGRAM = (
    "from collections.abc import Mapping, Sequence\n"
    + inspect.getsource(calculate_capacity)
    + r"""
import json, subprocess, sys, time

deadline = time.monotonic() + 45

def checked(arguments):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError('Capacity read exceeded its budget')
    result = subprocess.run(arguments, capture_output=True, check=False, timeout=min(10, remaining))
    if result.returncode:
        if any(value in result.stderr.lower() for value in
               (b'permission denied', b'authentication failed', b'authorization denied')):
            raise PermissionError('Existing provider reader refused the read')
        raise ValueError('Capacity read unavailable')
    if len(result.stdout) > 8 * 1024 * 1024:
        raise ValueError('Capacity read exceeds bound')
    return result.stdout

def identifiers():
    values = checked(['docker', 'network', 'ls', '--no-trunc', '--quiet']).decode().splitlines()
    if not values or len(values) > 10000 or len(set(values)) != len(values):
        raise ValueError('Incomplete network inventory')
    if any(len(value) != 64 or any(c not in '0123456789abcdef' for c in value) for value in values):
        raise ValueError('Invalid network identity')
    return sorted(values)

def capacity():
    pools = json.loads(checked(['docker', 'info', '--format', '{{json .DefaultAddressPools}}']))
    if not isinstance(pools, list) or not all(isinstance(pool, dict) for pool in pools):
        raise ValueError('Daemon pools are unavailable')
    ids = identifiers()
    occupied = []
    for index in range(0, len(ids), 32):
        selected = ids[index:index + 32]
        networks = json.loads(checked(['docker', 'network', 'inspect', *selected]))
        if not isinstance(networks, list) or len(networks) != len(selected):
            raise ValueError('Incomplete network inspections')
        if sorted(network['Id'] for network in networks) != selected:
            raise ValueError('Network identities changed')
        for network in networks:
            config = network['IPAM']['Config']
            if network['Driver'] in {'host', 'null'} and config in (None, []):
                continue
            if not isinstance(config, list) or not config:
                raise ValueError('Unknown network IPAM')
            for item in config:
                subnet = item['Subnet']
                if not isinstance(subnet, str) or not subnet:
                    raise ValueError('Unknown network subnet')
                occupied.append(subnet)
    routes = json.loads(checked(['ip', '-j', '-4', 'route', 'show', 'table', 'all']))
    if not isinstance(routes, list) or not routes:
        raise ValueError('Host routes are unavailable')
    for route in routes:
        destination = route['dst']
        if not isinstance(destination, str):
            raise ValueError('Invalid host route')
        if destination not in {'default', '0.0.0.0/0'}:
            # iproute2 prints host routes without their /32 mask.
            occupied.append(destination if '/' in destination else destination + '/32')
    result = calculate_capacity(pools, occupied)
    if identifiers() != ids:
        raise ValueError('Network inventory changed during observation')
    return result

try:
    frame = {'type': 'complete', 'result': capacity()}
except PermissionError:
    frame = {'type': 'denied'}
except Exception:
    frame = {'type': 'failed'}
print(sys.argv[2] + json.dumps(frame, sort_keys=True), flush=True)
"""
)


def read_bridge_network_capacity(*, host: str, token: str, server_id: str) -> BridgeNetworkCapacity:
    frames: list[dict[str, object]] = []
    try:
        native_copy_stream._read_remote(
            host=host,
            token=token,
            server_id=server_id,
            program=BRIDGE_CAPACITY_PROGRAM,
            payload={},
            receive=frames.append,
            timeout_seconds=60,
        )
        if len(frames) != 1 or frames[0].get("type") != "complete":
            raise ValueError("Incomplete capacity observation.")
        return BridgeNetworkCapacity.model_validate(frames[0].get("result"))
    except (ValueError, native_copy_stream.NativeSourceUnavailable):
        raise BridgeNetworkCapacityRefused("host_bridge_network_capacity_unavailable") from None


def require_bridge_network_capacity(
    *, host: str, token: str, server_id: str, required_networks: int
) -> BridgeNetworkCapacity:
    if type(required_networks) is not int or required_networks < 1:
        raise ValueError("A positive bridge-network requirement is required.")
    observation = read_bridge_network_capacity(host=host, token=token, server_id=server_id)
    if observation.free_slots < required_networks + BRIDGE_NETWORK_CAPACITY_MARGIN:
        raise BridgeNetworkCapacityRefused("host_bridge_network_capacity_short")
    return observation
