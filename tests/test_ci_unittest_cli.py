import json
from pathlib import Path
import tempfile
import sys
import unittest

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.unittest_sharding import SHARD_RUN_RECORD_TYPE, TIMING_SCHEMA_VERSION


class CiUnittestCliTests(unittest.TestCase):
    def test_list_outputs_discovered_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            tests_directory = _write_test_package(root, package_name="sample_cli_list_tests")
            _remove_imported_package("sample_cli_list_tests")

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
        self.assertEqual(result.output.strip(), "sample_cli_list_tests.test_sample")

    def test_plan_outputs_json_with_all_modules_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            tests_directory = _write_test_package(root, package_name="sample_cli_plan_tests")
            _remove_imported_package("sample_cli_plan_tests")

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
        self.assertEqual(modules, ["sample_cli_plan_tests.test_sample"])

    def test_plan_splits_large_modules_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            tests_directory = _write_test_package(
                root,
                package_name="sample_cli_split_plan_tests",
                extra_test=True,
            )
            _remove_imported_package("sample_cli_split_plan_tests")

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
                    "--import-root",
                    str(root),
                    "--max-tests-per-target",
                    "1",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        modules = [
            module_name
            for shard in payload["shards"]
            for module_name in shard["modules"]
        ]
        self.assertEqual(
            sorted(modules),
            [
                "sample_cli_split_plan_tests.test_sample.SampleTests.test_other",
                "sample_cli_split_plan_tests.test_sample.SampleTests.test_sample",
            ],
        )

    def test_run_writes_timing_artifact_and_returns_failure_for_failing_shard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            tests_directory = _write_test_package(
                root,
                passing=False,
                package_name="sample_cli_failing_run_tests",
            )
            _remove_imported_package("sample_cli_failing_run_tests")
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
        self.assertEqual(payload["modules"]["sample_cli_failing_run_tests.test_sample"]["tests_run"], 1)

    def test_run_writes_split_target_timing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            tests_directory = _write_test_package(
                root,
                package_name="sample_cli_split_run_tests",
                extra_test=True,
            )
            _remove_imported_package("sample_cli_split_run_tests")
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
                    "--max-tests-per-target",
                    "1",
                    "--verbosity",
                    "1",
                ],
            )
            payload = json.loads(output_file.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            payload["modules"]["sample_cli_split_run_tests.test_sample.SampleTests.test_other"]["tests_run"],
            1,
        )
        self.assertEqual(
            payload["modules"]["sample_cli_split_run_tests.test_sample.SampleTests.test_sample"]["tests_run"],
            1,
        )

    def test_aggregate_writes_next_run_timing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            tests_directory = _write_test_package(root, package_name="sample_cli_aggregate_tests")
            _remove_imported_package("sample_cli_aggregate_tests")
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
                            "sample_cli_aggregate_tests.test_sample": {"seconds": 0.5, "tests_run": 1}
                        },
                    }
                ),
                encoding="utf-8",
            )
            output_file = root / "aggregate" / "timings.json"
            input_file = root / "aggregate" / "previous.json"

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
                    "--timings-file",
                    str(input_file),
                    "--start-directory",
                    str(tests_directory),
                ],
            )
            payload = json.loads(output_file.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(payload["modules"]["sample_cli_aggregate_tests.test_sample"]["seconds"], 0.5)

    def test_aggregate_writes_split_target_timing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            tests_directory = _write_test_package(
                root,
                package_name="sample_cli_split_aggregate_tests",
                extra_test=True,
            )
            _remove_imported_package("sample_cli_split_aggregate_tests")
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
                            "sample_cli_split_aggregate_tests.test_sample.SampleTests.test_sample": {
                                "seconds": 0.5,
                                "tests_run": 1,
                            },
                            "sample_cli_split_aggregate_tests.test_sample.SampleTests.test_other": {
                                "seconds": 0.25,
                                "tests_run": 1,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            output_file = root / "aggregate" / "timings.json"
            input_file = root / "aggregate" / "previous.json"
            input_file.parent.mkdir(parents=True)
            input_file.write_text(
                json.dumps(
                    {
                        "schema_version": TIMING_SCHEMA_VERSION,
                        "record_type": "unittest_module_timings",
                        "modules": {
                            "sample_cli_split_aggregate_tests.test_sample": {
                                "seconds": 1.0,
                                "tests_run": 2,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

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
                    "--timings-file",
                    str(input_file),
                    "--start-directory",
                    str(tests_directory),
                    "--import-root",
                    str(root),
                    "--max-tests-per-target",
                    "1",
                ],
            )
            payload = json.loads(output_file.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            payload["modules"]["sample_cli_split_aggregate_tests.test_sample.SampleTests.test_sample"]["seconds"],
            0.5,
        )
        self.assertEqual(
            payload["modules"]["sample_cli_split_aggregate_tests.test_sample.SampleTests.test_other"]["seconds"],
            0.25,
        )


def _write_test_package(
    root: Path,
    *,
    passing: bool = True,
    package_name: str = "sample_cli_tests",
    extra_test: bool = False,
) -> Path:
    tests_directory = root / package_name
    tests_directory.mkdir()
    (tests_directory / "__init__.py").write_text("", encoding="utf-8")
    assertion = "self.assertTrue(True)" if passing else "self.assertTrue(False)"
    extra_method = (
        "    def test_other(self):\n"
        f"        {assertion}\n"
        if extra_test
        else ""
    )
    (tests_directory / "test_sample.py").write_text(
        "import unittest\n\n"
        "class SampleTests(unittest.TestCase):\n"
        "    def test_sample(self):\n"
        f"        {assertion}\n"
        f"{extra_method}",
        encoding="utf-8",
    )
    return tests_directory


def _remove_imported_package(package_name: str) -> None:
    for module_name in tuple(sys.modules):
        if module_name == package_name or module_name.startswith(f"{package_name}."):
            del sys.modules[module_name]
