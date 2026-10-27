import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

from sqlalchemy.engine import make_url

from control_plane.database_maintenance_cli import await_command, require_isolated_runtime
from tempfile import TemporaryDirectory
import io


class DatabaseMaintenanceIsolationTests(unittest.TestCase):
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
            patch.object(Path, "iterdir", return_value=iter([Path("lo")])),
        ):
            require_isolated_runtime(connection)
        with patch.dict(
            os.environ, {"LAUNCHPLANE_SECRET_KEYS_JSON": "synthetic-production-keyring"}, clear=True
        ):
            with self.assertRaisesRegex(RuntimeError, "keyring"):
                require_isolated_runtime(connection)
        connection.engine.url = make_url("postgresql+psycopg://postgres@database.invalid/restore")
        with self.assertRaisesRegex(RuntimeError, "isolated loopback"):
            require_isolated_runtime(connection)

    def test_rehearsal_refuses_a_networked_namespace(self) -> None:
        connection = Mock()
        connection.engine.url = make_url("postgresql+psycopg://postgres@127.0.0.1/restore")
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("control_plane.database_maintenance_cli.sys.platform", "linux"),
            patch.object(Path, "iterdir", return_value=iter([Path("lo"), Path("eth0")])),
        ):
            with self.assertRaisesRegex(RuntimeError, "loopback-only"):
                require_isolated_runtime(connection)
