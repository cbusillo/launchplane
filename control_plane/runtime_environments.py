from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import click

from control_plane import dokploy as control_plane_dokploy
from control_plane import secrets as control_plane_secrets
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.storage.factory import resolve_database_url
from control_plane.storage.postgres import PostgresRecordStore

DEFAULT_RUNTIME_ENVIRONMENTS_FILE = "config/runtime-environments.toml"

ScalarValue = str | int | float | bool
ScalarMap = dict[str, ScalarValue]


@dataclass(frozen=True)
class RuntimeEnvironmentInstanceDefinition:
    env: ScalarMap


@dataclass(frozen=True)
class RuntimeEnvironmentContextDefinition:
    shared_env: ScalarMap
    instances: dict[str, RuntimeEnvironmentInstanceDefinition]


@dataclass(frozen=True)
class RuntimeEnvironmentDefinition:
    schema_version: int
    shared_env: ScalarMap
    contexts: dict[str, RuntimeEnvironmentContextDefinition]


def load_runtime_environment_definition(
    *, control_plane_root: Path
) -> RuntimeEnvironmentDefinition:
    database_url = resolve_database_url()
    if database_url:
        database_definition = _load_optional_runtime_environment_definition_from_database(database_url=database_url)
        if database_definition is not None:
            return database_definition
        raise click.ClickException(
            "Missing DB-backed Launchplane runtime environment records."
        )

    raise click.ClickException(
        "Missing Launchplane runtime environment authority. Configure DB-backed runtime environment records."
    )


def resolve_runtime_environment_values(
    *,
    control_plane_root: Path,
    context_name: str,
    instance_name: str,
) -> dict[str, str]:
    definition = load_runtime_environment_definition(control_plane_root=control_plane_root)
    merged_values: dict[str, str] = _normalize_scalar_map(definition.shared_env)
    context_definition = definition.contexts.get(context_name)
    if context_definition is None:
        raise click.ClickException(
            f"Runtime environments file has no context definition for {context_name!r}."
        )
    merged_values.update(_normalize_scalar_map(context_definition.shared_env))
    instance_definition = context_definition.instances.get(instance_name)
    if instance_definition is None:
        raise click.ClickException(
            f"Runtime environments file has no instance definition for {context_name}/{instance_name}."
        )
    merged_values.update(_normalize_scalar_map(instance_definition.env))
    merged_values.update(
        resolve_tracked_target_environment_values(
            control_plane_root=control_plane_root,
            context_name=context_name,
            instance_name=instance_name,
        )
    )
    return control_plane_secrets.overlay_runtime_environment_secret_values(
        environment_values=merged_values,
        context_name=context_name,
        instance_name=instance_name,
    )


def resolve_runtime_context_values(
    *,
    control_plane_root: Path,
    context_name: str,
) -> dict[str, str]:
    definition = load_runtime_environment_definition(control_plane_root=control_plane_root)
    merged_values: dict[str, str] = _normalize_scalar_map(definition.shared_env)
    context_definition = definition.contexts.get(context_name)
    if context_definition is None:
        raise click.ClickException(
            f"Runtime environments file has no context definition for {context_name!r}."
        )
    merged_values.update(_normalize_scalar_map(context_definition.shared_env))
    return control_plane_secrets.overlay_runtime_environment_secret_values(
        environment_values=merged_values,
        context_name=context_name,
    )


def resolve_tracked_target_environment_values(
    *,
    control_plane_root: Path,
    context_name: str,
    instance_name: str,
) -> dict[str, str]:
    try:
        source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
            control_plane_root=control_plane_root
        )
    except click.ClickException as error:
        if str(error).startswith("Missing Launchplane tracked Dokploy target authority") or str(error).startswith(
            "Missing DB-backed Launchplane tracked Dokploy target records."
        ):
            return {}
        raise
    target_definition = control_plane_dokploy.find_dokploy_target_definition(
        source_of_truth,
        context_name=context_name,
        instance_name=instance_name,
    )
    if target_definition is None:
        return {}
    return dict(target_definition.env)


def _parse_runtime_environment_definition(
    payload: dict[str, object],
    *,
    source_file: Path,
) -> RuntimeEnvironmentDefinition:
    schema_version = _read_required_int(payload, "schema_version", scope="runtime_environments")
    contexts_table = _read_optional_table(payload, "contexts", scope="runtime_environments")
    contexts: dict[str, RuntimeEnvironmentContextDefinition] = {}
    for context_name, raw_context in contexts_table.items():
        context_table = _ensure_table(
            raw_context,
            scope=f"runtime_environments.contexts.{context_name}",
        )
        instances_table = _read_optional_table(
            context_table,
            "instances",
            scope=f"runtime_environments.contexts.{context_name}",
        )
        instances: dict[str, RuntimeEnvironmentInstanceDefinition] = {}
        for instance_name, raw_instance in instances_table.items():
            instance_table = _ensure_table(
                raw_instance,
                scope=f"runtime_environments.contexts.{context_name}.instances.{instance_name}",
            )
            instances[instance_name] = RuntimeEnvironmentInstanceDefinition(
                env=_read_optional_scalar_map(
                    instance_table,
                    "env",
                    scope=f"runtime_environments.contexts.{context_name}.instances.{instance_name}",
                )
            )
        contexts[context_name] = RuntimeEnvironmentContextDefinition(
            shared_env=_read_optional_scalar_map(
                context_table,
                "shared_env",
                scope=f"runtime_environments.contexts.{context_name}",
            ),
            instances=instances,
        )
    return RuntimeEnvironmentDefinition(
        schema_version=schema_version,
        shared_env=_read_optional_scalar_map(payload, "shared_env", scope="runtime_environments"),
        contexts=contexts,
    )


def _load_optional_runtime_environment_definition_from_database(
    *, database_url: str
) -> RuntimeEnvironmentDefinition | None:
    record_store: PostgresRecordStore | None = None
    try:
        record_store = PostgresRecordStore(database_url=database_url)
        record_store.ensure_schema()
        records = record_store.list_runtime_environment_records()
    except Exception as error:
        raise click.ClickException(
            f"Could not load runtime environments from Launchplane Postgres storage: {error}"
        ) from error
    finally:
        try:
            if record_store is not None:
                record_store.close()
        except Exception:
            pass
    if not records:
        return None
    return build_runtime_environment_definition_from_records(records)


def build_runtime_environment_definition_from_records(
    records: tuple[RuntimeEnvironmentRecord, ...],
) -> RuntimeEnvironmentDefinition:
    shared_env: ScalarMap = {}
    contexts: dict[str, RuntimeEnvironmentContextDefinition] = {}
    for record in sorted(records, key=lambda item: (item.scope, item.context, item.instance)):
        if record.scope == "global":
            shared_env.update(record.env)
            continue
        context_definition = contexts.setdefault(
            record.context,
            RuntimeEnvironmentContextDefinition(shared_env={}, instances={}),
        )
        if record.scope == "context":
            merged_shared_env = dict(context_definition.shared_env)
            merged_shared_env.update(record.env)
            contexts[record.context] = RuntimeEnvironmentContextDefinition(
                shared_env=merged_shared_env,
                instances=dict(context_definition.instances),
            )
            continue
        instances = dict(context_definition.instances)
        instances[record.instance] = RuntimeEnvironmentInstanceDefinition(env=dict(record.env))
        contexts[record.context] = RuntimeEnvironmentContextDefinition(
            shared_env=dict(context_definition.shared_env),
            instances=instances,
        )
    return RuntimeEnvironmentDefinition(schema_version=1, shared_env=shared_env, contexts=contexts)


def build_runtime_environment_records_from_definition(
    definition: RuntimeEnvironmentDefinition,
    *,
    updated_at: str,
    source_label: str,
) -> tuple[RuntimeEnvironmentRecord, ...]:
    records: list[RuntimeEnvironmentRecord] = []
    if definition.shared_env:
        records.append(
            RuntimeEnvironmentRecord(
                scope="global",
                env=dict(definition.shared_env),
                updated_at=updated_at,
                source_label=source_label,
            )
        )
    for context_name, context_definition in sorted(definition.contexts.items()):
        if context_definition.shared_env:
            records.append(
                RuntimeEnvironmentRecord(
                    scope="context",
                    context=context_name,
                    env=dict(context_definition.shared_env),
                    updated_at=updated_at,
                    source_label=source_label,
                )
            )
        for instance_name, instance_definition in sorted(context_definition.instances.items()):
            if not instance_definition.env:
                continue
            records.append(
                RuntimeEnvironmentRecord(
                    scope="instance",
                    context=context_name,
                    instance=instance_name,
                    env=dict(instance_definition.env),
                    updated_at=updated_at,
                    source_label=source_label,
                )
            )
    return tuple(records)


def _normalize_scalar_map(raw_values: ScalarMap) -> dict[str, str]:
    return {key: str(value) for key, value in raw_values.items()}


def _read_required_int(source: dict[str, object], key: str, *, scope: str) -> int:
    value = source.get(key)
    if not isinstance(value, int):
        raise click.ClickException(f"Expected {scope}.{key} to be an integer.")
    return value


def _read_optional_table(source: dict[str, object], key: str, *, scope: str) -> dict[str, object]:
    value = source.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise click.ClickException(f"Expected {scope}.{key} to be a table when present.")
    return value


def _ensure_table(value: object, *, scope: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise click.ClickException(f"Expected {scope} to be a table.")
    return value


def _read_optional_scalar_map(source: dict[str, object], key: str, *, scope: str) -> ScalarMap:
    value = source.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise click.ClickException(f"Expected {scope}.{key} to be a table when present.")
    scalar_map: ScalarMap = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str):
            raise click.ClickException(f"Expected {scope}.{key} keys to be strings.")
        if not isinstance(raw_value, (str, int, float, bool)):
            raise click.ClickException(f"Expected {scope}.{key}.{raw_key} to be a scalar value.")
        scalar_map[raw_key] = raw_value
    return scalar_map
