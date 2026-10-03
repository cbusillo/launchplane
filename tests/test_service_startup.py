from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from control_plane.cli import main


class ServiceStartupTests(unittest.TestCase):
    def test_serve_uses_environment_database_url(self) -> None:
        database_url = "postgresql+psycopg://fixture:synthetic-password@db.example.test/fixture"
        with TemporaryDirectory() as directory:
            policy_file = Path(directory) / "policy.toml"
            policy_file.write_text("schema_version = 1\n", encoding="utf-8")
            with patch("control_plane.cli_service.serve_launchplane_service") as serve:
                result = CliRunner().invoke(
                    main,
                    ["service", "serve", "--policy-file", str(policy_file)],
                    env={
                        "LAUNCHPLANE_DATABASE_URL": database_url,
                        "LAUNCHPLANE_SERVICE_AUDIENCE": "fixture-audience",
                    },
                )
        self.assertEqual(result.exit_code, 0, result.output)
        serve.assert_called_once()
        self.assertEqual(serve.call_args.kwargs["database_url"], database_url)
        self.assertNotIn(database_url, result.output)

    def test_serve_refuses_database_url_arguments_without_echoing_values(self) -> None:
        database_url = "postgresql+psycopg://fixture:synthetic-password@db.example.test/fixture"
        with TemporaryDirectory() as directory:
            policy_file = Path(directory) / "policy.toml"
            policy_file.write_text("schema_version = 1\n", encoding="utf-8")
            for arguments in (["--database-url", database_url], [f"--database-url={database_url}"]):
                with self.subTest(arguments=arguments):
                    with patch("control_plane.cli_service.serve_launchplane_service") as serve:
                        result = CliRunner().invoke(
                            main,
                            ["service", "serve", "--policy-file", str(policy_file), *arguments],
                            env={
                                "LAUNCHPLANE_DATABASE_URL": "sqlite+pysqlite:///:memory:",
                                "LAUNCHPLANE_SERVICE_AUDIENCE": "fixture-audience",
                            },
                        )
                    self.assertEqual(result.exit_code, 1, result.output)
                    self.assertIn("Set LAUNCHPLANE_DATABASE_URL", result.output)
                    self.assertNotIn(database_url, result.output)
                    self.assertNotIn("synthetic-password", result.output)
                    serve.assert_not_called()

    def test_entrypoint_keeps_database_url_in_environment(self) -> None:
        root = Path(__file__).resolve().parents[1]
        database_url = "postgresql+psycopg://fixture:synthetic-password@db.example.test/fixture"
        with TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            policy_file = temporary_root / "policy.toml"
            policy_file.write_text("schema_version = 1\n", encoding="utf-8")
            calls_file = temporary_root / "calls.jsonl"
            uv_stub = temporary_root / "uv"
            uv_stub.write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys\n"
                "with open(os.environ['STARTUP_CALLS_FILE'], 'a') as handle:\n"
                "    handle.write(json.dumps({'args': sys.argv[1:], "
                "'database_url': os.environ.get('LAUNCHPLANE_DATABASE_URL')}) + '\\n')\n"
                "if sys.argv[1:3] == ['run', 'python']:\n"
                "    print('fixture-revision')\n",
                encoding="utf-8",
            )
            uv_stub.chmod(0o755)
            result = subprocess.run(
                ["sh", str(root / "scripts/start-launchplane-service.sh")],
                cwd=root,
                env={
                    "PATH": f"{temporary_root}{os.pathsep}/usr/bin:/bin",
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    "LAUNCHPLANE_STATE_DIR": str(temporary_root / "state"),
                    "LAUNCHPLANE_POLICY_FILE": str(policy_file),
                    "LAUNCHPLANE_SERVICE_AUDIENCE": "fixture-audience",
                    "STARTUP_CALLS_FILE": str(calls_file),
                },
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            calls[0]["args"], ["run", "python", "-m", "control_plane.storage.schema_migration"]
        )
        self.assertEqual(calls[1]["args"][:4], ["run", "launchplane", "service", "serve"])
        for call in calls:
            self.assertEqual(call["database_url"], database_url)
            self.assertNotIn(database_url, call["args"])
            self.assertNotIn("--database-url", call["args"])
        self.assertNotIn(database_url, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
