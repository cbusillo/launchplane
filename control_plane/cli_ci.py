from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import click

from control_plane.unittest_sharding import (
    Shard,
    ShardPlan,
    ShardRunSummary,
    TIMING_SCHEMA_VERSION,
    UnittestShardingError,
    aggregate_shard_timings,
    discover_test_modules,
    discover_test_targets,
    plan_shards,
    read_json_object,
    read_module_timings,
    run_test_modules,
    utc_timestamp,
    write_json_object,
    write_shard_run_summary,
)

CI_UNITTEST_SHARD_COUNT = 12
CI_UNITTEST_MAX_TESTS_PER_TARGET = 20
CI_UNITTEST_MAX_SECONDS_PER_TARGET = 30.0
DEFAULT_LOCAL_TIMINGS_FILE = Path(".ci-cache/unittest-timings/history.json")
LOCAL_RUN_RECORD_TYPE = "unittest_local_shard_run"
LOCAL_PROCESS_POLL_SECONDS = 0.05
LOCAL_PROCESS_TERMINATE_SECONDS = 5.0
POSTGRES_INTEGRATION_MODULE = "tests.test_postgres_integration"


@dataclass(frozen=True)
class LocalShardProcess:
    shard_index: int
    process: subprocess.Popen[str]
    log_file: Path


def register_ci_commands(main: click.Group) -> None:
    main.add_command(ci)


@click.group()
def ci() -> None:
    """CI support commands."""


@ci.command("postgres-integration")
@click.option(
    "--database-url",
    envvar="LAUNCHPLANE_TEST_POSTGRES_URL",
    default="",
    help=(
        "Root PostgreSQL service URL used to create an isolated test database. "
        "Defaults to LAUNCHPLANE_TEST_POSTGRES_URL."
    ),
)
@click.option("--verbosity", type=int, default=2, show_default=True)
def run_postgres_integration_tests(database_url: str, verbosity: int) -> None:
    """Run real-PostgreSQL schema and concurrency integration tests."""
    normalized_database_url = database_url.strip()
    if not normalized_database_url:
        raise click.ClickException(
            "Set LAUNCHPLANE_TEST_POSTGRES_URL or pass --database-url with a "
            "PostgreSQL service URL. The test harness creates an isolated database."
        )
    environment = os.environ.copy()
    environment["LAUNCHPLANE_TEST_POSTGRES_URL"] = normalized_database_url
    command = [
        sys.executable,
        "-m",
        "unittest",
        "-v" if verbosity > 1 else "",
        POSTGRES_INTEGRATION_MODULE,
    ]
    command = [argument for argument in command if argument]
    result = subprocess.run(command, cwd=Path.cwd(), env=environment, check=False)
    if result.returncode != 0:
        raise click.ClickException("PostgreSQL integration tests failed")


@ci.group("unittest-shard")
def unittest_shard() -> None:
    """Plan and run unittest shards."""


@unittest_shard.command("list")
@click.option(
    "--start-directory",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path("tests"),
    show_default=True,
)
@click.option("--pattern", default="test*.py", show_default=True)
def list_unittest_modules(start_directory: Path, pattern: str) -> None:
    """List discovered unittest modules."""
    try:
        modules = discover_test_modules(start_directory=start_directory, pattern=pattern)
    except UnittestShardingError as error:
        raise click.ClickException(str(error)) from error
    for module_name in modules:
        click.echo(module_name)


@unittest_shard.command("plan")
@click.option("--shard-count", type=int, required=True)
@click.option(
    "--timings-file",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
)
@click.option(
    "--start-directory",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path("tests"),
    show_default=True,
)
@click.option("--pattern", default="test*.py", show_default=True)
@click.option("--max-tests-per-target", type=int, default=100, show_default=True)
@click.option("--max-seconds-per-target", type=float, default=60.0, show_default=True)
@click.option(
    "--import-root",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path("."),
    show_default=True,
)
def plan_unittest_shards(
    shard_count: int,
    timings_file: Path | None,
    start_directory: Path,
    pattern: str,
    max_tests_per_target: int,
    max_seconds_per_target: float,
    import_root: Path,
) -> None:
    """Print a deterministic unittest shard plan."""
    try:
        timings = read_module_timings(timings_file)
        modules = discover_test_targets(
            start_directory=start_directory,
            pattern=pattern,
            import_root=import_root,
            max_tests_per_target=max_tests_per_target,
            max_seconds_per_target=max_seconds_per_target,
            module_seconds=timings,
        )
        shard_plan = plan_shards(modules, shard_count=shard_count, module_seconds=timings)
    except UnittestShardingError as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(shard_plan.as_payload(), indent=2, sort_keys=True))


@unittest_shard.command("run")
@click.option("--shard-count", type=int, required=True)
@click.option("--shard-index", type=int, required=True)
@click.option(
    "--timings-file",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
)
@click.option(
    "--timings-output",
    type=click.Path(path_type=Path, dir_okay=False),
    required=True,
)
@click.option(
    "--start-directory",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path("tests"),
    show_default=True,
)
@click.option("--pattern", default="test*.py", show_default=True)
@click.option("--max-tests-per-target", type=int, default=100, show_default=True)
@click.option("--max-seconds-per-target", type=float, default=60.0, show_default=True)
@click.option(
    "--import-root",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path("."),
    show_default=True,
)
@click.option("--verbosity", type=int, default=2, show_default=True)
def run_unittest_shard(
    shard_count: int,
    shard_index: int,
    timings_file: Path | None,
    timings_output: Path,
    start_directory: Path,
    pattern: str,
    max_tests_per_target: int,
    max_seconds_per_target: float,
    import_root: Path,
    verbosity: int,
) -> None:
    """Run one unittest shard and write its timing artifact."""
    try:
        timings = read_module_timings(timings_file)
        modules = discover_test_targets(
            start_directory=start_directory,
            pattern=pattern,
            import_root=import_root,
            max_tests_per_target=max_tests_per_target,
            max_seconds_per_target=max_seconds_per_target,
            module_seconds=timings,
        )
        shard = plan_shards(modules, shard_count=shard_count, module_seconds=timings).shard(
            shard_index
        )
        click.echo(
            json.dumps(
                {
                    "shard_count": shard_count,
                    "shard_index": shard_index,
                    "modules": list(shard.modules),
                },
                indent=2,
                sort_keys=True,
            )
        )
        summary = _run_and_write_unittest_shard(
            targets=shard.modules,
            shard_index=shard_index,
            shard_count=shard_count,
            import_root=import_root,
            timings_output=timings_output,
            verbosity=verbosity,
        )
    except UnittestShardingError as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(summary.as_payload(), indent=2, sort_keys=True))
    if not summary.successful:
        raise click.ClickException(f"unittest shard {shard_index} failed")


@unittest_shard.command("run-targets", hidden=True)
@click.option("--shard-count", type=int, required=True)
@click.option("--shard-index", type=int, required=True)
@click.option(
    "--timings-output",
    type=click.Path(path_type=Path, dir_okay=False),
    required=True,
)
@click.option(
    "--import-root",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path("."),
    show_default=True,
)
@click.option("--verbosity", type=int, default=2, show_default=True)
@click.option(
    "--targets-file",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    required=True,
)
def run_unittest_targets(
    shard_count: int,
    shard_index: int,
    timings_output: Path,
    import_root: Path,
    verbosity: int,
    targets_file: Path,
) -> None:
    """Run exact unittest targets for the local shard orchestrator."""
    try:
        targets = _read_unittest_targets_file(
            targets_file,
            shard_index=shard_index,
            shard_count=shard_count,
        )
        summary = _run_and_write_unittest_shard(
            targets=targets,
            shard_index=shard_index,
            shard_count=shard_count,
            import_root=import_root,
            timings_output=timings_output,
            verbosity=verbosity,
        )
    except UnittestShardingError as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(summary.as_payload(), indent=2, sort_keys=True))
    if not summary.successful:
        raise click.ClickException(f"unittest shard {shard_index} failed")


@unittest_shard.command("local")
@click.option(
    "--shard-count",
    type=click.IntRange(min=1),
    default=CI_UNITTEST_SHARD_COUNT,
    show_default=True,
)
@click.option(
    "--jobs",
    type=click.IntRange(min=1),
    default=None,
    help="Concurrent shard processes; defaults to detected CPUs up to the shard count.",
)
@click.option(
    "--timings-file",
    type=click.Path(path_type=Path, dir_okay=False),
    default=DEFAULT_LOCAL_TIMINGS_FILE,
    show_default=True,
)
@click.option(
    "--timings-output",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Aggregate timing output; defaults to --timings-file.",
)
@click.option(
    "--start-directory",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path("tests"),
    show_default=True,
)
@click.option("--pattern", default="test*.py", show_default=True)
@click.option(
    "--max-tests-per-target",
    type=click.IntRange(min=1),
    default=CI_UNITTEST_MAX_TESTS_PER_TARGET,
    show_default=True,
)
@click.option(
    "--max-seconds-per-target",
    type=click.FloatRange(min=0.0, min_open=True),
    default=CI_UNITTEST_MAX_SECONDS_PER_TARGET,
    show_default=True,
)
@click.option(
    "--import-root",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path("."),
    show_default=True,
)
@click.option("--verbosity", type=int, default=2, show_default=True)
def run_local_unittest_suite(
    shard_count: int,
    jobs: int | None,
    timings_file: Path,
    timings_output: Path | None,
    start_directory: Path,
    pattern: str,
    max_tests_per_target: int,
    max_seconds_per_target: float,
    import_root: Path,
    verbosity: int,
) -> None:
    """Run the complete unittest suite through the CI shard topology."""
    started_at = time.monotonic()
    resolved_timings_output = timings_output or timings_file
    try:
        timings = read_module_timings(timings_file)
        targets = discover_test_targets(
            start_directory=start_directory,
            pattern=pattern,
            import_root=import_root,
            max_tests_per_target=max_tests_per_target,
            max_seconds_per_target=max_seconds_per_target,
            module_seconds=timings,
        )
        shard_plan = plan_shards(targets, shard_count=shard_count, module_seconds=timings)
        empty_shards = [shard.index for shard in shard_plan.shards if not shard.modules]
        if empty_shards:
            empty_list = ", ".join(str(shard_index) for shard_index in empty_shards)
            raise UnittestShardingError(
                f"local shard plan contains empty shards ({empty_list}); reduce --shard-count"
            )
        worker_count = _local_worker_count(shard_count=shard_count, jobs=jobs)
        click.echo(
            f"Running {shard_count} unittest shards with {worker_count} concurrent workers.",
            err=True,
        )
        with tempfile.TemporaryDirectory(prefix="launchplane-unittest-shards-") as run_directory:
            results_directory = Path(run_directory) / "results"
            logs_directory = Path(run_directory) / "logs"
            targets_directory = Path(run_directory) / "targets"
            return_codes = _run_local_shard_processes(
                shard_plan=shard_plan,
                worker_count=worker_count,
                results_directory=results_directory,
                logs_directory=logs_directory,
                targets_directory=targets_directory,
                import_root=import_root,
                verbosity=verbosity,
            )
            failed_shards = sorted(
                shard_index for shard_index, return_code in return_codes.items() if return_code != 0
            )
            payload: dict[str, object] = {
                "schema_version": TIMING_SCHEMA_VERSION,
                "record_type": LOCAL_RUN_RECORD_TYPE,
                "generated_at": utc_timestamp(),
                "successful": not failed_shards,
                "shard_count": shard_count,
                "jobs": worker_count,
                "target_count": len(targets),
                "failed_shards": failed_shards,
                "elapsed_seconds": round(time.monotonic() - started_at, 6),
                "timings_file": str(timings_file),
                "timings_output": str(resolved_timings_output),
                "shards": [
                    {"index": shard_index, "return_code": return_codes[shard_index]}
                    for shard_index in sorted(return_codes)
                ],
            }
            if failed_shards:
                click.echo(json.dumps(payload, indent=2, sort_keys=True))
                failed_list = ", ".join(str(shard_index) for shard_index in failed_shards)
                raise click.ClickException(f"local unittest shards failed: {failed_list}")

            aggregate_payload = aggregate_shard_timings(
                results_directory=results_directory,
                shard_count=shard_count,
                discovered_modules=targets,
            )
            _write_json_object_atomically(resolved_timings_output, aggregate_payload)
            click.echo(json.dumps(payload, indent=2, sort_keys=True))
    except UnittestShardingError as error:
        raise click.ClickException(str(error)) from error


@unittest_shard.command("aggregate")
@click.option("--shard-count", type=int, required=True)
@click.option(
    "--results-dir",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    required=True,
)
@click.option(
    "--timings-output",
    type=click.Path(path_type=Path, dir_okay=False),
    required=True,
)
@click.option(
    "--timings-file",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
)
@click.option(
    "--start-directory",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path("tests"),
    show_default=True,
)
@click.option("--pattern", default="test*.py", show_default=True)
@click.option("--max-tests-per-target", type=int, default=100, show_default=True)
@click.option("--max-seconds-per-target", type=float, default=60.0, show_default=True)
@click.option(
    "--import-root",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path("."),
    show_default=True,
)
def aggregate_unittest_shards(
    shard_count: int,
    results_dir: Path,
    timings_output: Path,
    timings_file: Path | None,
    start_directory: Path,
    pattern: str,
    max_tests_per_target: int,
    max_seconds_per_target: float,
    import_root: Path,
) -> None:
    """Aggregate shard timing artifacts into a next-run timing file."""
    try:
        timings = read_module_timings(timings_file)
        modules = discover_test_targets(
            start_directory=start_directory,
            pattern=pattern,
            import_root=import_root,
            max_tests_per_target=max_tests_per_target,
            max_seconds_per_target=max_seconds_per_target,
            module_seconds=timings,
        )
        payload = aggregate_shard_timings(
            results_directory=results_dir,
            shard_count=shard_count,
            discovered_modules=modules,
        )
        write_json_object(timings_output, payload)
    except UnittestShardingError as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


def _run_and_write_unittest_shard(
    *,
    targets: tuple[str, ...],
    shard_index: int,
    shard_count: int,
    import_root: Path,
    timings_output: Path,
    verbosity: int,
) -> ShardRunSummary:
    summary = run_test_modules(
        targets,
        shard_index=shard_index,
        shard_count=shard_count,
        import_root=import_root,
        stream=sys.stderr,
        verbosity=verbosity,
    )
    write_shard_run_summary(summary, timings_output)
    return summary


def _local_worker_count(*, shard_count: int, jobs: int | None) -> int:
    if jobs is not None:
        return min(shard_count, jobs)
    return min(shard_count, os.process_cpu_count() or 1)


def _run_local_shard_processes(
    *,
    shard_plan: ShardPlan,
    worker_count: int,
    results_directory: Path,
    logs_directory: Path,
    targets_directory: Path,
    import_root: Path,
    verbosity: int,
) -> dict[int, int]:
    results_directory.mkdir(parents=True, exist_ok=True)
    logs_directory.mkdir(parents=True, exist_ok=True)
    targets_directory.mkdir(parents=True, exist_ok=True)
    pending_shards = list(shard_plan.shards)
    active_processes: dict[int, LocalShardProcess] = {}
    return_codes: dict[int, int] = {}
    try:
        while pending_shards or active_processes:
            while pending_shards and len(active_processes) < worker_count:
                shard = pending_shards.pop(0)
                local_process = _start_local_shard_process(
                    shard=shard,
                    shard_count=shard_plan.shard_count,
                    results_directory=results_directory,
                    logs_directory=logs_directory,
                    targets_directory=targets_directory,
                    import_root=import_root,
                    verbosity=verbosity,
                )
                active_processes[shard.index] = local_process
                click.echo(f"Started unittest shard {shard.index}.", err=True)

            completed = False
            for shard_index, local_process in tuple(active_processes.items()):
                return_code = local_process.process.poll()
                if return_code is None:
                    continue
                completed = True
                return_codes[shard_index] = return_code
                del active_processes[shard_index]
                if return_code == 0:
                    click.echo(f"Unittest shard {shard_index} passed.", err=True)
                    continue
                click.echo(f"Unittest shard {shard_index} failed.", err=True)
                _echo_local_shard_log(local_process.log_file, shard_index=shard_index)
            if not completed and active_processes:
                time.sleep(LOCAL_PROCESS_POLL_SECONDS)
    except KeyboardInterrupt as error:
        _terminate_local_shard_processes(active_processes.values())
        raise UnittestShardingError("local unittest run interrupted") from error
    except OSError as error:
        _terminate_local_shard_processes(active_processes.values())
        raise UnittestShardingError(f"could not start local unittest shard: {error}") from error
    return return_codes


def _start_local_shard_process(
    *,
    shard: Shard,
    shard_count: int,
    results_directory: Path,
    logs_directory: Path,
    targets_directory: Path,
    import_root: Path,
    verbosity: int,
) -> LocalShardProcess:
    timings_output = results_directory / f"shard-{shard.index}.json"
    log_file = logs_directory / f"shard-{shard.index}.log"
    targets_file = targets_directory / f"shard-{shard.index}.json"
    write_json_object(
        targets_file,
        {
            "schema_version": TIMING_SCHEMA_VERSION,
            "record_type": "unittest_shard_targets",
            "shard_index": shard.index,
            "shard_count": shard_count,
            "targets": list(shard.modules),
        },
    )
    command = [
        sys.executable,
        "-m",
        "control_plane",
        "ci",
        "unittest-shard",
        "run-targets",
        "--shard-count",
        str(shard_count),
        "--shard-index",
        str(shard.index),
        "--timings-output",
        str(timings_output),
        "--import-root",
        str(import_root),
        "--verbosity",
        str(verbosity),
        "--targets-file",
        str(targets_file),
    ]
    with log_file.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            command,
            cwd=Path.cwd(),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
    return LocalShardProcess(shard_index=shard.index, process=process, log_file=log_file)


def _read_unittest_targets_file(
    targets_file: Path,
    *,
    shard_index: int,
    shard_count: int,
) -> tuple[str, ...]:
    payload = read_json_object(targets_file)
    if payload.get("schema_version") != TIMING_SCHEMA_VERSION:
        raise UnittestShardingError(f"unsupported target schema version in {targets_file}")
    if payload.get("record_type") != "unittest_shard_targets":
        raise UnittestShardingError(f"unexpected target record type in {targets_file}")
    if payload.get("shard_index") != shard_index:
        raise UnittestShardingError(f"target file has wrong shard index: {targets_file}")
    if payload.get("shard_count") != shard_count:
        raise UnittestShardingError(f"target file has wrong shard count: {targets_file}")
    targets_payload = payload.get("targets")
    if not isinstance(targets_payload, list) or not targets_payload:
        raise UnittestShardingError(
            f"target file must contain a non-empty target list: {targets_file}"
        )
    if not all(isinstance(target, str) and target for target in targets_payload):
        raise UnittestShardingError(f"target file contains an invalid target: {targets_file}")
    return tuple(targets_payload)


def _write_json_object_atomically(output_file: Path, payload: dict[str, object]) -> None:
    temporary_file = output_file.with_name(f".{output_file.name}.{os.getpid()}.tmp")
    try:
        write_json_object(temporary_file, payload)
        temporary_file.replace(output_file)
    finally:
        temporary_file.unlink(missing_ok=True)


def _echo_local_shard_log(log_file: Path, *, shard_index: int) -> None:
    output = log_file.read_text(encoding="utf-8").strip()
    if not output:
        return
    click.echo(f"--- unittest shard {shard_index} output ---", err=True)
    click.echo(output, err=True)


def _terminate_local_shard_processes(processes: Iterable[LocalShardProcess]) -> None:
    local_processes = tuple(processes)
    for local_process in local_processes:
        if local_process.process.poll() is None:
            local_process.process.terminate()
    deadline = time.monotonic() + LOCAL_PROCESS_TERMINATE_SECONDS
    for local_process in local_processes:
        if local_process.process.poll() is not None:
            continue
        try:
            local_process.process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            local_process.process.kill()
            local_process.process.wait()
