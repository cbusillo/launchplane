import base64
import os
import stat
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class StartLaunchplaneServiceScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script_path = (
            Path(__file__).resolve().parents[1] / "scripts" / "start-launchplane-service.sh"
        )

    def _write_fake_uv(self, bin_dir: Path) -> None:
        uv_path = bin_dir / "uv"
        uv_path.write_text(
            """#!/bin/sh
if [ "$1" = "run" ] && [ "$2" = "python" ]; then
  if [ "${UV_SCHEMA_STATUS:-0}" = "2" ]; then
    exit 2
  fi
  exit "${UV_SCHEMA_STATUS:-0}"
fi
printf '%s\n' "$@" >>"$UV_CAPTURE_FILE"
""",
            encoding="utf-8",
        )
        uv_path.chmod(uv_path.stat().st_mode | stat.S_IXUSR)

    def test_requires_explicit_policy_input(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            app_root = temporary_directory / "app"
            app_root.mkdir()

            result = subprocess.run(
                [str(self.script_path)],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "LAUNCHPLANE_APP_ROOT": str(app_root),
                    "LAUNCHPLANE_STATE_DIR": str(temporary_directory / "state"),
                },
                check=False,
            )

        self.assertEqual(result.returncode, 1, msg=result.stderr)
        self.assertIn("requires an explicit policy input", result.stderr)

    def test_rejects_example_policy_file_path(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            app_root = temporary_directory / "app"
            example_policy = app_root / "config" / "launchplane-authz.toml.example"
            example_policy.parent.mkdir(parents=True)
            example_policy.write_text("schema_version = 1\n", encoding="utf-8")

            result = subprocess.run(
                [str(self.script_path)],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "LAUNCHPLANE_APP_ROOT": str(app_root),
                    "LAUNCHPLANE_STATE_DIR": str(temporary_directory / "state"),
                    "LAUNCHPLANE_POLICY_FILE": str(example_policy),
                },
                check=False,
            )

        self.assertEqual(result.returncode, 1, msg=result.stderr)
        self.assertIn("Refusing to start Launchplane with example policy file", result.stderr)

    def test_requires_database_url_for_loopback_startup(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            app_root = temporary_directory / "app"
            app_root.mkdir()

            result = subprocess.run(
                [str(self.script_path)],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "LAUNCHPLANE_APP_ROOT": str(app_root),
                    "LAUNCHPLANE_STATE_DIR": str(temporary_directory / "runtime"),
                    "LAUNCHPLANE_POLICY_TOML": "schema_version = 1\n",
                    "LAUNCHPLANE_SERVICE_HOST": "127.0.0.1",
                    "LAUNCHPLANE_DATABASE_URL": "",
                },
                check=False,
            )

        self.assertEqual(result.returncode, 1, msg=result.stderr)
        self.assertIn("refuses startup without LAUNCHPLANE_DATABASE_URL", result.stderr)

    def test_accepts_explicit_base64_policy_input(self) -> None:
        policy_path = Path("/tmp/launchplane-authz.toml")
        policy_path.unlink(missing_ok=True)

        try:
            with TemporaryDirectory() as temporary_directory_name:
                temporary_directory = Path(temporary_directory_name)
                app_root = temporary_directory / "app"
                bin_dir = temporary_directory / "bin"
                capture_file = temporary_directory / "uv-args.txt"
                app_root.mkdir()
                bin_dir.mkdir()
                self._write_fake_uv(bin_dir)

                result = subprocess.run(
                    [str(self.script_path)],
                    capture_output=True,
                    text=True,
                    env={
                        **os.environ,
                        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                        "UV_CAPTURE_FILE": str(capture_file),
                        "LAUNCHPLANE_APP_ROOT": str(app_root),
                        "LAUNCHPLANE_STATE_DIR": str(temporary_directory / "runtime"),
                        "LAUNCHPLANE_SERVICE_HOST": "127.0.0.1",
                        "LAUNCHPLANE_DATABASE_URL": "postgresql+psycopg://launchplane:test@db/launchplane",
                        "LAUNCHPLANE_POLICY_B64": base64.b64encode(b"schema_version = 1\n").decode(
                            "ascii"
                        ),
                    },
                    check=False,
                )

                captured_args = capture_file.read_text(encoding="utf-8").splitlines()

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("--policy-file", captured_args)
            self.assertIn(str(policy_path), captured_args)
            self.assertIn("--database-url", captured_args)
            self.assertEqual(policy_path.read_text(encoding="utf-8"), "schema_version = 1\n")
        finally:
            policy_path.unlink(missing_ok=True)

    def test_rejects_hosted_filesystem_startup_without_database_url(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            app_root = temporary_directory / "app"
            app_root.mkdir()

            result = subprocess.run(
                [str(self.script_path)],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "LAUNCHPLANE_APP_ROOT": str(app_root),
                    "LAUNCHPLANE_STATE_DIR": str(temporary_directory / "state"),
                    "LAUNCHPLANE_POLICY_TOML": "schema_version = 1\n",
                    "LAUNCHPLANE_SERVICE_HOST": "0.0.0.0",
                    "LAUNCHPLANE_DATABASE_URL": "",
                },
                check=False,
            )

        self.assertEqual(result.returncode, 1, msg=result.stderr)
        self.assertIn("refuses startup without LAUNCHPLANE_DATABASE_URL", result.stderr)

    def test_forwards_database_url_for_hosted_startup(self) -> None:
        policy_path = Path("/tmp/launchplane-authz.toml")
        policy_path.unlink(missing_ok=True)

        try:
            with TemporaryDirectory() as temporary_directory_name:
                temporary_directory = Path(temporary_directory_name)
                app_root = temporary_directory / "app"
                bin_dir = temporary_directory / "bin"
                capture_file = temporary_directory / "uv-args.txt"
                app_root.mkdir()
                bin_dir.mkdir()
                self._write_fake_uv(bin_dir)

                result = subprocess.run(
                    [str(self.script_path)],
                    capture_output=True,
                    text=True,
                    env={
                        **os.environ,
                        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                        "UV_CAPTURE_FILE": str(capture_file),
                        "LAUNCHPLANE_APP_ROOT": str(app_root),
                        "LAUNCHPLANE_STATE_DIR": str(temporary_directory / "state"),
                        "LAUNCHPLANE_POLICY_TOML": "schema_version = 1\n",
                        "LAUNCHPLANE_SERVICE_HOST": "0.0.0.0",
                        "LAUNCHPLANE_DATABASE_URL": "postgresql+psycopg://launchplane:test@db/launchplane",
                    },
                    check=False,
                )

                captured_args = capture_file.read_text(encoding="utf-8").splitlines()

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("--database-url", captured_args)
            self.assertIn("postgresql+psycopg://launchplane:test@db/launchplane", captured_args)
        finally:
            policy_path.unlink(missing_ok=True)

    def test_stamps_unversioned_schema_before_upgrade(self) -> None:
        policy_path = Path("/tmp/launchplane-authz.toml")
        policy_path.unlink(missing_ok=True)

        try:
            with TemporaryDirectory() as temporary_directory_name:
                temporary_directory = Path(temporary_directory_name)
                app_root = temporary_directory / "app"
                bin_dir = temporary_directory / "bin"
                capture_file = temporary_directory / "uv-args.txt"
                app_root.mkdir()
                bin_dir.mkdir()
                self._write_fake_uv(bin_dir)

                result = subprocess.run(
                    [str(self.script_path)],
                    capture_output=True,
                    text=True,
                    env={
                        **os.environ,
                        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                        "UV_CAPTURE_FILE": str(capture_file),
                        "UV_SCHEMA_STATUS": "1",
                        "LAUNCHPLANE_APP_ROOT": str(app_root),
                        "LAUNCHPLANE_STATE_DIR": str(temporary_directory / "state"),
                        "LAUNCHPLANE_POLICY_TOML": "schema_version = 1\n",
                        "LAUNCHPLANE_SERVICE_HOST": "0.0.0.0",
                        "LAUNCHPLANE_DATABASE_URL": "postgresql+psycopg://launchplane:test@db/launchplane",
                    },
                    check=False,
                )

                captured_lines = capture_file.read_text(encoding="utf-8").splitlines()

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("alembic", captured_lines)
            self.assertIn("stamp", captured_lines)
            self.assertIn("fe94a0486977", captured_lines)
            self.assertIn("upgrade", captured_lines)
            self.assertIn("head", captured_lines)
        finally:
            policy_path.unlink(missing_ok=True)

    def test_fails_when_schema_probe_errors(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            app_root = temporary_directory / "app"
            bin_dir = temporary_directory / "bin"
            capture_file = temporary_directory / "uv-args.txt"
            app_root.mkdir()
            bin_dir.mkdir()
            self._write_fake_uv(bin_dir)

            result = subprocess.run(
                [str(self.script_path)],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                    "UV_CAPTURE_FILE": str(capture_file),
                    "UV_SCHEMA_STATUS": "2",
                    "LAUNCHPLANE_APP_ROOT": str(app_root),
                    "LAUNCHPLANE_STATE_DIR": str(temporary_directory / "state"),
                    "LAUNCHPLANE_POLICY_TOML": "schema_version = 1\n",
                    "LAUNCHPLANE_SERVICE_HOST": "0.0.0.0",
                    "LAUNCHPLANE_DATABASE_URL": "postgresql+psycopg://launchplane:test@db/launchplane",
                },
                check=False,
            )

        self.assertEqual(result.returncode, 1, msg=result.stdout)
        self.assertIn("schema verification failed before migrations", result.stderr)


if __name__ == "__main__":
    unittest.main()
