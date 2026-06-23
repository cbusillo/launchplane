import json
from pathlib import Path
import tempfile
import unittest

from control_plane.unittest_sharding import (
    SHARD_RUN_RECORD_TYPE,
    TIMING_RECORD_TYPE,
    TIMING_SCHEMA_VERSION,
    UnittestShardingError,
    aggregate_shard_timings,
    discover_test_modules,
    plan_shards,
    read_module_timings,
    run_test_modules,
    write_json_object,
    write_shard_run_summary,
)


class UnittestShardingTests(unittest.TestCase):
    def test_discover_test_modules_returns_sorted_unique_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            tests_directory = root / "tests"
            nested_directory = tests_directory / "nested"
            nested_directory.mkdir(parents=True)
            (tests_directory / "__init__.py").write_text("", encoding="utf-8")
            (nested_directory / "__init__.py").write_text("", encoding="utf-8")
            (tests_directory / "test_b.py").write_text("", encoding="utf-8")
            (tests_directory / "test_a.py").write_text("", encoding="utf-8")
            (nested_directory / "test_c.py").write_text("", encoding="utf-8")

            modules = discover_test_modules(start_directory=tests_directory)

        self.assertEqual(modules, ("tests.nested.test_c", "tests.test_a", "tests.test_b"))

    def test_plan_rejects_zero_shards(self) -> None:
        with self.assertRaisesRegex(UnittestShardingError, "shard count"):
            plan_shards(("tests.test_a",), shard_count=0)

    def test_plan_rejects_out_of_range_shard_index(self) -> None:
        shard_plan = plan_shards(("tests.test_a",), shard_count=1)

        with self.assertRaisesRegex(UnittestShardingError, "shard index"):
            shard_plan.shard(1)

    def test_plan_uses_timing_weights_to_balance_slow_modules(self) -> None:
        shard_plan = plan_shards(
            ("tests.test_fast_a", "tests.test_fast_b", "tests.test_slow"),
            shard_count=2,
            module_seconds={"tests.test_slow": 10.0},
        )

        self.assertEqual(shard_plan.shard(0).modules, ("tests.test_slow",))
        self.assertEqual(
            shard_plan.shard(1).modules,
            ("tests.test_fast_a", "tests.test_fast_b"),
        )

    def test_plan_is_stable_when_timings_are_missing(self) -> None:
        shard_plan = plan_shards(
            ("tests.test_c", "tests.test_a", "tests.test_b"),
            shard_count=2,
        )

        self.assertEqual(shard_plan.shard(0).modules, ("tests.test_a", "tests.test_c"))
        self.assertEqual(shard_plan.shard(1).modules, ("tests.test_b",))

    def test_read_module_timings_rejects_malformed_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            timings_file = Path(temporary_directory_name) / "timings.json"
            timings_file.write_text(
                json.dumps({"schema_version": 999, "record_type": TIMING_RECORD_TYPE}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(UnittestShardingError, "schema version"):
                read_module_timings(timings_file)

    def test_run_fails_closed_when_selected_shard_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            with (root / "test-output.txt").open("w", encoding="utf-8") as stream:
                with self.assertRaisesRegex(UnittestShardingError, "no test modules"):
                    run_test_modules(
                        (),
                        shard_index=0,
                        shard_count=1,
                        import_root=root,
                        stream=stream,
                    )

    def test_run_writes_module_timing_records_for_successful_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            _write_test_package(root, package_name="sample_run_tests")
            output_file = root / "shard-0.json"
            with (root / "test-output.txt").open("w", encoding="utf-8") as stream:
                summary = run_test_modules(
                    ("sample_run_tests.test_sample",),
                    shard_index=0,
                    shard_count=1,
                    import_root=root,
                    stream=stream,
                )
            write_shard_run_summary(summary, output_file)

            payload = json.loads(output_file.read_text(encoding="utf-8"))

        self.assertTrue(payload["successful"])
        self.assertEqual(payload["modules"]["sample_run_tests.test_sample"]["tests_run"], 1)
        self.assertGreaterEqual(payload["modules"]["sample_run_tests.test_sample"]["seconds"], 0)

    def test_aggregate_requires_all_shard_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)

            with self.assertRaisesRegex(UnittestShardingError, "missing shard timing file"):
                aggregate_shard_timings(
                    results_directory=root,
                    shard_count=1,
                    discovered_modules=("tests.test_sample",),
                )

    def test_aggregate_rejects_unknown_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            write_json_object(
                root / "shard-0.json",
                {
                    "schema_version": 999,
                    "record_type": SHARD_RUN_RECORD_TYPE,
                    "shard_index": 0,
                    "shard_count": 1,
                    "successful": True,
                    "modules": {},
                },
            )

            with self.assertRaisesRegex(UnittestShardingError, "schema version"):
                aggregate_shard_timings(
                    results_directory=root,
                    shard_count=1,
                    discovered_modules=("tests.test_sample",),
                )

    def test_aggregate_ignores_stale_modules_and_writes_discovered_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            write_json_object(
                root / "shard-0.json",
                {
                    "schema_version": TIMING_SCHEMA_VERSION,
                    "record_type": SHARD_RUN_RECORD_TYPE,
                    "shard_index": 0,
                    "shard_count": 1,
                    "successful": True,
                    "modules": {
                        "tests.test_sample": {"seconds": 1.5, "tests_run": 2},
                        "tests.test_deleted": {"seconds": 99.0, "tests_run": 1},
                    },
                },
            )

            payload = aggregate_shard_timings(
                results_directory=root,
                shard_count=1,
                discovered_modules=("tests.test_sample",),
            )

        self.assertEqual(payload["record_type"], TIMING_RECORD_TYPE)
        self.assertEqual(
            payload["modules"],
            {"tests.test_sample": {"seconds": 1.5, "tests_run": 2}},
        )


def _write_test_package(root: Path, *, package_name: str = "tests") -> Path:
    tests_directory = root / package_name
    tests_directory.mkdir()
    (tests_directory / "__init__.py").write_text("", encoding="utf-8")
    (tests_directory / "test_sample.py").write_text(
        "import unittest\n\n"
        "class SampleTests(unittest.TestCase):\n"
        "    def test_passes(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    return tests_directory
