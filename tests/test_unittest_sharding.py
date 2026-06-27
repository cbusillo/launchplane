import json
from pathlib import Path
import tempfile
import sys
import unittest

from control_plane.unittest_sharding import (
    SHARD_RUN_RECORD_TYPE,
    TIMING_RECORD_TYPE,
    TIMING_SCHEMA_VERSION,
    UnittestShardingError,
    aggregate_shard_timings,
    discover_test_modules,
    discover_test_targets,
    estimate_target_seconds,
    estimate_target_timing_sources,
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

    def test_discover_test_targets_splits_large_modules_by_test_case(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            tests_directory = _write_test_package(root, package_name="target_tests")
            (tests_directory / "test_big.py").write_text(
                "import unittest\n\n"
                "class BigTests(unittest.TestCase):\n"
                "    def test_a(self):\n"
                "        self.assertTrue(True)\n"
                "    def test_b(self):\n"
                "        self.assertTrue(True)\n\n"
                "class SmallTests(unittest.TestCase):\n"
                "    def test_c(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            _remove_imported_package("target_tests")

            targets = discover_test_targets(
                start_directory=tests_directory,
                import_root=root,
                max_tests_per_target=2,
            )

        self.assertIn("target_tests.test_big.BigTests", targets)
        self.assertIn("target_tests.test_big.SmallTests", targets)
        self.assertIn("target_tests.test_sample", targets)

    def test_discover_test_targets_splits_oversized_test_cases_by_method(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            tests_directory = _write_test_package(root, package_name="target_tests")
            (tests_directory / "test_big.py").write_text(
                "import unittest\n\n"
                "class BigTests(unittest.TestCase):\n"
                "    def test_a(self):\n"
                "        self.assertTrue(True)\n"
                "    def test_b(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            _remove_imported_package("target_tests")

            targets = discover_test_targets(
                start_directory=tests_directory,
                import_root=root,
                max_tests_per_target=1,
            )

        self.assertIn("target_tests.test_big.BigTests.test_a", targets)
        self.assertIn("target_tests.test_big.BigTests.test_b", targets)

    def test_discover_test_targets_keeps_small_modules_as_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            tests_directory = _write_test_package(root, package_name="target_tests")
            _remove_imported_package("target_tests")

            targets = discover_test_targets(
                start_directory=tests_directory,
                import_root=root,
                max_tests_per_target=40,
            )

        self.assertEqual(targets, ("target_tests.test_sample",))

    def test_discover_test_targets_splits_timing_known_slow_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            tests_directory = _write_test_package(root, package_name="target_tests")
            _remove_imported_package("target_tests")

            targets = discover_test_targets(
                start_directory=tests_directory,
                import_root=root,
                max_tests_per_target=40,
                max_seconds_per_target=0.25,
                module_seconds={"target_tests.test_sample": 1.0},
            )

        self.assertEqual(targets, ("target_tests.test_sample.SampleTests",))

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

    def test_plan_spreads_split_target_estimates_from_parent_module_timing(self) -> None:
        shard_plan = plan_shards(
            (
                "tests.test_big.BigTests",
                "tests.test_big.SmallTests",
                "tests.test_small",
            ),
            shard_count=2,
            module_seconds={"tests.test_big": 10.0, "tests.test_small": 1.0},
        )

        self.assertEqual(
            tuple(sorted(shard.modules for shard in shard_plan.shards)),
            (("tests.test_big.BigTests", "tests.test_small"), ("tests.test_big.SmallTests",)),
        )

    def test_plan_payload_reports_timing_source_diagnostics(self) -> None:
        shard_plan = plan_shards(
            (
                "tests.test_big.BigTests",
                "tests.test_big.SmallTests",
                "tests.test_fast",
                "tests.test_unknown",
            ),
            shard_count=2,
            module_seconds={"tests.test_big": 10.0, "tests.test_fast": 2.0},
        )

        payload = shard_plan.as_payload()
        timing_sources = {
            target_name: timing_source
            for shard_payload in payload["shards"]
            for target_name, timing_source in shard_payload["timing_sources"].items()
        }
        timing_source_counts = {
            shard_payload["index"]: shard_payload["timing_source_counts"]
            for shard_payload in payload["shards"]
        }

        self.assertEqual(
            timing_sources,
            {
                "tests.test_big.BigTests": "parent",
                "tests.test_big.SmallTests": "parent",
                "tests.test_fast": "exact",
                "tests.test_unknown": "default",
            },
        )
        self.assertEqual(timing_source_counts[0], {"exact": 1, "parent": 1})
        self.assertEqual(timing_source_counts[1], {"default": 1, "parent": 1})

    def test_estimate_target_seconds_prefers_exact_target_timing(self) -> None:
        estimates = estimate_target_seconds(
            ("tests.test_big.BigTests",),
            {
                "tests.test_big": 10.0,
                "tests.test_big.BigTests": 3.0,
            },
        )

        self.assertEqual(estimates["tests.test_big.BigTests"], 3.0)

    def test_estimate_target_timing_sources_explain_estimates(self) -> None:
        estimate_sources = estimate_target_timing_sources(
            (
                "tests.test_big.BigTests",
                "tests.test_big.SmallTests.test_behavior",
                "tests.test_exact",
                "tests.test_unknown",
            ),
            {
                "tests.test_big": 10.0,
                "tests.test_exact": 2.0,
            },
        )

        self.assertEqual(
            estimate_sources,
            {
                "tests.test_big.BigTests": "parent",
                "tests.test_big.SmallTests.test_behavior": "parent",
                "tests.test_exact": "exact",
                "tests.test_unknown": "default",
            },
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
                with self.assertRaisesRegex(UnittestShardingError, "no test targets"):
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

    def test_run_writes_timing_records_for_split_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            _write_test_package(root, package_name="sample_run_tests")
            with (root / "test-output.txt").open("w", encoding="utf-8") as stream:
                summary = run_test_modules(
                    ("sample_run_tests.test_sample.SampleTests",),
                    shard_index=0,
                    shard_count=1,
                    import_root=root,
                    stream=stream,
                )

        self.assertTrue(summary.successful)
        self.assertEqual(summary.modules[0].module, "sample_run_tests.test_sample.SampleTests")
        self.assertEqual(summary.modules[0].tests_run, 1)

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

    def test_aggregate_splits_parent_module_timings_for_new_targets(self) -> None:
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
                        "tests.test_big": {"seconds": 10.0, "tests_run": 4},
                    },
                },
            )

            payload = aggregate_shard_timings(
                results_directory=root,
                shard_count=1,
                discovered_modules=(
                    "tests.test_big.BigTests",
                    "tests.test_big.SmallTests",
                ),
            )

        self.assertEqual(
            payload["modules"],
            {
                "tests.test_big.BigTests": {"seconds": 5.0, "tests_run": 2},
                "tests.test_big.SmallTests": {"seconds": 5.0, "tests_run": 2},
            },
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


def _remove_imported_package(package_name: str) -> None:
    for module_name in tuple(sys.modules):
        if module_name == package_name or module_name.startswith(f"{package_name}."):
            del sys.modules[module_name]
