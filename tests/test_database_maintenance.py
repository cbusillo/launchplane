import os
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

from sqlalchemy.engine import make_url

from control_plane.database_maintenance_cli import require_isolated_runtime


class DatabaseMaintenanceIsolationTests(unittest.TestCase):
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
