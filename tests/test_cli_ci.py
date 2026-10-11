from __future__ import annotations

import subprocess
import unittest
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from control_plane.cli_ci import (
    POSTGRES_INTEGRATION_MODULES,
    postgres_integration_shard_targets,
    run_postgres_integration_tests,
)
from control_plane.unittest_sharding import UnittestShardingError, iter_test_target_ids


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

    def test_parallel_shards_cover_the_serial_suite_once(self) -> None:
        loader = unittest.TestLoader()
        serial = tuple(
            iter_test_target_ids(loader.loadTestsFromNames(POSTGRES_INTEGRATION_MODULES))
        )
        self.assertFalse(loader.errors)
        for count in (2, 3):
            with self.subTest(shard_count=count):
                shards = [
                    postgres_integration_shard_targets(shard_count=count, shard_index=index)
                    for index in range(count)
                ]
                combined = [target for shard in shards for target in shard]
                self.assertCountEqual(combined, serial)
                for shard in shards:
                    self.assertTrue(shard)
                    loaded = loader.loadTestsFromNames(shard)
                    self.assertCountEqual(iter_test_target_ids(loaded), shard)
                self.assertFalse(loader.errors)

    @patch("control_plane.cli_ci.subprocess.run")
    def test_shard_executes_its_discovered_tests_and_propagates_failure(
        self, run: MagicMock
    ) -> None:
        targets = postgres_integration_shard_targets(shard_count=3, shard_index=1)
        run.return_value = subprocess.CompletedProcess(args=(), returncode=1)
        result = CliRunner().invoke(
            run_postgres_integration_tests,
            [
                "--database-url",
                "postgresql+psycopg://fixture@127.0.0.1/postgres",
                "--shard-count",
                "3",
                "--shard-index",
                "1",
            ],
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(run.call_args.args[0][-len(targets) :], list(targets))
        self.assertEqual(
            run.call_args.kwargs["env"]["LAUNCHPLANE_TEST_POSTGRES_URL"],
            "postgresql+psycopg://fixture@127.0.0.1/postgres",
        )

    @patch("control_plane.cli_ci.subprocess.run")
    def test_invalid_shard_does_not_start_tests(self, run: MagicMock) -> None:
        for count, index in ((0, 0), (2, -1), (2, 2), (10000, 9999)):
            with self.subTest(count=count, index=index):
                result = CliRunner().invoke(
                    run_postgres_integration_tests,
                    [
                        "--database-url",
                        "postgresql+psycopg://fixture@127.0.0.1/postgres",
                        "--shard-count",
                        str(count),
                        "--shard-index",
                        str(index),
                    ],
                )
                self.assertNotEqual(result.exit_code, 0)
        run.assert_not_called()

    def test_discovery_failure_is_not_silently_omitted(self) -> None:
        with patch("control_plane.cli_ci.POSTGRES_INTEGRATION_MODULES", ("tests.missing_fixture",)):
            with self.assertRaisesRegex(UnittestShardingError, "failed to load"):
                postgres_integration_shard_targets(shard_count=3, shard_index=0)
