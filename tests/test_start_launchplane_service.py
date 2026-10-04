import base64
import os
from fnmatch import fnmatchcase
import shlex
import stat
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml


class ComposeWorkerSupervisionTests(unittest.TestCase):
    def test_worker_services_share_primary_settings_and_wait_for_health(self) -> None:
        compose_path = Path(__file__).resolve().parents[1] / "docker-compose.yml"
        services = yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"]
        assert isinstance(services, dict)
        primary = services["launchplane"]
        assert isinstance(primary, dict)
        workers = []
        for name, service in services.items():
            assert isinstance(service, dict)
            command = service.get("command", [])
            arguments = shlex.split(command) if isinstance(command, str) else command
            assert isinstance(arguments, list)
            if any(
                fnmatchcase(str(argument), "/app/scripts/start-launchplane-*-workers.sh")
                for argument in arguments
            ):
                workers.append(name)
                with self.subTest(service=name):
                    for key in ("image", "restart", "env_file", "volumes", "networks"):
                        self.assertIn(key, primary)
                        self.assertEqual(service.get(key), primary[key], key)
                    depends_on = service.get("depends_on")
                    assert isinstance(depends_on, dict)
                    dependency = depends_on.get("launchplane")
                    assert isinstance(dependency, dict)
                    self.assertEqual(dependency.get("condition"), "service_healthy")
        self.assertTrue(workers, "Compose must define supervised worker services")


class StartLaunchplaneServiceScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script_path = (
            Path(__file__).resolve().parents[1] / "scripts" / "start-launchplane-service.sh"
        )

    def _write_fake_uv(self, bin_dir: Path) -> None:
        uv_path = bin_dir / "uv"
        uv_path.write_text(
            """#!/bin/sh
printf '%s\n' "$@" >>"$UV_CAPTURE_FILE"
if [ "$1" = "run" ] && [ "$2" = "python" ]; then
  if [ "${UV_SCHEMA_STATUS:-0}" = "2" ]; then
    exit 2
  fi
  printf '%s\n' "${UV_SCHEMA_REVISION:-b3d5f7a9c1e4}"
  exit 0
fi
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
            self.assertNotIn("--database-url", captured_args)
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

    def test_keeps_database_url_out_of_hosted_startup_arguments(self) -> None:
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
            self.assertNotIn("stamp", captured_args)
            self.assertNotIn("--database-url", captured_args)
            self.assertNotIn("postgresql+psycopg://launchplane:test@db/launchplane", captured_args)
        finally:
            policy_path.unlink(missing_ok=True)

    def test_stamps_legacy_head_for_unversioned_current_schema(self) -> None:
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
                        "UV_LEGACY_REVISION": "b1c3d5e7f9a1",
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
            self.assertIn("control_plane.storage.schema_migration", captured_lines)
        finally:
            policy_path.unlink(missing_ok=True)

    def test_stamps_legacy_head_for_baseline_stamped_current_schema(self) -> None:
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
                        "UV_SCHEMA_STATUS": "0",
                        "UV_LEGACY_REVISION": "b1c3d5e7f9a1",
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
            self.assertIn("control_plane.storage.schema_migration", captured_lines)
        finally:
            policy_path.unlink(missing_ok=True)

    def test_stamps_baseline_for_unversioned_baseline_schema(self) -> None:
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
                        "UV_LEGACY_REVISION": "fe94a0486977",
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
            self.assertIn("control_plane.storage.schema_migration", captured_lines)
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
        self.assertIn("schema migration failed before service startup", result.stderr)


class StartLaunchplaneOdooWorkersScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        self.script_path = repo_root / "scripts" / "start-launchplane-odoo-workers.sh"

    def _write_fake_uv(self, bin_dir: Path) -> None:
        uv_path = bin_dir / "uv"
        uv_path.write_text(
            """#!/bin/sh
printf '%s\n' "$@" >>"$UV_CAPTURE_FILE"
""",
            encoding="utf-8",
        )
        uv_path.chmod(uv_path.stat().st_mode | stat.S_IXUSR)

    def test_requires_database_url_for_worker_startup(self) -> None:
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
                    "LAUNCHPLANE_DATABASE_URL": "",
                },
                check=False,
            )

        self.assertEqual(result.returncode, 1, msg=result.stderr)
        self.assertIn("refuse startup without LAUNCHPLANE_DATABASE_URL", result.stderr)

    def test_worker_startup_uses_database_env_and_generic_timing_options(self) -> None:
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
                    "LAUNCHPLANE_DATABASE_URL": "postgresql+psycopg://launchplane:test@db/launchplane",
                    "LAUNCHPLANE_ODOO_WORKER_LEASE_SECONDS": "120",
                    "LAUNCHPLANE_ODOO_WORKER_HEARTBEAT_SECONDS": "20",
                    "LAUNCHPLANE_ODOO_WORKER_MAX_ATTEMPTS": "2",
                    "LAUNCHPLANE_ODOO_WORKER_POLL_SECONDS": "5",
                    "LAUNCHPLANE_ODOO_WORKER_ERROR_BACKOFF_SECONDS": "15",
                    "LAUNCHPLANE_ODOO_WORKER_MAX_CONSECUTIVE_ERRORS": "3",
                },
                check=False,
            )

            captured_args = capture_file.read_text(encoding="utf-8").splitlines()

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(
            captured_args[:5],
            ["run", "launchplane", "service", "odoo-workers", "run"],
        )
        self.assertIn("--state-dir", captured_args)
        self.assertNotIn("--database-url", captured_args)
        self.assertNotIn("postgresql+psycopg://launchplane:test@db/launchplane", captured_args)
        self.assertIn("--lease-seconds", captured_args)
        self.assertIn("120", captured_args)
        self.assertIn("--heartbeat-seconds", captured_args)
        self.assertIn("20", captured_args)
        self.assertIn("--max-attempts", captured_args)
        self.assertIn("2", captured_args)
        self.assertIn("--poll-seconds", captured_args)
        self.assertIn("5", captured_args)
        self.assertIn("--error-backoff-seconds", captured_args)
        self.assertIn("15", captured_args)
        self.assertIn("--max-consecutive-errors", captured_args)
        self.assertIn("3", captured_args)


class StartLaunchplaneVeriReelWorkersScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        self.script_path = repo_root / "scripts" / "start-launchplane-verireel-workers.sh"

    def _write_fake_uv(self, bin_dir: Path) -> None:
        uv_path = bin_dir / "uv"
        uv_path.write_text(
            """#!/bin/sh
printf '%s\n' "$@" >>"$UV_CAPTURE_FILE"
""",
            encoding="utf-8",
        )
        uv_path.chmod(uv_path.stat().st_mode | stat.S_IXUSR)

    def test_requires_database_url_for_worker_startup(self) -> None:
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
                    "LAUNCHPLANE_DATABASE_URL": "",
                },
                check=False,
            )

        self.assertEqual(result.returncode, 1, msg=result.stderr)
        self.assertIn("refuse startup without LAUNCHPLANE_DATABASE_URL", result.stderr)

    def test_worker_startup_uses_database_env_and_generic_timing_options(self) -> None:
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
                    "LAUNCHPLANE_DATABASE_URL": "postgresql+psycopg://launchplane:test@db/launchplane",
                    "LAUNCHPLANE_VERIREEL_WORKER_LEASE_SECONDS": "120",
                    "LAUNCHPLANE_VERIREEL_WORKER_HEARTBEAT_SECONDS": "20",
                    "LAUNCHPLANE_VERIREEL_WORKER_MAX_ATTEMPTS": "2",
                    "LAUNCHPLANE_VERIREEL_WORKER_POLL_SECONDS": "5",
                    "LAUNCHPLANE_VERIREEL_WORKER_ERROR_BACKOFF_SECONDS": "15",
                    "LAUNCHPLANE_VERIREEL_WORKER_MAX_CONSECUTIVE_ERRORS": "3",
                },
                check=False,
            )

            captured_args = capture_file.read_text(encoding="utf-8").splitlines()

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(
            captured_args[:5],
            ["run", "launchplane", "service", "verireel-workers", "run"],
        )
        self.assertIn("--state-dir", captured_args)
        self.assertNotIn("--database-url", captured_args)
        self.assertNotIn("postgresql+psycopg://launchplane:test@db/launchplane", captured_args)
        self.assertIn("--lease-seconds", captured_args)
        self.assertIn("120", captured_args)
        self.assertIn("--heartbeat-seconds", captured_args)
        self.assertIn("20", captured_args)
        self.assertIn("--max-attempts", captured_args)
        self.assertIn("2", captured_args)
        self.assertIn("--poll-seconds", captured_args)
        self.assertIn("5", captured_args)
        self.assertIn("--error-backoff-seconds", captured_args)
        self.assertIn("15", captured_args)
        self.assertIn("--max-consecutive-errors", captured_args)
        self.assertIn("3", captured_args)


class StartLaunchplaneMergeTrainWorkersScriptTests(unittest.TestCase):
    def test_worker_startup_runs_the_scheduler_with_its_interval(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            bin_dir = temporary_directory / "bin"
            capture_file = temporary_directory / "uv-args.txt"
            bin_dir.mkdir()
            uv_path = bin_dir / "uv"
            uv_path.write_text(
                """#!/bin/sh
printf '%s\\n' "$@" >>"$UV_CAPTURE_FILE"
""",
                encoding="utf-8",
            )
            uv_path.chmod(uv_path.stat().st_mode | stat.S_IXUSR)

            result = subprocess.run(
                [str(repo_root / "scripts" / "start-launchplane-merge-train-workers.sh")],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                    "UV_CAPTURE_FILE": str(capture_file),
                    "LAUNCHPLANE_STATE_DIR": str(temporary_directory / "runtime"),
                    "LAUNCHPLANE_DATABASE_URL": "postgresql+psycopg://launchplane:test@db/launchplane",
                    "LAUNCHPLANE_MERGE_TRAIN_SCHEDULER_INTERVAL_SECONDS": "120",
                },
                check=False,
            )

            captured_args = capture_file.read_text(encoding="utf-8").splitlines()

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(
            captured_args[:5],
            ["run", "launchplane", "service", "merge-train-workers", "run"],
        )
        self.assertEqual(captured_args[captured_args.index("--interval-seconds") + 1], "120")
        self.assertNotIn("postgresql+psycopg://launchplane:test@db/launchplane", captured_args)


class StartLaunchplanePrivilegedOperationWorkersScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        self.script_path = (
            repo_root / "scripts" / "start-launchplane-privileged-operation-workers.sh"
        )

    def _write_fake_uv(self, bin_dir: Path) -> None:
        uv_path = bin_dir / "uv"
        uv_path.write_text(
            """#!/bin/sh
printf '%s\\n' \"$@\" >>\"$UV_CAPTURE_FILE\"
if [ -n "${UV_CAPTURE_EVIDENCE_FILE:-}" ]; then
    cat <&3 >"$UV_CAPTURE_EVIDENCE_FILE"
fi
""",
            encoding="utf-8",
        )
        uv_path.chmod(uv_path.stat().st_mode | stat.S_IXUSR)

    def _write_fake_timeout(self, bin_dir: Path) -> None:
        timeout_path = bin_dir / "timeout"
        timeout_path.write_text(
            """#!/bin/sh
if [ -n "${TIMEOUT_EXIT_CODE:-}" ]; then
    exit "$TIMEOUT_EXIT_CODE"
fi
shift 2
exec "$@"
""",
            encoding="utf-8",
        )
        timeout_path.chmod(timeout_path.stat().st_mode | stat.S_IXUSR)

    def _write_fake_python(
        self,
        bin_dir: Path,
        *,
        environment_capture_file: Path,
        exit_code: int = 0,
    ) -> None:
        python_path = bin_dir / "python"
        python_path.write_text(
            f"""#!/bin/sh
env | sort >"{environment_capture_file}"
exit {exit_code}
""",
            encoding="utf-8",
        )
        python_path.chmod(python_path.stat().st_mode | stat.S_IXUSR)

    def test_requires_database_url_for_worker_startup(self) -> None:
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
                    "LAUNCHPLANE_DATABASE_URL": "",
                },
                check=False,
            )

        self.assertEqual(result.returncode, 1, msg=result.stderr)
        self.assertIn("refuse startup without LAUNCHPLANE_DATABASE_URL", result.stderr)

    def test_worker_startup_wires_only_process_settings(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            app_root = temporary_directory / "app"
            bin_dir = temporary_directory / "bin"
            capture_file = temporary_directory / "uv-args.txt"
            probe_environment_file = temporary_directory / "worker-probe-env.txt"
            evidence_capture_file = temporary_directory / "worker-probe-evidence.txt"
            app_root.mkdir()
            bin_dir.mkdir()
            self._write_fake_uv(bin_dir)
            self._write_fake_timeout(bin_dir)
            self._write_fake_python(
                bin_dir,
                environment_capture_file=probe_environment_file,
            )

            result = subprocess.run(
                [str(self.script_path)],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                    "UV_CAPTURE_FILE": str(capture_file),
                    "UV_CAPTURE_EVIDENCE_FILE": str(evidence_capture_file),
                    "LAUNCHPLANE_APP_ROOT": str(app_root),
                    "LAUNCHPLANE_STATE_DIR": str(temporary_directory / "runtime"),
                    "LAUNCHPLANE_DATABASE_URL": "postgresql+psycopg://launchplane:test@db/launchplane",
                    "LAUNCHPLANE_PRIVILEGED_OPERATION_WORKER_POLL_SECONDS": "5",
                    "LAUNCHPLANE_PRIVILEGED_OPERATION_WORKER_LIMIT": "4",
                    "LAUNCHPLANE_PRIVILEGED_OPERATION_WORKER_ERROR_BACKOFF_SECONDS": "15",
                    "LAUNCHPLANE_PRIVILEGED_OPERATION_WORKER_MAX_CONSECUTIVE_ERRORS": "3",
                    "LAUNCHPLANE_PRIVILEGED_OPERATION_WORKER_OPERATION_ID": "must-not-forward",
                    "LAUNCHPLANE_PRIVILEGED_OPERATION_WORKER_PLAN_DIGEST": "must-not-forward",
                    "LAUNCHPLANE_PRIVILEGED_OPERATION_WORKER_EXECUTE_PAYLOAD": "must-not-forward",
                    "PGSSLROOTCERT": "/tmp/root.crt",
                },
                check=False,
            )

            captured_args = capture_file.read_text(encoding="utf-8").splitlines()
            probe_environment = probe_environment_file.read_text(encoding="utf-8")
            probe_evidence = evidence_capture_file.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                '{"event":"privileged_operation_worker_entrypoint_started"}',
                '{"event":"privileged_operation_worker_entrypoint_probe_succeeded"}',
            ],
        )
        self.assertEqual(
            captured_args[:5],
            ["run", "launchplane", "service", "privileged-operation-workers", "run"],
        )
        self.assertEqual(
            probe_evidence,
            "launchplane-privileged-operation-worker-schema-probe-completed-v1\n",
        )
        self.assertIn(
            "LAUNCHPLANE_DATABASE_URL=postgresql+psycopg://launchplane:test@db/launchplane",
            probe_environment,
        )
        self.assertIn("PGSSLROOTCERT=/tmp/root.crt", probe_environment)
        self.assertNotIn("must-not-forward", probe_environment)
        self.assertNotIn("LAUNCHPLANE_PRIVILEGED_OPERATION_WORKER_", probe_environment)
        self.assertIn("--state-dir", captured_args)
        self.assertIn("--schema-probe-fd", captured_args)
        self.assertIn("3", captured_args)
        self.assertNotIn("--database-url", captured_args)
        self.assertIn("--poll-seconds", captured_args)
        self.assertIn("5", captured_args)
        self.assertIn("--limit", captured_args)
        self.assertIn("4", captured_args)
        self.assertIn("--error-backoff-seconds", captured_args)
        self.assertIn("15", captured_args)
        self.assertIn("--max-consecutive-errors", captured_args)
        self.assertIn("3", captured_args)
        self.assertNotIn("must-not-forward", captured_args)

    def test_worker_startup_fails_closed_when_probe_times_out(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            app_root = temporary_directory / "app"
            bin_dir = temporary_directory / "bin"
            capture_file = temporary_directory / "uv-args.txt"
            app_root.mkdir()
            bin_dir.mkdir()
            self._write_fake_uv(bin_dir)
            self._write_fake_timeout(bin_dir)

            result = subprocess.run(
                [str(self.script_path)],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                    "TIMEOUT_EXIT_CODE": "124",
                    "UV_CAPTURE_FILE": str(capture_file),
                    "LAUNCHPLANE_APP_ROOT": str(app_root),
                    "LAUNCHPLANE_STATE_DIR": str(temporary_directory / "runtime"),
                    "LAUNCHPLANE_DATABASE_URL": (
                        "postgresql+psycopg://launchplane:test@db/launchplane"
                    ),
                },
                check=False,
            )

        self.assertEqual(result.returncode, 1, msg=result.stderr)
        self.assertIn("privileged_operation_worker_entrypoint_started", result.stdout)
        self.assertNotIn("privileged_operation_worker_entrypoint_probe_succeeded", result.stdout)
        self.assertIn("privileged_operation_worker_startup_probe_failed", result.stdout)
        self.assertIn('"error_type":"timeout"', result.stdout)
        self.assertFalse(capture_file.exists())

    def test_worker_startup_reports_killed_probe_separately(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            app_root = temporary_directory / "app"
            bin_dir = temporary_directory / "bin"
            app_root.mkdir()
            bin_dir.mkdir()
            self._write_fake_timeout(bin_dir)

            result = subprocess.run(
                [str(self.script_path)],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                    "TIMEOUT_EXIT_CODE": "137",
                    "LAUNCHPLANE_APP_ROOT": str(app_root),
                    "LAUNCHPLANE_STATE_DIR": str(temporary_directory / "runtime"),
                    "LAUNCHPLANE_DATABASE_URL": (
                        "postgresql+psycopg://launchplane:test@db/launchplane"
                    ),
                },
                check=False,
            )

        self.assertEqual(result.returncode, 1, msg=result.stderr)
        self.assertIn('"error_type":"killed"', result.stdout)

    def test_worker_startup_reports_probe_failure(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            app_root = temporary_directory / "app"
            bin_dir = temporary_directory / "bin"
            probe_environment_file = temporary_directory / "worker-probe-env.txt"
            app_root.mkdir()
            bin_dir.mkdir()
            self._write_fake_timeout(bin_dir)
            self._write_fake_python(
                bin_dir,
                environment_capture_file=probe_environment_file,
                exit_code=2,
            )

            result = subprocess.run(
                [str(self.script_path)],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                    "LAUNCHPLANE_APP_ROOT": str(app_root),
                    "LAUNCHPLANE_STATE_DIR": str(temporary_directory / "runtime"),
                    "LAUNCHPLANE_DATABASE_URL": (
                        "postgresql+psycopg://launchplane:test@db/launchplane"
                    ),
                },
                check=False,
            )

        self.assertEqual(result.returncode, 1, msg=result.stderr)
        self.assertIn('"error_type":"probe_failed"', result.stdout)


if __name__ == "__main__":
    unittest.main()
