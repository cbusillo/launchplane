from __future__ import annotations

import subprocess
import unittest
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from control_plane.cli_ci import POSTGRES_INTEGRATION_MODULES, run_postgres_integration_tests


class PostgresIntegrationCommandTests(unittest.TestCase):
    @patch("control_plane.cli_ci.subprocess.run")
    def test_postgres_integration_runs_every_real_postgres_module(self, run: MagicMock) -> None:
        run.return_value = subprocess.CompletedProcess(args=(), returncode=0)

        result = CliRunner().invoke(
            run_postgres_integration_tests,
            ["--database-url", "postgresql+psycopg://postgres:test@127.0.0.1/postgres"],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        command = run.call_args.args[0]
        self.assertEqual(
            command[-len(POSTGRES_INTEGRATION_MODULES) :],
            list(POSTGRES_INTEGRATION_MODULES),
        )
