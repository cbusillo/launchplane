import json
from pathlib import Path
import tempfile
import unittest

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.unittest_sharding import SHARD_RUN_RECORD_TYPE, TIMING_SCHEMA_VERSION


class CiUnittestCliTests(unittest.TestCase):
    def test_list_outputs_discovered_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            tests_directory = _write_test_package(root)

            result = CliRunner().invoke(
                main,
                [
                    "ci",
                    "unittest-shard",
                    "list",
                    "--start-directory",
                    str(tests_directory),
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output.strip(), "tests.test_sample")

    def test_plan_outputs_json_with_all_modules_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            tests_directory = _write_test_package(root)

            result = CliRunner().invoke(
                main,
                [
                    "ci",
                    "unittest-shard",
                    "plan",
                    "--shard-count",
                    "2",
                    "--start-directory",
                    str(tests_directory),
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        modules = [
            module_name
            for shard in payload["shards"]
            for module_name in shard["modules"]
        ]
        self.assertEqual(payload["shard_count"], 2)
        self.assertEqual(modules, ["tests.test_sample"])

    def test_run_writes_timing_artifact_and_returns_failure_for_failing_shard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            tests_directory = _write_test_package(root, passing=False)
            output_file = root / "timings" / "shard-0.json"

            result = CliRunner().invoke(
                main,
                [
                    "ci",
                    "unittest-shard",
                    "run",
                    "--shard-count",
                    "1",
                    "--shard-index",
                    "0",
                    "--start-directory",
                    str(tests_directory),
                    "--import-root",
                    str(root),
                    "--timings-output",
                    str(output_file),
                    "--verbosity",
                    "1",
                ],
            )
            payload = json.loads(output_file.read_text(encoding="utf-8"))

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn('"modules"', result.output)
        self.assertEqual(payload["record_type"], SHARD_RUN_RECORD_TYPE)
        self.assertFalse(payload["successful"])
        self.assertEqual(payload["modules"]["tests.test_sample"]["tests_run"], 1)

    def test_aggregate_writes_next_run_timing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            tests_directory = _write_test_package(root)
            results_directory = root / "results"
            results_directory.mkdir()
            (results_directory / "shard-0.json").write_text(
                json.dumps(
                    {
                        "schema_version": TIMING_SCHEMA_VERSION,
                        "record_type": SHARD_RUN_RECORD_TYPE,
                        "shard_index": 0,
                        "shard_count": 1,
                        "successful": True,
                        "modules": {
                            "tests.test_sample": {"seconds": 0.5, "tests_run": 1}
                        },
                    }
                ),
                encoding="utf-8",
            )
            output_file = root / "aggregate" / "timings.json"

            result = CliRunner().invoke(
                main,
                [
                    "ci",
                    "unittest-shard",
                    "aggregate",
                    "--shard-count",
                    "1",
                    "--results-dir",
                    str(results_directory),
                    "--timings-output",
                    str(output_file),
                    "--start-directory",
                    str(tests_directory),
                ],
            )
            payload = json.loads(output_file.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(payload["modules"]["tests.test_sample"]["seconds"], 0.5)


def _write_test_package(root: Path, *, passing: bool = True) -> Path:
    tests_directory = root / "tests"
    tests_directory.mkdir()
    (tests_directory / "__init__.py").write_text("", encoding="utf-8")
    assertion = "self.assertTrue(True)" if passing else "self.assertTrue(False)"
    (tests_directory / "test_sample.py").write_text(
        "import unittest\n\n"
        "class SampleTests(unittest.TestCase):\n"
        "    def test_sample(self):\n"
        f"        {assertion}\n",
        encoding="utf-8",
    )
    return tests_directory
