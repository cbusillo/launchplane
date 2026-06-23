from __future__ import annotations

import json
from pathlib import Path
import sys

import click

from control_plane.unittest_sharding import (
    UnittestShardingError,
    aggregate_shard_timings,
    discover_test_modules,
    discover_test_targets,
    plan_shards,
    read_module_timings,
    run_test_modules,
    write_json_object,
    write_shard_run_summary,
)


def register_ci_commands(main: click.Group) -> None:
    main.add_command(ci)


@click.group()
def ci() -> None:
    """CI support commands."""


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
        summary = run_test_modules(
            shard.modules,
            shard_index=shard_index,
            shard_count=shard_count,
            import_root=import_root,
            stream=sys.stderr,
            verbosity=verbosity,
        )
        write_shard_run_summary(summary, timings_output)
    except UnittestShardingError as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(summary.as_payload(), indent=2, sort_keys=True))
    if not summary.successful:
        raise click.ClickException(f"unittest shard {shard_index} failed")


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
