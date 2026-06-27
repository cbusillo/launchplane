from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import sys
import time
from pathlib import Path
from types import TracebackType
from typing import Any, Iterable, TextIO
import unittest

TIMING_SCHEMA_VERSION = 1
TIMING_RECORD_TYPE = "unittest_module_timings"
SHARD_RUN_RECORD_TYPE = "unittest_shard_run"
DEFAULT_MODULE_SECONDS = 1.0


class UnittestShardingError(ValueError):
    """Raised when unittest sharding input is invalid."""


@dataclass(frozen=True)
class Shard:
    index: int
    modules: tuple[str, ...]
    estimated_seconds: float
    estimate_sources: dict[str, str]


@dataclass(frozen=True)
class ShardPlan:
    shard_count: int
    shards: tuple[Shard, ...]

    def shard(self, shard_index: int) -> Shard:
        validate_shard_index(shard_count=self.shard_count, shard_index=shard_index)
        return self.shards[shard_index]

    def as_payload(self) -> dict[str, object]:
        all_targets = [module_name for shard in self.shards for module_name in shard.modules]
        return {
            "schema_version": TIMING_SCHEMA_VERSION,
            "record_type": "unittest_shard_plan",
            "shard_count": self.shard_count,
            "target_count": len(all_targets),
            "target_granularity": "module_or_unittest_target",
            "shards": [
                {
                    "index": shard.index,
                    "estimated_seconds": round(shard.estimated_seconds, 6),
                    "modules": list(shard.modules),
                    "timing_sources": {
                        module_name: shard.estimate_sources[module_name]
                        for module_name in shard.modules
                    },
                    "timing_source_counts": timing_source_counts(shard.estimate_sources),
                }
                for shard in self.shards
            ],
        }


@dataclass(frozen=True)
class ModuleRunTiming:
    module: str
    seconds: float
    tests_run: int

    def as_payload(self) -> dict[str, object]:
        return {
            "seconds": round(self.seconds, 6),
            "tests_run": self.tests_run,
        }


@dataclass(frozen=True)
class ShardRunSummary:
    shard_index: int
    shard_count: int
    successful: bool
    modules: tuple[ModuleRunTiming, ...]

    def as_payload(self) -> dict[str, object]:
        return {
            "schema_version": TIMING_SCHEMA_VERSION,
            "record_type": SHARD_RUN_RECORD_TYPE,
            "generated_at": utc_timestamp(),
            "shard_index": self.shard_index,
            "shard_count": self.shard_count,
            "successful": self.successful,
            "modules": {
                module_timing.module: module_timing.as_payload() for module_timing in self.modules
            },
        }


class TemporaryImportRoot:
    def __init__(self, import_root: Path) -> None:
        self._import_root = str(import_root.resolve())
        self._inserted = False

    def __enter__(self) -> None:
        if self._import_root not in sys.path:
            sys.path.insert(0, self._import_root)
            self._inserted = True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._inserted:
            try:
                sys.path.remove(self._import_root)
            except ValueError:
                pass


def utc_timestamp() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def validate_shard_count(shard_count: int) -> None:
    if shard_count < 1:
        raise UnittestShardingError("shard count must be at least 1")


def validate_shard_index(*, shard_count: int, shard_index: int) -> None:
    validate_shard_count(shard_count)
    if shard_index < 0 or shard_index >= shard_count:
        raise UnittestShardingError(
            f"shard index must be between 0 and {shard_count - 1}: {shard_index}"
        )


def discover_test_modules(*, start_directory: Path, pattern: str = "test*.py") -> tuple[str, ...]:
    if not start_directory.exists():
        raise UnittestShardingError(f"test start directory does not exist: {start_directory}")
    if not start_directory.is_dir():
        raise UnittestShardingError(f"test start directory is not a directory: {start_directory}")

    modules: list[str] = []
    for test_file in sorted(start_directory.rglob(pattern)):
        if not test_file.is_file() or test_file.name == "__init__.py":
            continue
        modules.append(module_name_for_test_file(test_file, start_directory=start_directory))
    return tuple(dict.fromkeys(modules))


def discover_test_targets(
    *,
    start_directory: Path,
    import_root: Path,
    pattern: str = "test*.py",
    max_tests_per_target: int = 40,
    max_seconds_per_target: float = 60.0,
    module_seconds: dict[str, float] | None = None,
) -> tuple[str, ...]:
    modules = discover_test_modules(start_directory=start_directory, pattern=pattern)
    if max_tests_per_target < 1:
        raise UnittestShardingError("max tests per target must be at least 1")
    if max_seconds_per_target <= 0:
        raise UnittestShardingError("max seconds per target must be greater than zero")
    seconds_by_module = module_seconds or {}

    loader = unittest.TestLoader()
    targets: list[str] = []
    with TemporaryImportRoot(import_root):
        for module_name in modules:
            suite = loader.loadTestsFromName(module_name)
            if contains_failed_test(suite):
                targets.append(module_name)
                continue
            test_case_targets = tuple(iter_test_case_target_ids(suite))
            module_targets = tuple(iter_test_target_ids(suite))
            if not test_case_targets and not module_targets:
                targets.append(module_name)
                continue
            should_split = should_split_module_target(
                module_name=module_name,
                module_targets=module_targets,
                max_tests_per_target=max_tests_per_target,
                max_seconds_per_target=max_seconds_per_target,
                seconds_by_module=seconds_by_module,
            )
            if not should_split:
                targets.append(module_name)
                continue
            targets.extend(
                split_module_targets(
                    test_case_targets,
                    module_targets,
                    max_tests_per_target,
                    loader=loader,
                )
            )
    return tuple(dict.fromkeys(targets))


def split_module_targets(
    test_case_targets: tuple[str, ...],
    method_targets: tuple[str, ...],
    max_tests_per_target: int,
    *,
    loader: unittest.TestLoader | None = None,
) -> tuple[str, ...]:
    tests_by_case: dict[str, list[str]] = {
        test_case_target: [] for test_case_target in test_case_targets
    }
    for method_target in method_targets:
        case_target = ".".join(method_target.split(".")[:-1])
        tests_by_case.setdefault(case_target, []).append(method_target)

    split_targets: list[str] = []
    for test_case_target in sorted(tests_by_case):
        case_methods = tuple(sorted(tests_by_case[test_case_target]))
        if len(case_methods) > max_tests_per_target:
            if loader is not None and not all(
                is_loadable_unittest_target(loader, method_target) for method_target in case_methods
            ):
                split_targets.append(test_case_target)
                continue
            split_targets.extend(case_methods)
            continue
        split_targets.append(test_case_target)
    return tuple(split_targets)


def is_loadable_unittest_target(loader: unittest.TestLoader, target: str) -> bool:
    try:
        suite = loader.loadTestsFromName(target)
    except (AttributeError, ImportError, TypeError, ValueError):
        return False
    return not contains_failed_test(suite) and suite.countTestCases() > 0


def iter_test_case_target_ids(suite: unittest.TestSuite) -> Iterable[str]:
    case_target_ids: set[str] = set()
    for test_id in iter_test_target_ids(suite):
        parts = test_id.split(".")
        if len(parts) < 4:
            continue
        case_target_ids.add(".".join(parts[:-1]))
    return tuple(sorted(case_target_ids))


def should_split_module_target(
    *,
    module_name: str,
    module_targets: tuple[str, ...],
    max_tests_per_target: int,
    max_seconds_per_target: float,
    seconds_by_module: dict[str, float],
) -> bool:
    if len(module_targets) > max_tests_per_target:
        return True
    module_seconds = seconds_by_module.get(module_name)
    if module_seconds is not None and module_seconds > max_seconds_per_target:
        return True
    target_prefix = f"{module_name}."
    return any(target_name.startswith(target_prefix) for target_name in seconds_by_module)


def iter_test_target_ids(suite: unittest.TestSuite) -> Iterable[str]:
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from iter_test_target_ids(test)
            continue
        test_id = test.id()
        if is_failed_test(test):
            continue
        yield test_id


def contains_failed_test(suite: unittest.TestSuite) -> bool:
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            if contains_failed_test(test):
                return True
            continue
        if is_failed_test(test):
            return True
    return False


def is_failed_test(test: unittest.TestCase) -> bool:
    return test.__class__.__name__ == "_FailedTest" or test.id().startswith(
        "unittest.loader._FailedTest."
    )


def module_name_for_test_file(test_file: Path, *, start_directory: Path) -> str:
    try:
        relative_file = test_file.resolve().relative_to(start_directory.resolve().parent)
    except ValueError as error:
        raise UnittestShardingError(
            f"test file must be inside start directory parent: {test_file}"
        ) from error
    return ".".join(relative_file.with_suffix("").parts)


def read_module_timings(timings_file: Path | None) -> dict[str, float]:
    if timings_file is None or not timings_file.exists():
        return {}
    payload = read_json_object(timings_file)
    require_schema(payload, timings_file=timings_file, record_type=TIMING_RECORD_TYPE)
    modules_payload = payload.get("modules")
    if not isinstance(modules_payload, dict):
        raise UnittestShardingError(f"timing file modules must be an object: {timings_file}")

    timings: dict[str, float] = {}
    for module_name, module_payload in modules_payload.items():
        if not isinstance(module_name, str) or not isinstance(module_payload, dict):
            raise UnittestShardingError(f"invalid module timing entry in {timings_file}")
        seconds_value = module_payload.get("seconds")
        if not isinstance(seconds_value, int | float) or seconds_value < 0:
            raise UnittestShardingError(
                f"invalid seconds for module {module_name} in {timings_file}"
            )
        timings[module_name] = float(seconds_value)
    return timings


def plan_shards(
    modules: tuple[str, ...],
    *,
    shard_count: int,
    module_seconds: dict[str, float] | None = None,
) -> ShardPlan:
    validate_shard_count(shard_count)
    seconds_by_module = module_seconds or {}
    estimated_seconds_by_target = estimate_target_seconds(modules, seconds_by_module)
    estimate_sources_by_target = estimate_target_timing_sources(modules, seconds_by_module)
    shard_modules: list[list[str]] = [[] for _ in range(shard_count)]
    shard_estimates = [0.0 for _ in range(shard_count)]

    weighted_modules = sorted(
        tuple(dict.fromkeys(modules)),
        key=lambda module_name: (-estimated_seconds_by_target[module_name], module_name),
    )
    for module_name in weighted_modules:
        shard_index = min(range(shard_count), key=lambda index: (shard_estimates[index], index))
        shard_modules[shard_index].append(module_name)
        shard_estimates[shard_index] += estimated_seconds_by_target[module_name]

    return ShardPlan(
        shard_count=shard_count,
        shards=tuple(
            Shard(
                index=index,
                modules=tuple(sorted(modules_for_shard)),
                estimated_seconds=shard_estimates[index],
                estimate_sources={
                    module_name: estimate_sources_by_target[module_name]
                    for module_name in sorted(modules_for_shard)
                },
            )
            for index, modules_for_shard in enumerate(shard_modules)
        ),
    )


def estimate_target_seconds(
    targets: tuple[str, ...],
    seconds_by_target: dict[str, float],
) -> dict[str, float]:
    estimate_sources = estimate_target_timing_sources(targets, seconds_by_target)
    unique_targets = tuple(dict.fromkeys(targets))
    child_counts_by_module = child_counts_for_parent_timing(unique_targets, seconds_by_target)

    estimates: dict[str, float] = {}
    for target in unique_targets:
        timing_source = estimate_sources[target]
        if timing_source == "exact":
            estimates[target] = seconds_by_target[target]
            continue
        module_name = parent_module_name(target)
        child_count = child_counts_by_module.get(module_name, 0)
        if timing_source == "parent" and child_count > 0:
            estimates[target] = seconds_by_target[module_name] / child_count
            continue
        estimates[target] = DEFAULT_MODULE_SECONDS
    return estimates


def estimate_target_timing_sources(
    targets: tuple[str, ...],
    seconds_by_target: dict[str, float],
) -> dict[str, str]:
    unique_targets = tuple(dict.fromkeys(targets))
    child_counts_by_module = child_counts_for_parent_timing(unique_targets, seconds_by_target)
    estimate_sources: dict[str, str] = {}
    for target in unique_targets:
        if target in seconds_by_target:
            estimate_sources[target] = "exact"
            continue
        module_name = parent_module_name(target)
        if child_counts_by_module.get(module_name, 0) > 0:
            estimate_sources[target] = "parent"
            continue
        estimate_sources[target] = "default"
    return estimate_sources


def child_counts_for_parent_timing(
    targets: tuple[str, ...],
    seconds_by_target: dict[str, float],
) -> dict[str, int]:
    child_counts_by_module: dict[str, int] = {}
    for target in targets:
        module_name = parent_module_name(target)
        if module_name != target and module_name in seconds_by_target:
            child_counts_by_module[module_name] = child_counts_by_module.get(module_name, 0) + 1
    return child_counts_by_module


def timing_source_counts(estimate_sources: dict[str, str]) -> dict[str, int]:
    source_counts: dict[str, int] = {}
    for timing_source in estimate_sources.values():
        source_counts[timing_source] = source_counts.get(timing_source, 0) + 1
    return {timing_source: source_counts[timing_source] for timing_source in sorted(source_counts)}


def parent_module_name(target: str) -> str:
    parts = target.split(".")
    if len(parts) >= 4 and parts[-1].startswith("test_") and parts[-2][:1].isupper():
        return ".".join(parts[:-2])
    if len(parts) >= 3 and parts[-1][:1].isupper():
        return ".".join(parts[:-1])
    return target


def run_test_modules(
    modules: tuple[str, ...],
    *,
    shard_index: int,
    shard_count: int,
    import_root: Path,
    stream: TextIO,
    verbosity: int = 2,
) -> ShardRunSummary:
    validate_shard_index(shard_count=shard_count, shard_index=shard_index)
    if not modules:
        raise UnittestShardingError(f"shard {shard_index} has no test targets")

    loader = unittest.TestLoader()
    module_timings: list[ModuleRunTiming] = []
    successful = True
    with TemporaryImportRoot(import_root):
        for module_name in modules:
            suite = loader.loadTestsFromName(module_name)
            if contains_failed_test(suite):
                raise UnittestShardingError(f"failed to load unittest target: {module_name}")
            started_at = time.monotonic()
            result = unittest.TextTestRunner(stream=stream, verbosity=verbosity).run(suite)
            elapsed_seconds = time.monotonic() - started_at
            successful = successful and result.wasSuccessful()
            module_timings.append(
                ModuleRunTiming(
                    module=module_name,
                    seconds=elapsed_seconds,
                    tests_run=result.testsRun,
                )
            )
    return ShardRunSummary(
        shard_index=shard_index,
        shard_count=shard_count,
        successful=successful,
        modules=tuple(module_timings),
    )


def write_shard_run_summary(summary: ShardRunSummary, output_file: Path) -> None:
    write_json_object(output_file, summary.as_payload())


def aggregate_shard_timings(
    *,
    results_directory: Path,
    shard_count: int,
    discovered_modules: tuple[str, ...],
) -> dict[str, object]:
    validate_shard_count(shard_count)
    discovered_module_set = set(discovered_modules)
    discovered_by_parent_module: dict[str, list[str]] = {}
    for discovered_module in discovered_modules:
        parent_module = parent_module_name(discovered_module)
        if parent_module != discovered_module:
            discovered_by_parent_module.setdefault(parent_module, []).append(discovered_module)
    aggregate_modules: dict[str, dict[str, object]] = {}

    for shard_index in range(shard_count):
        shard_file = results_directory / f"shard-{shard_index}.json"
        if not shard_file.is_file():
            raise UnittestShardingError(f"missing shard timing file: {shard_file}")
        payload = read_json_object(shard_file)
        require_schema(payload, timings_file=shard_file, record_type=SHARD_RUN_RECORD_TYPE)
        if payload.get("shard_index") != shard_index:
            raise UnittestShardingError(f"shard timing file has wrong shard index: {shard_file}")
        if payload.get("shard_count") != shard_count:
            raise UnittestShardingError(f"shard timing file has wrong shard count: {shard_file}")
        if payload.get("successful") is not True:
            raise UnittestShardingError(f"shard timing file reports failed run: {shard_file}")
        modules_payload = payload.get("modules")
        if not isinstance(modules_payload, dict):
            raise UnittestShardingError(f"shard timing modules must be an object: {shard_file}")
        for module_name, module_payload in modules_payload.items():
            target_names: tuple[str, ...]
            if module_name in discovered_module_set:
                target_names = (module_name,)
            else:
                target_names = tuple(discovered_by_parent_module.get(module_name, ()))
            if not target_names:
                continue
            normalized_payload = normalize_module_timing_payload(
                module_name=module_name,
                module_payload=module_payload,
                timings_file=shard_file,
            )
            if len(target_names) > 1:
                normalized_payload = split_module_timing_payload(
                    normalized_payload,
                    split_count=len(target_names),
                )
            for target_name in target_names:
                if target_name in aggregate_modules:
                    raise UnittestShardingError(f"duplicate module timing: {target_name}")
                aggregate_modules[target_name] = normalized_payload

    missing_modules = sorted(discovered_module_set.difference(aggregate_modules))
    if missing_modules:
        missing_list = ", ".join(missing_modules[:5])
        if len(missing_modules) > 5:
            missing_list = f"{missing_list}, ..."
        raise UnittestShardingError(
            f"missing timing records for discovered modules: {missing_list}"
        )

    return {
        "schema_version": TIMING_SCHEMA_VERSION,
        "record_type": TIMING_RECORD_TYPE,
        "generated_at": utc_timestamp(),
        "modules": {
            module_name: aggregate_modules[module_name] for module_name in sorted(aggregate_modules)
        },
    }


def normalize_module_timing_payload(
    *,
    module_name: str,
    module_payload: object,
    timings_file: Path,
) -> dict[str, object]:
    if not isinstance(module_payload, dict):
        raise UnittestShardingError(f"invalid module timing for {module_name} in {timings_file}")
    seconds_value = module_payload.get("seconds")
    tests_run_value = module_payload.get("tests_run")
    if not isinstance(seconds_value, int | float) or seconds_value < 0:
        raise UnittestShardingError(f"invalid seconds for module {module_name} in {timings_file}")
    if not isinstance(tests_run_value, int) or tests_run_value < 0:
        raise UnittestShardingError(f"invalid tests_run for module {module_name} in {timings_file}")
    return {"seconds": round(float(seconds_value), 6), "tests_run": tests_run_value}


def split_module_timing_payload(
    module_payload: dict[str, object],
    *,
    split_count: int,
) -> dict[str, object]:
    seconds_value = module_payload["seconds"]
    tests_run_value = module_payload["tests_run"]
    if not isinstance(seconds_value, int | float):
        raise UnittestShardingError("module seconds must be numeric")
    if not isinstance(tests_run_value, int):
        raise UnittestShardingError("module tests_run must be an integer")
    if split_count < 1:
        raise UnittestShardingError("split count must be at least 1")
    return {
        "seconds": round(float(seconds_value) / split_count, 6),
        "tests_run": max(1, round(tests_run_value / split_count)),
    }


def read_json_object(input_file: Path) -> dict[str, Any]:
    try:
        payload = json.loads(input_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise UnittestShardingError(f"invalid JSON file: {input_file}") from error
    if not isinstance(payload, dict):
        raise UnittestShardingError(f"JSON payload must be an object: {input_file}")
    return payload


def write_json_object(output_file: Path, payload: dict[str, object]) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def require_schema(payload: dict[str, Any], *, timings_file: Path, record_type: str) -> None:
    if payload.get("schema_version") != TIMING_SCHEMA_VERSION:
        raise UnittestShardingError(f"unsupported timing schema version in {timings_file}")
    if payload.get("record_type") != record_type:
        raise UnittestShardingError(f"unexpected timing record type in {timings_file}")
