import os
import errno
import fcntl
import struct
import socket
import sys
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
import unittest

from sqlalchemy.engine import make_url

from control_plane.database_maintenance_cli import await_command, require_isolated_runtime
from control_plane import database_maintenance_cli
from control_plane.deployment_maintenance import MAINTENANCE_UNTIL_ENV, recovery_marker
from control_plane.runtime_platform_credentials import PLATFORM_CREDENTIAL_KEYS
from control_plane.database_maintenance import require_no_other_clients
from control_plane.database_maintenance_host import dump_application_name
from control_plane.database_maintenance_host import RecoveryRequired
from control_plane.database_maintenance_host import (
    DATABASE_UNSAFE_EVENT,
    DEPLOYMENT_RECOVERY_REQUIRED_EVENT,
)
from control_plane import database_maintenance
from sqlalchemy.exc import OperationalError
from tempfile import TemporaryDirectory
import io


class DatabaseMaintenanceIsolationTests(unittest.TestCase):
    def test_cli_emits_terminal_database_and_deployment_results_for_the_host(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "commit-pending.json").write_text(json.dumps({"hold_id": "own"}))
            base = [
                "keeper",
                "verify",
                "--expires-at",
                (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                "--hold-id",
                "own",
                "--job-directory",
                str(root),
            ]
            for mode, event in (
                ("verify", DATABASE_UNSAFE_EVENT),
                ("unfence", DEPLOYMENT_RECOVERY_REQUIRED_EVENT),
            ):
                output = io.StringIO()
                arguments = list(base)
                arguments[1] = mode
                with (
                    patch.dict(
                        os.environ,
                        {"LAUNCHPLANE_DATABASE_URL": "postgresql://synthetic"},
                        clear=True,
                    ),
                    patch.object(sys, "argv", arguments),
                    patch.object(
                        database_maintenance_cli, "_build_engine", return_value=MagicMock()
                    ),
                    patch.object(
                        database_maintenance_cli,
                        "verify_committed_database",
                        side_effect=RecoveryRequired("actual mismatch"),
                    ),
                    patch.object(
                        database_maintenance_cli,
                        "release_deployment",
                        side_effect=RecoveryRequired("original marker unresolved"),
                    ),
                    patch.object(sys, "stdout", output),
                ):
                    self.assertEqual(database_maintenance_cli.main(), 0)
                self.assertEqual(json.loads(output.getvalue())["event"], event)

    def test_transient_wrapped_invariant_read_is_retryable_but_mismatch_is_terminal(self) -> None:
        connection = Mock()
        proof = {"system_identifier": "own", "before": {}, "after": {}}
        transient = RuntimeError("schema read unavailable")
        transient.__cause__ = OperationalError("read", None, RuntimeError("connection reset"))
        with (
            patch.object(database_maintenance, "database_system_identifier", return_value="own"),
            patch.object(database_maintenance, "require_no_other_clients"),
            patch.object(database_maintenance, "database_snapshot", return_value={}),
        ):
            for error in (transient, RuntimeError("actual invariant mismatch")):
                with patch.object(
                    database_maintenance, "verify_postgres_schema_invariants", side_effect=error
                ):
                    try:
                        database_maintenance.verify_committed_database(connection, proof)
                    except RecoveryRequired:
                        self.assertIsNot(error, transient)
                    except RuntimeError:
                        self.assertIs(error, transient)
                    else:
                        self.fail("A failed read or invariant must not report verified state")

    def test_only_the_scoped_local_dump_backend_keeps_its_exact_identity(self) -> None:
        connection = Mock()
        connection.scalar.return_value = 0
        client = {
            "pid": 123,
            "backend_start": datetime.now(timezone.utc),
            "application_name": "pg_dump",
            "backend_xid": None,
            "local": True,
            "own_role": True,
        }
        connection.execute.return_value.mappings.return_value.all.return_value = [client]
        dump = {"name": dump_application_name("own")}
        with self.assertRaises(RuntimeError):
            require_no_other_clients(connection, dump_client=dump)
        client["application_name"] = dump["name"]
        require_no_other_clients(connection, dump_client=dump)
        client["pid"] = 124
        with self.assertRaises(RuntimeError):
            require_no_other_clients(connection, dump_client=dump)
        client["pid"] = 123
        for field, wrong in (("local", False), ("own_role", False), ("backend_xid", 1)):
            original = client[field]
            client[field] = wrong
            with self.assertRaises(RuntimeError):
                require_no_other_clients(connection, dump_client=dump)
            client[field] = original
        require_no_other_clients(connection, dump_client=dump)

    def test_pending_commit_marker_cannot_be_removed_before_all_recovery_readbacks(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "commit-pending.json").write_text("{}")
            deployment = Mock()
            deployment.read_target.return_value = {
                "env": f"{MAINTENANCE_UNTIL_ENV}={recovery_marker('own')}"
            }
            arguments = [
                "keeper",
                "unfence",
                "--expires-at",
                (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
                "--hold-id",
                "own",
                "--target-id",
                "target",
                "--job-directory",
                str(root),
            ]
            with (
                patch.dict(
                    os.environ, {"LAUNCHPLANE_DATABASE_URL": "postgresql://synthetic"}, clear=True
                ),
                patch.object(
                    database_maintenance_cli, "DeploymentMaintenance", return_value=deployment
                ),
                patch.object(sys, "argv", arguments),
            ):
                with self.assertRaises(RuntimeError):
                    database_maintenance_cli.main()
                (root / "database-safe.json").write_text("{}")
                with self.assertRaises(RuntimeError):
                    database_maintenance_cli.main()
                deployment.marker.assert_not_called()
                (root / "resumed").touch()
                self.assertEqual(database_maintenance_cli.main(), 0)
            deployment.marker.assert_called_once_with(recovery_marker("own"), remove=True)

    def test_host_release_wins_over_a_ready_destructive_command(self) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)

            def observe() -> None:
                (directory / "releasing").touch()

            with (
                patch(
                    "control_plane.database_maintenance_cli.select.select",
                    return_value=([True], [], []),
                ),
                patch(
                    "control_plane.database_maintenance_cli.sys.stdin",
                    io.StringIO('{"action":"apply"}\n'),
                ),
            ):
                with self.assertRaises(InterruptedError):
                    await_command(
                        datetime.now(timezone.utc) + timedelta(seconds=10), observe, directory
                    )

    def test_rehearsal_requires_loopback_and_an_empty_production_credential_environment(
        self,
    ) -> None:
        connection = Mock()
        connection.engine.url = make_url("postgresql+psycopg://postgres@127.0.0.1/restore")
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("control_plane.database_maintenance_cli.sys.platform", "linux"),
            patch.object(database_maintenance_cli, "require_isolated_network"),
        ):
            require_isolated_runtime(connection)
        with patch.dict(
            os.environ,
            {next(iter(PLATFORM_CREDENTIAL_KEYS)): "synthetic-production-credential"},
            clear=True,
        ):
            with self.assertRaises(RuntimeError):
                require_isolated_runtime(connection)
        connection.engine.url = make_url("postgresql+psycopg://postgres@database.invalid/restore")
        with self.assertRaises(RuntimeError):
            require_isolated_runtime(connection)

    def test_down_unconfigured_tunnels_are_isolated_but_routes_addresses_and_capabilities_refuse(
        self,
    ) -> None:
        views = {
            "/proc/self/status": "CapInh: 0\nCapPrm: 0\nCapEff: 0\nCapBnd: 0\nCapAmb: 0\nNoNewPrivs: 1\n",
            "/proc/self/net/route": "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n",
            "/proc/self/net/ipv6_route": "0 0 0 0 0 0 0 0 0 local\n",
            "/proc/self/net/if_inet6": "00000000000000000000000000000001 01 80 10 80 local\n",
        }
        external_up = False
        external_address = False

        def ioctl(_descriptor: int, command: int, request: bytes) -> bytes:
            local = request.split(b"\0", 1)[0] == b"local"
            if command == database_maintenance_cli.SIOCGIFFLAGS:
                flags = (
                    database_maintenance_cli.IFF_UP | database_maintenance_cli.IFF_LOOPBACK
                    if local
                    else database_maintenance_cli.IFF_UP
                    if external_up
                    else 0
                )
                return request[:16] + struct.pack("H", flags) + bytes(238)
            if not local and not external_address:
                raise OSError(errno.EADDRNOTAVAIL, "no address")
            return (
                request[:20] + socket.inet_aton("127.0.0.1" if local else "192.0.2.1") + bytes(232)
            )

        with (
            patch.object(
                socket, "if_nameindex", return_value=[(1, "local"), (2, "dormant-tunnel")]
            ),
            patch.object(fcntl, "ioctl", side_effect=ioctl),
            patch.object(Path, "read_text", lambda path, **_values: views[str(path)]),
        ):
            database_maintenance_cli.require_isolated_network()
            external_up = True
            with self.assertRaises(RuntimeError):
                database_maintenance_cli.require_isolated_network()
            external_up = False
            external_address = True
            with self.assertRaises(RuntimeError):
                database_maintenance_cli.require_isolated_network()
            external_address = False
            for path, bad in (
                ("/proc/self/status", views["/proc/self/status"].replace("CapBnd: 0", "CapBnd: 1")),
                (
                    "/proc/self/status",
                    views["/proc/self/status"].replace("NoNewPrivs: 1", "NoNewPrivs: 0"),
                ),
                (
                    "/proc/self/net/route",
                    views["/proc/self/net/route"] + "dormant-tunnel 0 0 0 0 0 0 0 0 0 0\n",
                ),
                ("/proc/self/net/ipv6_route", "0 0 0 0 0 0 0 0 0 dormant-tunnel\n"),
                (
                    "/proc/self/net/if_inet6",
                    "20010db8000000000000000000000001 02 40 00 80 dormant-tunnel\n",
                ),
            ):
                original = views[path]
                views[path] = bad
                with self.assertRaises(RuntimeError):
                    database_maintenance_cli.require_isolated_network()
                views[path] = original

    def test_rehearsal_refuses_a_networked_namespace(self) -> None:
        connection = Mock()
        connection.engine.url = make_url("postgresql+psycopg://postgres@127.0.0.1/restore")
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("control_plane.database_maintenance_cli.sys.platform", "linux"),
            patch.object(
                database_maintenance_cli,
                "require_isolated_network",
                side_effect=RuntimeError("external network"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                require_isolated_runtime(connection)
