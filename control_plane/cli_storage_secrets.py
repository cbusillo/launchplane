import json
from pathlib import Path
import click

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.secret_record import SecretScope
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.provider_target_audit import audit_provider_targets
from control_plane.workflows.provider_target_backfill import backfill_provider_targets


_DATABASE_URL_ENV_KEYS = ("LAUNCHPLANE_DATABASE_URL",)


def register_storage_secret_commands(main: click.Group) -> None:
    main.add_command(storage)
    main.add_command(secrets)


@click.group()
def storage() -> None:
    """Launchplane storage commands."""


@click.group()
def secrets() -> None:
    """Launchplane managed secret commands."""


def normalize_secret_scope(scope: str) -> SecretScope:
    normalized_scope = scope.strip().lower()
    choices: dict[str, SecretScope] = {
        "global": "global",
        "context": "context",
        "context_instance": "context_instance",
    }
    try:
        return choices[normalized_scope]
    except KeyError as exc:
        raise click.ClickException(
            "Secret scope must be one of global, context, or context_instance."
        ) from exc


@storage.command("import-core-records")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane shared-service core records.",
)
def storage_import_core_records(state_dir: Path, database_url: str) -> None:
    filesystem_store = FilesystemRecordStore(state_dir=state_dir)
    postgres_store = PostgresRecordStore(database_url=database_url)
    postgres_store.ensure_schema()
    counts = postgres_store.import_core_records_from_filesystem(filesystem_store)
    click.echo(json.dumps({"status": "ok", "counts": counts}, indent=2, sort_keys=True))


@storage.command("provider-target-audit")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane provider-target records.",
)
@click.option("--provider-id", default="", help="Optional provider id filter.")
@click.option("--context", "context_name", default="", help="Optional context filter.")
@click.option("--instance", "instance_name", default="", help="Optional instance filter.")
def storage_provider_target_audit(
    database_url: str,
    provider_id: str,
    context_name: str,
    instance_name: str,
) -> None:
    postgres_store = PostgresRecordStore(database_url=database_url)
    try:
        postgres_store.ensure_schema()
        result = audit_provider_targets(
            postgres_store,
            provider_id=provider_id,
            context_name=context_name,
            instance_name=instance_name,
        )
        click.echo(json.dumps(result.to_report_payload(), indent=2, sort_keys=True))
    finally:
        postgres_store.close()
    if not result.ok:
        raise click.exceptions.Exit(1)


@storage.command("provider-target-backfill")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane provider-target records.",
)
@click.option("--apply", "apply_changes", is_flag=True, help="Write missing rows.")
@click.option("--provider-id", default="", help="Optional provider id filter.")
@click.option("--context", "context_name", default="", help="Optional context filter.")
@click.option("--instance", "instance_name", default="", help="Optional instance filter.")
def storage_provider_target_backfill(
    database_url: str,
    apply_changes: bool,
    provider_id: str,
    context_name: str,
    instance_name: str,
) -> None:
    postgres_store = PostgresRecordStore(database_url=database_url)
    try:
        postgres_store.ensure_schema()
        result = backfill_provider_targets(
            postgres_store,
            apply=apply_changes,
            provider_id=provider_id,
            context_name=context_name,
            instance_name=instance_name,
        )
        click.echo(json.dumps(result.to_report_payload(), indent=2, sort_keys=True))
    finally:
        postgres_store.close()
    if not result.ok:
        raise click.exceptions.Exit(1)


@secrets.command("put")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane managed secrets.",
)
@click.option(
    "--scope", type=click.Choice(["global", "context", "context_instance"]), required=True
)
@click.option("--integration", required=True)
@click.option("--name", required=True)
@click.option("--binding-key", required=True)
@click.option("--value", required=True)
@click.option("--context", "context_name", default="")
@click.option("--instance", "instance_name", default="")
@click.option("--description", default="")
@click.option("--actor", default="cli", show_default=True)
def secrets_put(
    database_url: str,
    scope: str,
    integration: str,
    name: str,
    binding_key: str,
    value: str,
    context_name: str,
    instance_name: str,
    description: str,
    actor: str,
) -> None:
    postgres_store = PostgresRecordStore(database_url=database_url)
    postgres_store.ensure_schema()
    try:
        result = control_plane_secrets.write_secret_value(
            record_store=postgres_store,
            scope=normalize_secret_scope(scope),
            integration=integration,
            name=name,
            plaintext_value=value,
            binding_key=binding_key,
            context_name=context_name,
            instance_name=instance_name,
            description=description,
            actor=actor,
        )
        payload = control_plane_secrets.build_secret_status(
            postgres_store, secret_id=result["secret_id"]
        )
    finally:
        postgres_store.close()
    click.echo(
        json.dumps({"status": "ok", "result": result, "secret": payload}, indent=2, sort_keys=True)
    )


@secrets.command("list")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane managed secrets.",
)
@click.option("--integration", default="")
@click.option("--context", "context_name", default="")
@click.option("--instance", "instance_name", default="")
def secrets_list(
    database_url: str, integration: str, context_name: str, instance_name: str
) -> None:
    postgres_store = PostgresRecordStore(database_url=database_url)
    try:
        payload = control_plane_secrets.list_secret_statuses(
            postgres_store,
            integration=integration,
            context_name=context_name,
            instance_name=instance_name,
        )
    finally:
        postgres_store.close()
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@secrets.command("show")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane managed secrets.",
)
@click.option("--secret-id", required=True)
def secrets_show(database_url: str, secret_id: str) -> None:
    postgres_store = PostgresRecordStore(database_url=database_url)
    try:
        payload = control_plane_secrets.build_secret_status(postgres_store, secret_id=secret_id)
    finally:
        postgres_store.close()
    click.echo(json.dumps(payload, indent=2, sort_keys=True))
