import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click

from control_plane import dokploy as control_plane_dokploy


class DokployConfigTests(unittest.TestCase):
    def test_read_dokploy_config_prefers_control_plane_env_file(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            (control_plane_root / ".env").write_text(
                "DOKPLOY_HOST=https://dokploy.control-plane.example\nDOKPLOY_TOKEN=control-plane-token\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {},
                clear=True,
            ):
                host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)

        self.assertEqual(host, "https://dokploy.control-plane.example")
        self.assertEqual(token, "control-plane-token")

    def test_read_dokploy_config_uses_process_environment_over_file(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            (control_plane_root / ".env").write_text(
                "DOKPLOY_HOST=https://dokploy.control-plane.example\nDOKPLOY_TOKEN=control-plane-token\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "DOKPLOY_HOST": "https://dokploy.process.example",
                    "DOKPLOY_TOKEN": "process-token",
                },
                clear=True,
            ):
                host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)

        self.assertEqual(host, "https://dokploy.process.example")
        self.assertEqual(token, "process-token")

    def test_read_dokploy_config_supports_explicit_control_plane_env_file(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            explicit_env_file = control_plane_root / "tmp" / "control-plane.env"
            explicit_env_file.parent.mkdir(parents=True, exist_ok=True)
            explicit_env_file.write_text(
                "DOKPLOY_HOST=https://dokploy.explicit.example\nDOKPLOY_TOKEN=explicit-token\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    control_plane_dokploy.CONTROL_PLANE_ENV_FILE_ENV_VAR: str(explicit_env_file),
                },
                clear=True,
            ):
                host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)

        self.assertEqual(host, "https://dokploy.explicit.example")
        self.assertEqual(token, "explicit-token")

    def test_read_dokploy_config_fails_closed_without_control_plane_secret_source(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)

            with patch.dict(
                os.environ,
                {},
                clear=True,
            ):
                with self.assertRaises(click.ClickException) as raised_error:
                    control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)

        self.assertIn("control-plane .env", str(raised_error.exception))


if __name__ == "__main__":
    unittest.main()
