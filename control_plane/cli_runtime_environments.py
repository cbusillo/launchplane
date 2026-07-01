import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
import time

import click

from control_plane import dokploy as control_plane_dokploy
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.runtime_environment_record import (
    RuntimeEnvironmentDeleteEvent,
    RuntimeEnvironmentRecord,
    RuntimeEnvironmentScope,
    ScalarValue,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.tracked_target_logs import build_tracked_target_logs_payload
from control_plane.workflows.ship import utc_now_timestamp


_DATABASE_URL_ENV_KEYS = ("LAUNCHPLANE_DATABASE_URL",)
_DIRECT_DB_MUTATION_MESSAGE = (
    "Direct local DB mutation is restricted after the Launchplane service boundary. "
    "Use the deployed service route or operator workflow for shared/production changes, "
    "or pass --allow-direct-db-mutation only for explicit local/bootstrap repair."
)
_SECRET_SHAPED_RUNTIME_ENV_KEY_PARTS = {"PASSWORD", "TOKEN", "SECRET", "KEY"}
_REDACTED_RUNTIME_ENVIRONMENT_VALUE = "<redacted>"


def register_runtime_environment_commands(
    main: click.Group,
    *,
    control_plane_root: Callable[[], Path],
    build_live_target_runtime_contract_payload: Callable[..., dict[str, object]],
) -> None:
    global _RUNTIME_ENVIRONMENT_CALLBACKS
    _RUNTIME_ENVIRONMENT_CALLBACKS = _RuntimeEnvironmentCallbacks(
        control_plane_root=control_plane_root,
        build_live_target_runtime_contract_payload=build_live_target_runtime_contract_payload,
    )
    main.add_command(environments)


@click.group()
def environments() -> None:
    """Runtime environment contract commands."""


def _direct_db_mutation_acknowledgement_option(
    function: Callable[..., object],
) -> Callable[..., object]:
    return click.option(
        "--allow-direct-db-mutation",
        is_flag=True,
        default=False,
        help="Acknowledge direct local DB mutation for explicit local/bootstrap repair.",
    )(function)


def _require_direct_db_mutation_acknowledgement(allow_direct_db_mutation: bool) -> None:
    if not allow_direct_db_mutation:
        raise click.ClickException(_DIRECT_DB_MUTATION_MESSAGE)


@environments.command("put")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane runtime-environment records.",
)
@click.option("--scope", type=click.Choice(["global", "context", "instance"]), required=True)
@click.option("--context", "context_name", default="")
@click.option("--instance", "instance_name", default="")
@click.option(
    "--set",
    "assignments",
    multiple=True,
    required=True,
    help="Runtime value assignment as KEY=VALUE.",
)
@click.option("--source-label", default="cli", show_default=True)
@_direct_db_mutation_acknowledgement_option
def environments_put(
    database_url: str,
    scope: str,
    context_name: str,
    instance_name: str,
    assignments: tuple[str, ...],
    source_label: str,
    allow_direct_db_mutation: bool,
) -> None:
    _require_direct_db_mutation_acknowledgement(allow_direct_db_mutation)
    postgres_store = PostgresRecordStore(database_url=database_url)
    postgres_store.ensure_schema()
    try:
        existing_records = postgres_store.list_runtime_environment_records()
        record = _build_runtime_environment_record_for_put(
            existing_records=existing_records,
            scope=scope,
            context_name=context_name.strip(),
            instance_name=instance_name.strip(),
            assignments=assignments,
            source_label=source_label,
        )
        postgres_store.write_runtime_environment_record(record)
    finally:
        postgres_store.close()
    click.echo(
        json.dumps(
            {
                "status": "ok",
                "record": summarize_runtime_environment_record(record),
            },
            indent=2,
            sort_keys=True,
        )
    )


@environments.command("unset")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane runtime-environment records.",
)
@click.option("--scope", type=click.Choice(["global", "context", "instance"]), required=True)
@click.option("--context", "context_name", default="")
@click.option("--instance", "instance_name", default="")
@click.option(
    "--key",
    "keys",
    multiple=True,
    required=True,
    help="Runtime value key to remove from the record.",
)
@click.option("--source-label", default="cli", show_default=True)
@_direct_db_mutation_acknowledgement_option
def environments_unset(
    database_url: str,
    scope: str,
    context_name: str,
    instance_name: str,
    keys: tuple[str, ...],
    source_label: str,
    allow_direct_db_mutation: bool,
) -> None:
    _require_direct_db_mutation_acknowledgement(allow_direct_db_mutation)
    postgres_store = PostgresRecordStore(database_url=database_url)
    postgres_store.ensure_schema()
    try:
        existing_records = postgres_store.list_runtime_environment_records()
        record, removed_keys, missing_keys = _build_runtime_environment_record_for_unset(
            existing_records=existing_records,
            scope=scope,
            context_name=context_name.strip(),
            instance_name=instance_name.strip(),
            keys=keys,
            source_label=source_label,
        )
        postgres_store.write_runtime_environment_record(record)
    finally:
        postgres_store.close()
    click.echo(
        json.dumps(
            {
                "status": "ok",
                "record": summarize_runtime_environment_record(record),
                "removed_keys": removed_keys,
                "missing_keys": missing_keys,
            },
            indent=2,
            sort_keys=True,
        )
    )


@environments.command("delete-record")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane runtime-environment records.",
)
@click.option("--scope", type=click.Choice(["global", "context", "instance"]), required=True)
@click.option("--context", "context_name", default="")
@click.option("--instance", "instance_name", default="")
@click.option("--actor", default="", show_default=False)
@click.option("--allow-tracked-target", is_flag=True, default=False)
@click.option("--dry-run", "dry_run", is_flag=True, default=False)
@click.option("--apply", "apply_changes", is_flag=True, default=False)
@_direct_db_mutation_acknowledgement_option
def environments_delete_record(
    database_url: str,
    scope: str,
    context_name: str,
    instance_name: str,
    actor: str,
    allow_tracked_target: bool,
    dry_run: bool,
    apply_changes: bool,
    allow_direct_db_mutation: bool,
) -> None:
    if dry_run == apply_changes:
        raise click.ClickException("Choose exactly one of --dry-run or --apply.")
    if apply_changes:
        _require_direct_db_mutation_acknowledgement(allow_direct_db_mutation)
    normalized_context = context_name.strip()
    normalized_instance = instance_name.strip()
    normalized_scope = _validate_runtime_environment_scope_route(
        scope=scope,
        context_name=normalized_context,
        instance_name=normalized_instance,
    )

    postgres_store = PostgresRecordStore(database_url=database_url)
    postgres_store.ensure_schema()
    try:
        target_record = _find_runtime_environment_record(
            existing_records=postgres_store.list_runtime_environment_records(),
            scope=normalized_scope,
            context_name=normalized_context,
            instance_name=normalized_instance,
        )
        if target_record is None:
            raise click.ClickException(
                "Missing DB-backed runtime environment record for the requested scope."
            )
        protected_targets = _protected_runtime_environment_delete_targets(
            scope=normalized_scope,
            context_name=normalized_context,
            instance_name=normalized_instance,
            target_records=postgres_store.list_dokploy_target_records(),
        )
        if apply_changes and protected_targets and not allow_tracked_target:
            routes = ", ".join(
                f"{target['context']}/{target['instance']}" for target in protected_targets
            )
            raise click.ClickException(
                "Refusing to delete a runtime environment record that can affect tracked "
                f"Dokploy target(s): {routes}. Re-run with --allow-tracked-target only "
                "after confirming the tracked live target should lose this record."
            )
        event = _build_runtime_environment_delete_event(record=target_record, actor=actor)
        if apply_changes:
            delete_status = postgres_store.delete_runtime_environment_record_with_event(
                event=event,
                expected_record=target_record,
            )
            if delete_status == "missing":
                raise click.ClickException(
                    "Runtime environment record disappeared before delete could complete."
                )
            if delete_status == "changed":
                raise click.ClickException(
                    "Runtime environment record changed before delete could complete; "
                    "re-run delete-record after reviewing the current record."
                )
    finally:
        postgres_store.close()

    click.echo(
        json.dumps(
            {
                "status": "ok" if apply_changes else "dry_run",
                "action": "delete_runtime_environment_record",
                "record": summarize_runtime_environment_record(target_record),
                "event": _summarize_runtime_environment_delete_event(event),
                "protected_tracked_targets": protected_targets,
                "requires_allow_tracked_target": bool(
                    protected_targets and not allow_tracked_target
                ),
                "deleted": apply_changes,
            },
            indent=2,
            sort_keys=True,
        )
    )


@environments.command("relabel")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane runtime-environment records.",
)
@click.option("--scope", type=click.Choice(["global", "context", "instance"]), required=True)
@click.option("--context", "context_name", default="")
@click.option("--instance", "instance_name", default="")
@click.option("--source-label", required=True)
@_direct_db_mutation_acknowledgement_option
def environments_relabel(
    database_url: str,
    scope: str,
    context_name: str,
    instance_name: str,
    source_label: str,
    allow_direct_db_mutation: bool,
) -> None:
    _require_direct_db_mutation_acknowledgement(allow_direct_db_mutation)
    postgres_store = PostgresRecordStore(database_url=database_url)
    postgres_store.ensure_schema()
    try:
        existing_records = postgres_store.list_runtime_environment_records()
        record = _build_runtime_environment_record_for_relabel(
            existing_records=existing_records,
            scope=scope,
            context_name=context_name.strip(),
            instance_name=instance_name.strip(),
            source_label=source_label,
        )
        postgres_store.write_runtime_environment_record(record)
    finally:
        postgres_store.close()
    click.echo(
        json.dumps(
            {
                "status": "ok",
                "record": summarize_runtime_environment_record(record),
            },
            indent=2,
            sort_keys=True,
        )
    )


@environments.command("list")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane runtime-environment records.",
)
def environments_list(database_url: str) -> None:
    postgres_store = PostgresRecordStore(database_url=database_url)
    try:
        records = postgres_store.list_runtime_environment_records()
    finally:
        postgres_store.close()
    click.echo(
        json.dumps(
            {
                "status": "ok",
                "count": len(records),
                "records": [summarize_runtime_environment_record(record) for record in records],
            },
            indent=2,
            sort_keys=True,
        )
    )


@environments.command("resolve")
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", default="local", show_default=True)
@click.option("--json-output", is_flag=True, default=False)
@click.option(
    "--include-secret-values",
    is_flag=True,
    default=False,
    help="Print resolved secret-shaped values. Use only in trusted operator shells.",
)
def environments_resolve(
    context_name: str,
    instance_name: str,
    json_output: bool,
    include_secret_values: bool,
) -> None:
    callbacks = _runtime_environment_callbacks()
    environment_values = control_plane_runtime_environments.resolve_runtime_environment_values(
        control_plane_root=callbacks.control_plane_root(),
        context_name=context_name,
        instance_name=instance_name,
    )
    output_values = (
        environment_values
        if include_secret_values
        else _redact_runtime_environment_values(environment_values)
    )
    if json_output:
        click.echo(
            json.dumps(
                {
                    "context": context_name,
                    "instance": instance_name,
                    "environment": output_values,
                    "redacted": not include_secret_values,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    for environment_key in sorted(output_values):
        click.echo(f"{environment_key}={output_values[environment_key]}")


@environments.command("show-live-target")
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
def environments_show_live_target(context_name: str, instance_name: str) -> None:
    callbacks = _runtime_environment_callbacks()
    payload = callbacks.build_live_target_runtime_contract_payload(
        context_name=context_name,
        instance_name=instance_name,
    )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@environments.command("logs")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane tracked target records.",
)
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
@click.option(
    "--lines",
    "line_count",
    type=int,
    default=control_plane_dokploy.DEFAULT_DOKPLOY_LOG_LINE_COUNT,
    show_default=True,
)
@click.option("--since", default="all", show_default=True)
@click.option("--search", default="", show_default=False)
@click.option(
    "--control-plane-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional Launchplane repo root used to resolve Dokploy credentials.",
)
def environments_logs(
    database_url: str,
    context_name: str,
    instance_name: str,
    line_count: int,
    since: str,
    search: str,
    control_plane_root: Path | None,
) -> None:
    callbacks = _runtime_environment_callbacks()
    postgres_store = PostgresRecordStore(database_url=database_url)
    try:
        try:
            payload = build_tracked_target_logs_payload(
                record_store=postgres_store,
                control_plane_root=control_plane_root or callbacks.control_plane_root(),
                context_name=context_name.strip(),
                instance_name=instance_name.strip(),
                line_count=line_count,
                since=since,
                search=search,
            )
        except ValueError as error:
            raise click.ClickException(str(error)) from error
    finally:
        postgres_store.close()
    click.echo(json.dumps({"status": "ok", **payload}, indent=2, sort_keys=True))


def summarize_runtime_environment_record(
    record: RuntimeEnvironmentRecord,
) -> dict[str, object]:
    return {
        "scope": record.scope,
        "context": record.context,
        "instance": record.instance,
        "updated_at": record.updated_at,
        "source_label": record.source_label,
        "env_keys": sorted(record.env.keys()),
        "env_value_count": len(record.env),
    }


def _parse_runtime_environment_assignment(raw_assignment: str) -> tuple[str, str]:
    key_name, separator, value = raw_assignment.partition("=")
    normalized_key = key_name.strip()
    if not separator or not normalized_key:
        raise click.ClickException("Runtime environment values must be provided as KEY=VALUE.")
    if _runtime_environment_key_requires_secret_store(normalized_key):
        raise click.ClickException(
            f"Runtime environment key {normalized_key!r} must be written through "
            "product-config apply for routine changes, or with "
            "launchplane secrets put --allow-direct-db-mutation for explicit "
            "local/bootstrap repair."
        )
    return normalized_key, value


def _runtime_environment_key_requires_secret_store(key_name: str) -> bool:
    return any(
        key_part in _SECRET_SHAPED_RUNTIME_ENV_KEY_PARTS
        for key_part in key_name.strip().upper().split("_")
    )


def _redact_runtime_environment_values(environment_values: dict[str, str]) -> dict[str, str]:
    return {
        key: (
            _REDACTED_RUNTIME_ENVIRONMENT_VALUE
            if _runtime_environment_key_requires_secret_store(key)
            else value
        )
        for key, value in environment_values.items()
    }


def _validate_runtime_environment_scope_route(
    *,
    scope: str,
    context_name: str,
    instance_name: str,
) -> RuntimeEnvironmentScope:
    if scope == "global":
        if context_name or instance_name:
            raise click.ClickException(
                "Global runtime environment records do not accept --context or --instance."
            )
        return "global"
    if scope == "context":
        if not context_name or instance_name:
            raise click.ClickException(
                "Context runtime environment records require --context and do not accept --instance."
            )
        return "context"
    if scope == "instance":
        if not context_name or not instance_name:
            raise click.ClickException(
                "Instance runtime environment records require --context and --instance."
            )
        return "instance"
    raise click.ClickException(f"Unsupported runtime environment scope: {scope}")


def _runtime_environment_record_matches(
    record: RuntimeEnvironmentRecord,
    *,
    scope: str,
    context_name: str,
    instance_name: str,
) -> bool:
    return (
        record.scope == scope
        and record.context == context_name
        and record.instance == instance_name
    )


def _build_runtime_environment_record_for_put(
    *,
    existing_records: tuple[RuntimeEnvironmentRecord, ...],
    scope: str,
    context_name: str,
    instance_name: str,
    assignments: tuple[str, ...],
    source_label: str,
) -> RuntimeEnvironmentRecord:
    normalized_scope = _validate_runtime_environment_scope_route(
        scope=scope,
        context_name=context_name,
        instance_name=instance_name,
    )

    env_values: dict[str, ScalarValue] = {}
    for record in existing_records:
        if _runtime_environment_record_matches(
            record,
            scope=normalized_scope,
            context_name=context_name,
            instance_name=instance_name,
        ):
            env_values.update(record.env)
            break
    for raw_assignment in assignments:
        key_name, value = _parse_runtime_environment_assignment(raw_assignment)
        env_values[key_name] = value

    return RuntimeEnvironmentRecord(
        scope=normalized_scope,
        context=context_name,
        instance=instance_name,
        env=env_values,
        updated_at=utc_now_timestamp(),
        source_label=source_label.strip() or "cli",
    )


def _normalize_runtime_environment_key(raw_key: str) -> str:
    normalized_key = raw_key.strip()
    if not normalized_key:
        raise click.ClickException("Runtime environment keys must be non-empty.")
    return normalized_key


def _find_runtime_environment_record(
    *,
    existing_records: tuple[RuntimeEnvironmentRecord, ...],
    scope: str,
    context_name: str,
    instance_name: str,
) -> RuntimeEnvironmentRecord | None:
    for record in existing_records:
        if _runtime_environment_record_matches(
            record,
            scope=scope,
            context_name=context_name,
            instance_name=instance_name,
        ):
            return record
    return None


def _build_runtime_environment_record_for_unset(
    *,
    existing_records: tuple[RuntimeEnvironmentRecord, ...],
    scope: str,
    context_name: str,
    instance_name: str,
    keys: tuple[str, ...],
    source_label: str,
) -> tuple[RuntimeEnvironmentRecord, tuple[str, ...], tuple[str, ...]]:
    normalized_scope = _validate_runtime_environment_scope_route(
        scope=scope,
        context_name=context_name,
        instance_name=instance_name,
    )
    target_record = _find_runtime_environment_record(
        existing_records=existing_records,
        scope=normalized_scope,
        context_name=context_name,
        instance_name=instance_name,
    )
    if target_record is None:
        raise click.ClickException(
            "Missing DB-backed runtime environment record for the requested scope."
        )

    requested_keys = tuple(_normalize_runtime_environment_key(key_name) for key_name in keys)
    env_values = dict(target_record.env)
    removed_keys: list[str] = []
    missing_keys: list[str] = []
    for key_name in requested_keys:
        if key_name in env_values:
            removed_keys.append(key_name)
            del env_values[key_name]
        else:
            missing_keys.append(key_name)
    if not env_values:
        raise click.ClickException("Refusing to leave an empty runtime environment record.")

    return (
        RuntimeEnvironmentRecord(
            scope=target_record.scope,
            context=target_record.context,
            instance=target_record.instance,
            env=env_values,
            updated_at=utc_now_timestamp(),
            source_label=source_label.strip() or "cli",
        ),
        tuple(sorted(removed_keys)),
        tuple(sorted(missing_keys)),
    )


def _build_runtime_environment_record_for_relabel(
    *,
    existing_records: tuple[RuntimeEnvironmentRecord, ...],
    scope: str,
    context_name: str,
    instance_name: str,
    source_label: str,
) -> RuntimeEnvironmentRecord:
    normalized_scope = _validate_runtime_environment_scope_route(
        scope=scope,
        context_name=context_name,
        instance_name=instance_name,
    )
    target_record = _find_runtime_environment_record(
        existing_records=existing_records,
        scope=normalized_scope,
        context_name=context_name,
        instance_name=instance_name,
    )
    if target_record is None:
        raise click.ClickException(
            "Missing DB-backed runtime environment record for the requested scope."
        )
    return RuntimeEnvironmentRecord(
        scope=target_record.scope,
        context=target_record.context,
        instance=target_record.instance,
        env=dict(target_record.env),
        updated_at=utc_now_timestamp(),
        source_label=source_label.strip() or "cli",
    )


def _runtime_environment_delete_actor(actor: str) -> str:
    return (
        actor.strip()
        or os.environ.get("GITHUB_ACTOR", "").strip()
        or os.environ.get("USER", "").strip()
        or os.environ.get("LOGNAME", "").strip()
        or "unknown"
    )


def _runtime_environment_delete_event_id(
    *,
    record: RuntimeEnvironmentRecord,
    actor: str,
    recorded_at: str,
    nonce: str,
) -> str:
    key_digest_input = "\n".join(
        (
            record.scope,
            record.context,
            record.instance,
            record.updated_at,
            record.source_label,
            actor,
            recorded_at,
            nonce,
            *sorted(record.env.keys()),
        )
    )
    digest = hashlib.sha256(key_digest_input.encode("utf-8")).hexdigest()[:16]
    return f"runtime-env-delete-{digest}"


def _build_runtime_environment_delete_event(
    *,
    record: RuntimeEnvironmentRecord,
    actor: str,
) -> RuntimeEnvironmentDeleteEvent:
    recorded_at = utc_now_timestamp()
    normalized_actor = _runtime_environment_delete_actor(actor)
    env_keys = tuple(sorted(record.env.keys()))
    return RuntimeEnvironmentDeleteEvent(
        event_id=_runtime_environment_delete_event_id(
            record=record,
            actor=normalized_actor,
            recorded_at=recorded_at,
            nonce=str(time.time_ns()),
        ),
        recorded_at=recorded_at,
        actor=normalized_actor,
        scope=record.scope,
        context=record.context,
        instance=record.instance,
        source_label=record.source_label,
        env_keys=env_keys,
        env_value_count=len(env_keys),
        detail="deleted by launchplane environments delete-record",
    )


def _summarize_runtime_environment_delete_event(
    event: RuntimeEnvironmentDeleteEvent,
) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "recorded_at": event.recorded_at,
        "actor": event.actor,
        "scope": event.scope,
        "context": event.context,
        "instance": event.instance,
        "source_label": event.source_label,
        "env_keys": list(event.env_keys),
        "env_value_count": event.env_value_count,
        "detail": event.detail,
    }


def _protected_runtime_environment_delete_targets(
    *,
    scope: str,
    context_name: str,
    instance_name: str,
    target_records: tuple[DokployTargetRecord, ...],
) -> tuple[dict[str, object], ...]:
    protected_targets: list[dict[str, object]] = []
    for target_record in target_records:
        if scope == "context" and target_record.context != context_name:
            continue
        if scope == "instance" and (
            target_record.context != context_name or target_record.instance != instance_name
        ):
            continue
        protected_targets.append(
            {
                "context": target_record.context,
                "instance": target_record.instance,
                "target_name": target_record.target_name,
                "target_type": target_record.target_type,
                "source_label": target_record.source_label,
            }
        )
    return tuple(
        sorted(protected_targets, key=lambda item: (str(item["context"]), str(item["instance"])))
    )


class _RuntimeEnvironmentCallbacks:
    def __init__(
        self,
        *,
        control_plane_root: Callable[[], Path],
        build_live_target_runtime_contract_payload: Callable[..., dict[str, object]],
    ) -> None:
        self.control_plane_root = control_plane_root
        self.build_live_target_runtime_contract_payload = build_live_target_runtime_contract_payload


_RUNTIME_ENVIRONMENT_CALLBACKS: _RuntimeEnvironmentCallbacks | None = None


def _runtime_environment_callbacks() -> _RuntimeEnvironmentCallbacks:
    if _RUNTIME_ENVIRONMENT_CALLBACKS is None:
        raise click.ClickException("Runtime environment CLI callbacks were not registered.")
    return _RUNTIME_ENVIRONMENT_CALLBACKS
