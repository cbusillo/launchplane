import json
import click

from control_plane import secrets as control_plane_secrets
from control_plane.cli_shared import (
    DATABASE_URL_ENV_KEYS as _DATABASE_URL_ENV_KEYS,
    direct_db_mutation_acknowledgement_option as _direct_db_mutation_acknowledgement_option,
    require_direct_db_mutation_acknowledgement as _require_direct_db_mutation_acknowledgement,
)
from control_plane.contracts.secret_record import SecretScope
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.provider_target_audit import audit_provider_targets
from control_plane.workflows.provider_target_backfill import backfill_provider_targets


_SECRET_WRITE_DIRECT_DB_MUTATION_MESSAGE = (
    "Direct local DB mutation is restricted after the Launchplane service boundary. "
    "Use the deployed service route or operator workflow for shared/production "
    "secret writes, or pass --allow-direct-db-mutation only for explicit "
    "local/bootstrap repair."
)


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
@click.option("--provider-id", default="", help="Optional provider id filter.")
@click.option("--context", "context_name", default="", help="Optional context filter.")
@click.option("--instance", "instance_name", default="", help="Optional instance filter.")
def storage_provider_target_backfill(
    database_url: str,
    provider_id: str,
    context_name: str,
    instance_name: str,
) -> None:
    postgres_store = PostgresRecordStore(database_url=database_url)
    try:
        postgres_store.ensure_schema()
        result = backfill_provider_targets(
            postgres_store,
            apply=False,
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
@_direct_db_mutation_acknowledgement_option
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
    allow_direct_db_mutation: bool,
) -> None:
    _require_direct_db_mutation_acknowledgement(
        allow_direct_db_mutation,
        message=_SECRET_WRITE_DIRECT_DB_MUTATION_MESSAGE,
    )
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


@secrets.command("reencrypt")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane managed secrets.",
)
@click.option(
    "--apply",
    is_flag=True,
    default=False,
    help="Apply the re-encryption rather than just dry-running.",
)
@click.option(
    "--expected-plan-digest",
    default="",
    help="Matching digest emitted by a successful dry-run.",
)
@click.option(
    "--reason",
    default="",
    help="Operator reason recorded with an applied re-encryption.",
)
@_direct_db_mutation_acknowledgement_option
def secrets_reencrypt(
    database_url: str,
    apply: bool,
    expected_plan_digest: str,
    reason: str,
    allow_direct_db_mutation: bool,
) -> None:
    _require_direct_db_mutation_acknowledgement(
        allow_direct_db_mutation,
        message=_SECRET_WRITE_DIRECT_DB_MUTATION_MESSAGE,
    )
    postgres_store = PostgresRecordStore(database_url=database_url)
    postgres_store.ensure_schema()
    try:
        result = control_plane_secrets.reencrypt_secrets(
            record_store=postgres_store,
            apply=apply,
            expected_plan_digest=expected_plan_digest,
            reason=reason,
        )
    finally:
        postgres_store.close()

    click.echo(json.dumps(result, indent=2, sort_keys=True))
    if result["status"] == "error":
        raise click.exceptions.Exit(1)
