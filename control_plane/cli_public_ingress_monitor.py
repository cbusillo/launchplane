import json
from pathlib import Path

import click

from control_plane.workflows.public_ingress_monitor import (
    build_github_issue_notifier,
    run_public_ingress_monitor_once,
)


def register_public_ingress_monitor_commands(
    main: click.Group,
    *,
    store_factory: object,
) -> None:
    @main.group("public-ingress-monitor")
    def public_ingress_monitor() -> None:
        """Run public ingress synthetic checks."""

    @public_ingress_monitor.command("run-once")
    @click.option("--state-dir", type=click.Path(path_type=Path), default=Path("state"))
    @click.option("--database-url", default="", help="Launchplane database URL.")
    @click.option("--timeout-seconds", default=10, type=click.IntRange(min=1, max=120))
    @click.option("--notify/--no-notify", default=True, show_default=True)
    def run_once(
        state_dir: Path,
        database_url: str,
        timeout_seconds: int,
        notify: bool,
    ) -> None:
        factory = store_factory
        if not callable(factory):
            raise click.ClickException("public ingress monitor requires a store factory.")
        record_store = factory(state_dir, database_url=database_url)
        notifier = build_github_issue_notifier() if notify else None
        result = run_public_ingress_monitor_once(
            record_store=record_store,
            timeout_seconds=timeout_seconds,
            notify=notify,
            notifier=notifier,
        )
        click.echo(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
