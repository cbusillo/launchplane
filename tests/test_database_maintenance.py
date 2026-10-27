import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

from sqlalchemy.engine import make_url

from control_plane.database_maintenance_cli import await_command, require_isolated_runtime
from control_plane import database_maintenance_cli
from control_plane.deployment_maintenance import MAINTENANCE_UNTIL_ENV, recovery_marker
from control_plane.runtime_platform_credentials import PLATFORM_CREDENTIAL_KEYS
from tempfile import TemporaryDirectory
import io


class DatabaseMaintenanceIsolationTests(unittest.TestCase):
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
            patch.object(Path, "iterdir", return_value=iter([Path("lo")])),
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

    def test_rehearsal_refuses_a_networked_namespace(self) -> None:
        connection = Mock()
        connection.engine.url = make_url("postgresql+psycopg://postgres@127.0.0.1/restore")
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("control_plane.database_maintenance_cli.sys.platform", "linux"),
            patch.object(Path, "iterdir", return_value=iter([Path("lo"), Path("eth0")])),
        ):
            with self.assertRaises(RuntimeError):
                require_isolated_runtime(connection)
