from __future__ import annotations

import json
from pathlib import Path

import click

from control_plane.first_party_action_pins import (
    ActionPinError,
    ActionPinReport,
    build_action_pin_report,
    update_action_pins,
)


def register_action_pin_commands(main: click.Group) -> None:
    main.add_command(action_pins)


@click.group("action-pins")
def action_pins() -> None:
    """Check and update first-party GitHub Action pins."""


@action_pins.command("check")
@click.option("--repo-root", type=click.Path(path_type=Path, file_okay=False), default=Path("."))
def check_action_pins(repo_root: Path) -> None:
    """Fail when ordinary first-party action pins are stale or unverifiable."""
    report = _load_report(repo_root)
    click.echo(json.dumps(report.as_summary_dict(), indent=2, sort_keys=True))
    if report.violations:
        raise click.exceptions.Exit(1)


@action_pins.command("report")
@click.option("--repo-root", type=click.Path(path_type=Path, file_okay=False), default=Path("."))
def report_action_pins(repo_root: Path) -> None:
    """Emit deterministic first-party action pin evidence."""
    report = _load_report(repo_root)
    click.echo(json.dumps(report.as_dict(), indent=2, sort_keys=True))


@action_pins.command("update")
@click.option("--repo-root", type=click.Path(path_type=Path, file_okay=False), default=Path("."))
@click.option("--release-sha", required=True)
@click.option("--dry-run", is_flag=True)
def update_action_pin_references(repo_root: Path, release_sha: str, dry_run: bool) -> None:
    """Rewrite ordinary consumers to one content-equivalent release SHA."""
    try:
        changed_paths = update_action_pins(
            repo_root,
            release_sha=release_sha,
            dry_run=dry_run,
        )
    except ActionPinError as error:
        raise click.ClickException(str(error)) from error
    click.echo(
        json.dumps(
            {
                "schema_version": 1,
                "status": "dry-run" if dry_run else "updated",
                "release_sha": release_sha,
                "changed_files": [path.as_posix() for path in changed_paths],
            },
            indent=2,
            sort_keys=True,
        )
    )


def _load_report(repo_root: Path) -> ActionPinReport:
    try:
        return build_action_pin_report(repo_root)
    except ActionPinError as error:
        raise click.ClickException(str(error)) from error
