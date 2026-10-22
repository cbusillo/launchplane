"""Pool geometry, complete provider reads, and fail-closed admission."""

from contextlib import redirect_stdout
import io
import json
import subprocess
import sys
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.dokploy import native_copy_stream
from control_plane.dokploy.network_capacity import (
    BRIDGE_CAPACITY_PROGRAM,
    BRIDGE_NETWORK_CAPACITY_MARGIN,
    BridgeNetworkCapacityRefused,
    calculate_capacity,
    compose_bridge_network_requirement,
    read_bridge_network_capacity,
    require_bridge_network_capacity,
)


class BridgeCapacityTests(unittest.TestCase):
    def test_known_compose_needs_default_and_each_declared_private_bridge(self) -> None:
        self.assertEqual(compose_bridge_network_requirement({}), 1)
        self.assertEqual(compose_bridge_network_requirement({"networks": {"default": {}}}), 1)
        self.assertEqual(
            compose_bridge_network_requirement(
                {"networks": {"data": {}, "private": {"internal": True}}}
            ),
            3,
        )

    def test_capacity_counts_subnets_and_route_overlap_once(self) -> None:
        pools: list[dict[str, object]] = [{"Base": "172.22.0.0/22", "Size": 24}]
        observation = calculate_capacity(
            pools, ["172.22.0.0/24", "172.22.0.0/25", "172.22.1.128/25", "10.0.0.0/8"]
        )
        self.assertEqual(observation, {"total_slots": 4, "free_slots": 2})
        self.assertEqual(calculate_capacity(pools, ["172.22.0.0/16"])["free_slots"], 0)
        # Old, larger networks consume several new pool slots after a pool change.
        self.assertEqual(calculate_capacity(pools, ["172.22.0.0/23"])["free_slots"], 2)
        self.assertEqual(calculate_capacity(pools, ["::/0"])["free_slots"], 4)

    def test_missing_or_ambiguous_daemon_pools_are_unknown(self) -> None:
        for pools in (
            [],
            [{"Base": "172.22.0.0/16", "Size": True}],
            [{"Base": "172.22.0.0/16", "Size": 8}],
            [{"Base": "172.22.0.0/16"}],
            [{"Base": "172.22.0.0/16", "Size": 24}] * 2,
        ):
            with self.subTest(pools=pools), self.assertRaises(ValueError):
                calculate_capacity(pools, [])

    def test_need_plus_margin_is_admitted_exactly_at_boundary(self) -> None:
        required = 3
        for free in (required - 1, required, required + BRIDGE_NETWORK_CAPACITY_MARGIN - 1):
            with (
                self.subTest(free=free),
                patch.object(
                    native_copy_stream,
                    "_read_remote",
                    side_effect=lambda **kwargs: kwargs["receive"](
                        {"type": "complete", "result": {"total_slots": 16, "free_slots": free}}
                    ),
                ),
            ):
                with self.assertRaises(BridgeNetworkCapacityRefused) as refused:
                    require_bridge_network_capacity(
                        host="https://provider.invalid",
                        token="fixture",
                        server_id="host-a",
                        required_networks=required,
                    )
                self.assertEqual(refused.exception.code, "host_bridge_network_capacity_short")
        free = required + BRIDGE_NETWORK_CAPACITY_MARGIN
        with patch.object(native_copy_stream, "_read_remote") as read:
            read.side_effect = lambda **kwargs: kwargs["receive"](
                {"type": "complete", "result": {"total_slots": 16, "free_slots": free}}
            )
            result = require_bridge_network_capacity(
                host="https://provider.invalid",
                token="fixture",
                server_id="host-a",
                required_networks=required,
            )
        self.assertEqual(result.free_slots, free)
        self.assertEqual(read.call_args.kwargs["server_id"], "host-a")
        self.assertEqual(read.call_args.kwargs["payload"], {})

    def test_incomplete_observation_is_unknown_and_denial_keeps_its_identity(self) -> None:
        frames: tuple[list[dict[str, object]], ...] = (
            [],
            [{"type": "complete", "result": {"total_slots": 2, "free_slots": 3}}],
            [{"type": "complete", "result": {"total_slots": 3, "free_slots": True}}],
            [{"type": "complete", "result": {"total_slots": 3}}],
            [{"type": "complete", "result": {"total_slots": 3, "free_slots": 3}}] * 2,
        )
        for observation in frames:
            with (
                self.subTest(observation=observation),
                patch.object(
                    native_copy_stream,
                    "_read_remote",
                    side_effect=lambda **kwargs: [
                        kwargs["receive"](frame) for frame in observation
                    ],
                ),
                self.assertRaises(BridgeNetworkCapacityRefused) as refused,
            ):
                read_bridge_network_capacity(
                    host="https://provider.invalid", token="fixture", server_id="host-a"
                )
            self.assertEqual(refused.exception.code, "host_bridge_network_capacity_unavailable")
        denial = native_copy_stream.NativeSourceAuthorizationDenied("fixture refusal")
        with patch.object(native_copy_stream, "_read_remote", side_effect=denial):
            with self.assertRaises(
                native_copy_stream.NativeSourceAuthorizationDenied
            ) as refused_denial:
                read_bridge_network_capacity(
                    host="https://provider.invalid", token="fixture", server_id="host-a"
                )
        self.assertIs(refused_denial.exception, denial)


class CapacityHostProgramTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identifier = "a" * 64
        self.identifiers = [self.identifier]
        self.pool: object = [{"Base": "172.22.0.0/22", "Size": 24}]
        self.networks: object = [
            {
                "Id": self.identifier,
                "Driver": "bridge",
                "IPAM": {"Config": [{"Subnet": "172.22.0.0/24"}]},
            }
        ]
        self.routes: object = [{"dst": "default"}, {"dst": "172.22.1.0/24"}, {"dst": "172.22.1.10"}]
        self.list_calls = 0
        self.changed = False

    def run_program(
        self, *, denial: bytes = b"", denial_command: str = "docker", spawn_failure: str = ""
    ) -> dict[str, Any]:
        def provider(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            if spawn_failure == arguments[0]:
                raise PermissionError("executable permission failure")
            if denial and arguments[0] == denial_command:
                return subprocess.CompletedProcess(arguments, 1, stdout=b"", stderr=denial)
            if arguments[:2] == ["docker", "info"]:
                value = self.pool
            elif arguments[:3] == ["docker", "network", "inspect"]:
                value = self.networks
            elif arguments[0] == "ip" and "route" in arguments:
                value = self.routes
            elif arguments[:3] == ["docker", "network", "ls"]:
                self.list_calls += 1
                identifiers = (
                    ["b" * 64] if self.changed and self.list_calls > 1 else self.identifiers
                )
                return subprocess.CompletedProcess(
                    arguments, 0, stdout=("\n".join(identifiers) + "\n").encode(), stderr=b""
                )
            else:
                raise AssertionError("The capacity probe dispatched a non-read operation")
            return subprocess.CompletedProcess(
                arguments, 0, stdout=json.dumps(value).encode(), stderr=b""
            )

        output = io.StringIO()
        with (
            patch("subprocess.run", side_effect=provider),
            patch.object(sys, "argv", ["capacity", "unused", "FRAME:"]),
            redirect_stdout(output),
        ):
            exec(compile(BRIDGE_CAPACITY_PROGRAM, "<capacity-read>", "exec"), {})
        result = json.loads(output.getvalue().removeprefix("FRAME:"))
        assert isinstance(result, dict)
        return result

    def test_fixed_program_counts_real_read_shapes_without_emitting_topology(self) -> None:
        assert isinstance(self.networks, list)
        overlay = "pqrx7y2zn0tvf8b3cd5j6lm9s"
        self.identifiers.append(overlay)
        self.networks.append(
            {
                "Id": overlay,
                "Driver": "overlay",
                "IPAM": {"Config": [{"Subnet": "10.0.0.0/24"}, {"Subnet": "fd00::/64"}]},
            }
        )
        self.assertEqual(
            self.run_program(), {"type": "complete", "result": {"total_slots": 4, "free_slots": 2}}
        )
        # Host/none networks reserve no IPAM subnet, including Docker's null form.
        self.identifiers = [self.identifier]
        for driver in ("host", "null"):
            self.networks = [{"Id": self.identifier, "Driver": driver, "IPAM": {"Config": None}}]
            self.assertEqual(
                self.run_program(),
                {"type": "complete", "result": {"total_slots": 4, "free_slots": 3}},
            )

    def test_incomplete_reads_and_inventory_churn_never_report_capacity(self) -> None:
        for field, value in (("pool", None), ("networks", []), ("routes", []), ("changed", True)):
            with self.subTest(field=field), patch.object(self, field, value):
                self.list_calls = 0
                self.assertEqual(self.run_program(), {"type": "failed"})
        self.assertEqual(
            self.run_program(denial=b"Error response from daemon: unauthorized"), {"type": "denied"}
        )
        self.assertEqual(
            self.run_program(denial=b"Error response from daemon: access denied"),
            {"type": "denied"},
        )
        self.assertEqual(
            self.run_program(denial=b"permission denied: unrelated filesystem"), {"type": "failed"}
        )
        self.assertEqual(
            self.run_program(denial=b"permission denied", denial_command="ip"), {"type": "failed"}
        )

    def test_executable_permission_failure_is_unavailable_without_a_daemon_denial(self) -> None:
        for binary in ("docker", "ip"):
            with self.subTest(binary=binary):
                self.assertEqual(self.run_program(spawn_failure=binary), {"type": "failed"})
