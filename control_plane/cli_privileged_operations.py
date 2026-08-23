from __future__ import annotations

import json
import time

import click

from control_plane.cli_shared import DATABASE_URL_ENV_KEYS
from control_plane.privileged_operation_worker import execute_approved_privileged_operations_once
from control_plane.storage.postgres import PostgresRecordStore


def register_privileged_operation_commands(main: click.Group) -> None:
    main.add_command(privileged_operations)


@click.group("privileged-operations")
def privileged_operations() -> None:
    """Service-internal governed privileged-operation commands."""


@privileged_operations.command("worker")
@click.option("--database-url", envvar=DATABASE_URL_ENV_KEYS, required=True)
@click.option("--once", "run_once", is_flag=True, default=False)
@click.option("--poll-seconds", type=click.IntRange(min=1, max=300), default=15, show_default=True)
@click.option("--limit", type=click.IntRange(min=1, max=100), default=20, show_default=True)
def privileged_operation_worker(
    database_url: str, run_once: bool, poll_seconds: int, limit: int
) -> None:
    """Claim and execute approved operations; this is not an operator execute command."""

    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    try:
        while True:
            records = execute_approved_privileged_operations_once(record_store=store, limit=limit)
            click.echo(
                json.dumps(
                    {"processed": len(records), "statuses": [record.status for record in records]},
                    sort_keys=True,
                )
            )
            if run_once:
                return
            time.sleep(poll_seconds)
    finally:
        store.close()
